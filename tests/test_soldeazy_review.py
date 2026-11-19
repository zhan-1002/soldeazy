"""人工核对靠两份文件，不靠 AI 读明细，也不靠令牌。

设计约束（2026-11-17 用户要求）：

1. **AI 只读返回的计数与判决**。明细不进对话——之前让 AI 把上百条新旧值
   逐条读一遍，实测又慢又贵（累积 6000 万 token），而且不比机械比对可靠。
2. **人工核对常被越过**，所以要给它一个真实落点：预览直接产出
   ``review_before`` / ``review_after`` 两份 xlsx，打开就能逐行看。
3. **两份文件来自上传用的同一份导出与同一份变更列表**，不另算一套口径 ——
   保证"人看到的 = 会上传的"。
4. **AI 与人分工不重叠**：AI 看计数，人看逐行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from soldeazy_mcp_server.providers.soldeazy_review import (
    MARK_COLUMN,
    build_review_files,
)
from soldeazy_mcp_server.providers.soldeazy_workbook import (
    diff_matrices,
    read_matrix,
)


def _make_export(path: Path, rows: int = 3) -> list[list]:
    """造一份与真实导出同结构的文件：两行表头 + 一个数据表 + 若干子体行。"""

    from openpyxl import Workbook

    header1 = ["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku",
               "dsheet_product_code", "dsheet_qty", "dsheet_price",
               "dsheet_first_post", "dsheet_EOF"]
    header2 = ["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price",
               "Post.D.", "dsheet_EOF"]
    body = [["4569384", "FUN", "ZM-P", "ZMDC0063", "100", "21.9", None, "o"]]
    for i in range(1, rows):
        body.append([None, None, "ZM-P", f"ZMDC006{i}", "100", "22.9", None, "o"])
    body.append(["4569385", "FUN", "ZM-Q", "ZMDC0070", "50", "9.9", None, "o"])

    wb = Workbook()
    ws = wb.active
    for row in (header1, header2, *body):
        ws.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    wb.close()
    return [header1, header2, *body]


def test_review_files_are_produced_and_readable(tmp_path) -> None:
    src = tmp_path / "export.xlsx"
    _make_export(src)

    changes = [
        ("price", 3, 25.0),   # 第 3 行（ZMDC0063）改价
        ("qty", 4, 75.0),     # 第 4 行（子体）改量
    ]
    files = build_review_files(src, tmp_path / "out", "p1", changes)

    assert files.before.is_file()
    assert files.after.is_file()
    assert files.rows == 2
    assert files.price_rows == 1
    assert files.qty_rows == 1
    # 两个数据表（4569384 与 4569385）
    assert files.datasheets == 2


def test_before_holds_old_values_and_after_holds_new(tmp_path) -> None:
    src = tmp_path / "export.xlsx"
    _make_export(src)

    changes = [("price", 3, 25.0), ("qty", 3, 75.0)]
    files = build_review_files(src, tmp_path / "out", "p1", changes)

    _n1, before = read_matrix(files.before)
    _n2, after = read_matrix(files.after)

    # 第 3 行（0-based 索引 2）：E 列数量、F 列价格
    assert before[2][4] == "100"
    assert before[2][5] == "21.9"
    assert after[2][4] == 75.0
    assert after[2][5] == 25.0


def test_mark_column_says_what_changed(tmp_path) -> None:
    """人一眼能看到「F: 21.9→25.0」，不用两文件来回对。"""

    src = tmp_path / "export.xlsx"
    _make_export(src)

    files = build_review_files(
        src, tmp_path / "out", "p1", [("price", 3, 25.0), ("qty", 3, 75.0)]
    )
    _name, after = read_matrix(files.after)

    assert after[0][-1] == MARK_COLUMN
    mark = after[2][-1]
    assert "F:" in mark and "21.9" in mark and "25.0" in mark
    assert "E:" in mark and "100" in mark and "75.0" in mark


def test_unchanged_rows_have_no_mark(tmp_path) -> None:
    src = tmp_path / "export.xlsx"
    _make_export(src)

    files = build_review_files(src, tmp_path / "out", "p1", [("price", 3, 25.0)])
    _name, after = read_matrix(files.after)

    # 第 4 行没改，标记应为空
    assert after[3][-1] in ("", None)


def test_keep_rows_narrows_to_the_datasheets_being_changed(tmp_path) -> None:
    """几千行里只改几十行时，核对文件也只给这几十行——否则人找不到。"""

    src = tmp_path / "export.xlsx"
    _make_export(src)

    # 只保留第 1、2 行表头 + 第 3 行（第一个数据表的母体行）
    files = build_review_files(
        src, tmp_path / "out", "p1", [("price", 3, 25.0)], keep_rows=[3]
    )
    _name, after = read_matrix(files.after)

    assert len(after) == 3                       # 2 行表头 + 1 行数据
    assert after[2][3] == "ZMDC0063"


def test_review_files_never_touch_columns_outside_e_and_f(tmp_path) -> None:
    """核对文件的数据部分只改 E/F；那列「变更标记」是给人看的，不算数据。"""

    src = tmp_path / "export.xlsx"
    _make_export(src)

    files = build_review_files(
        src, tmp_path / "out", "p1", [("price", 3, 25.0), ("qty", 3, 75.0)]
    )
    _n1, before = read_matrix(files.before)
    _n2, after = read_matrix(files.after)

    diffs = diff_matrices(before, after)
    letters = {d.letter for d in diffs}
    # G 是第 8 列之后的标记列（真实导出 8 列 → 标记落在 I）
    data_letters = {x for x in letters if x not in {"I", "G"}}
    assert data_letters <= {"E", "F"}, sorted(letters)


def test_missing_source_raises_not_silently_skips(tmp_path) -> None:
    """源文件不存在必须报错，不能生成两份空文件让人误以为核对过了。"""

    with pytest.raises(Exception):
        build_review_files(
            tmp_path / "nope.xlsx", tmp_path / "out", "p1", [("price", 3, 1.0)]
        )
