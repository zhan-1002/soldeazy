"""人工核对用的可读文件：由导出表直接生成「变更前 / 变更后」两份 xlsx。

为什么要有这个模块（2026-11-17 用户要求）：

1. **AI 不该读逐条明细**。之前让 AI 承接"把 137 条新旧值读一遍"的工作，
   实测又慢又贵（累积到 6000 万 token），而且 AI 核对并不比机械比对更可靠。
   AI 只该读工具返回的**判决与计数**，明细交给人。

2. **人工核对常被越过**，因为"要看明细"这件事在流程里没有落点——
   对话里只塞 5 条 sample，想看全量得靠 verbose 撑爆 token。
   现在把它变成**磁盘上的两份文件**：打开就能逐行看，不需要任何 token。

3. **两份文件直接来自上传用的那份数据表**，不是另算一遍——
   保证"人看到的就是要上传的"，不存在第二套口径。

4. **AI 与人分工不重叠**：AI 看计数（改了几条、几份表、跳过多少套装），
   人看逐行（第 N 行的 E/F 列从 X 变成 Y）。两边不重复劳动。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from soldeazy_mcp_server.providers.soldeazy_workbook import (
    FIELD_COLUMNS,
    apply_changes,
    column_letter,
    header_row_count,
    read_matrix,
)

#: 核对文件里额外标注变化的列（放在数据表最右侧，不干扰原有列）
MARK_COLUMN = "变更标记"


@dataclass(frozen=True)
class ReviewFiles:
    """一次核对产出的两份文件 + 给人看的摘要。"""

    before: Path
    after: Path
    rows: int
    datasheets: int
    price_rows: int
    qty_rows: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_before": str(self.before),
            "review_after": str(self.after),
            "review_rows": self.rows,
            "review_datasheets": self.datasheets,
            "review_price_rows": self.price_rows,
            "review_qty_rows": self.qty_rows,
        }


def _mark(row_number: int, changes_by_row: dict[int, list[tuple[str, Any, Any]]]) -> str:
    """给某一行生成「E: 100→50; F: 21.9→25」这样的可读标记。"""

    items = changes_by_row.get(row_number)
    if not items:
        return ""
    parts = []
    for field, old, new in items:
        letter = column_letter(FIELD_COLUMNS[field])
        parts.append(f"{letter}: {old}→{new}")
    return "; ".join(parts)


def _index_changes(
    before: Sequence[Sequence[Any]],
    changes: Sequence[tuple[str, int, Any]],
) -> dict[int, list[tuple[str, Any, Any]]]:
    """``行号 -> [(字段, 旧值, 新值)]``，用于在「变更后」文件上标注。"""

    indexed: dict[int, list[tuple[str, Any, Any]]] = {}
    for field, row_number, new_value in changes:
        column = FIELD_COLUMNS.get(field)
        if 0 < row_number <= len(before) and column:
            old_value = before[row_number - 1][column - 1]
        else:
            old_value = None
        indexed.setdefault(row_number, []).append((field, old_value, new_value))
    return indexed


def _trim_rows(
    before: Sequence[Sequence[Any]],
    after: Sequence[Sequence[Any]],
    keep_rows: Sequence[int] | None,
    header: int,
) -> tuple[list[Sequence[Any]], list[Sequence[Any]], dict[int, int]]:
    """只保留计划涉及的行（表头行始终保留），并给出「新位置 → 原行号」映射。"""

    if keep_rows is None:
        rows = list(before)
        return rows, list(after), {i: i for i in range(1, len(before) + 1)}

    keep = set(keep_rows) | set(range(1, header + 1))
    positions = [i for i in range(1, len(before) + 1) if i in keep]
    trimmed_before = [before[i - 1] for i in positions]
    trimmed_after = [after[i - 1] for i in positions]
    return trimmed_before, trimmed_after, {
        new: old for new, old in enumerate(positions, start=1)
    }


def _count_datasheets(rows: Sequence[Sequence[Any]], header: int) -> int:
    """数据表数按「保留后的行里出现过的 rowid」算，与上传范围口径一致。"""

    return len(
        {
            str(row[0]).strip()
            for row in rows[header:]
            if row and str(row[0] or "").strip()
        }
    )


def build_review_files(
    src: str | Path,
    workdir: str | Path,
    plan_id: str,
    changes: Sequence[tuple[str, int, Any]],
    *,
    keep_rows: Sequence[int] | None = None,
) -> ReviewFiles:
    """由导出表 ``src`` 直接生成变更前/后两份核对文件。

    ``changes`` 与上传用的是**同一份** ``(字段, 行号, 新值)`` 列表，
    所以人对完这两份文件，等于对完了将要上传的内容。

    ``keep_rows`` 给出时只保留这些行（与上传范围一致），避免人对着
    几千行没改的数据找那几十行。

    按「收集变更 → 裁剪行 → 追加标记列 → 统计」拆成 4 段。
    """

    name, before = read_matrix(src)
    after = apply_changes(before, changes)
    header = header_row_count(before)

    changes_by_row = _index_changes(before, changes)
    before, after, row_map = _trim_rows(before, after, keep_rows, header)

    # 在末尾追加「变更标记」列
    before_rows = [list(row) + [""] for row in before]
    after_rows = [
        list(row) + [_mark(row_map.get(index, index), changes_by_row)]
        for index, row in enumerate(after, start=1)
    ]
    # 表头写明这一列是什么
    if before_rows:
        before_rows[0][-1] = MARK_COLUMN
        after_rows[0][-1] = MARK_COLUMN

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    before_path = workdir / f"{plan_id}.review-before.xlsx"
    after_path = workdir / f"{plan_id}.review-after.xlsx"

    _write(before_path, before_rows, name)
    _write(after_path, after_rows, name)

    # 核对文件最右侧那一列「变更标记」**只给人看，不是数据**。
    # 千万不要拿 review-after.xlsx 去上传：它是 8 列 + 1 列标记 = 9 列，
    # 上传会多一列。真正要上传的是 apply_plan 内部生成的 done.xlsx。
    # 这里不删这一列，是因为它在核对时价值最高（一眼看到"从多少变成多少"）。

    return ReviewFiles(
        before=before_path,
        after=after_path,
        rows=len(changes),
        datasheets=_count_datasheets(after, header),
        price_rows=sum(1 for field, *_ in changes if field == "price"),
        qty_rows=sum(1 for field, *_ in changes if field == "qty"),
    )


def _write(path: Path, rows: list[list[Any]], sheet: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet[:31] or "Sheet1"

    for row in rows:
        worksheet.append(row)

    # 表头加粗 + 冻结，方便人往下翻
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
    worksheet.freeze_panes = "A2"

    workbook.save(str(path))
    workbook.close()


async def write_review_async(*args: Any, **kwargs: Any) -> ReviewFiles:
    """在事件循环里跑（openpyxl 是同步 IO，包一层避免阻塞）。"""

    return await asyncio.to_thread(build_review_files, *args, **kwargs)
