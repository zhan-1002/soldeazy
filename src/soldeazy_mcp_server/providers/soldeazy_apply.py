"""把冻结的改动计划落到线上，并对账。

- ``build_plan``   读当前实际值 → 生成计划条目（只读，不写）
- ``apply_plan``   按 rowid 分组，一次表单提交改完一个数据表（原子）
- ``reconcile``    重新读实际值，与计划逐条比对，报告写成新文件
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from soldeazy_mcp_server.providers.datasheet_browser import DatasheetBrowser
from soldeazy_mcp_server.providers.soldeazy import PLATFORM_BULK_LIMIT
from soldeazy_mcp_server.providers.soldeazy_plan import (
    MERGED_QUERY_MAX_SKUS,
    ChangePlan,
    PlanItem,
    new_plan_id,
)

logger = logging.getLogger("soldeazy_mcp.apply")

# 套装后缀：SKU 尾巴上的 " x2" / "x4"。单件没有这个后缀。
PACK_SUFFIX = re.compile(r"\s*[xX]\s*(\d+)\s*$")

PRICE = "price"
QTY = "qty"
TOLERANCE = 0.005


#: 等同步工序任务的轮数（每轮 6 秒）。
#:
#: 已刊登计划：10 轮（60 秒）——同步任务确实会出现，值得等。
#: 未刊登计划：**0 轮**。未刊登数据表在平台上同步不上去（FUN 实机：历史 25 个
#: 工序任务全是 DSUploadTask，从来没有同步类任务），所以等它是纯浪费——
#: 原来设 2 轮仍要空烧 12 秒。现在直接跳过，靠下面的 else 分支记一条
#: 「工序队列没观察到同步任务，未刊登属预期」的警告（不判失败）。
SYNC_JOB_ATTEMPTS = 10
SYNC_JOB_ATTEMPTS_UNLISTED = 0

#: 对账把计划里的 rowid 拼成查询串时允许的最大长度。
#:
#: 实测（2026-11-18）：``3117213 3117211 3117210`` 能精确命中这些数据表，
#: 但**不能带 ``*``**（``*3117213`` 反而 0 命中）。rowid 都是 7 位数字，
#: 每个约占 8 字符；3000 个就是 24KB，查询串肯定过长。
#: 超过这个长度就退回全店导出——慢，但不会漏。
ROWID_QUERY_MAX_CHARS = 4000


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def split_pack(sku: str) -> tuple[str, int | None]:
    """把 ``ZMHB0004US x4`` 拆成 ``("ZMHB0004US", 4)``；不是套装返回 ``(sku, None)``。

    只看 SKU 尾巴上的 ``xN``，不猜件数——件数由用户显式写出来。
    """

    text = str(sku or "").strip()
    match = PACK_SUFFIX.search(text)
    if not match:
        return text, None
    return text[: match.start()].strip(), int(match.group(1))


def _target_pairs(
    live: dict[tuple[str, str], dict[str, Any]],
    key: str,
    field: str,
    hits: Sequence[tuple[str, str]],
    target: float,
) -> list[PlanItem]:
    """把命中的行转成计划条目，跳过「读不到现值」和「已经是目标值」。

    两路（按子体改价、按母体改量）共用这一段，避免"一边跳过一边不跳"。
    """

    items: list[PlanItem] = []
    for rowid, sku in hits:
        record = live.get((rowid, sku)) or {}
        old = _num(record.get(key))
        if old is None:
            continue
        if abs(old - float(target)) < TOLERANCE:
            continue
        items.append(
            PlanItem(rowid=rowid, sku=sku, field=field, old=old, new=float(target))
        )
    return items


def _price_matches(
    live: dict[tuple[str, str], dict[str, Any]],
    targets: dict[str, float],
) -> tuple[list[PlanItem], list[str], dict[str, list[str]]]:
    """按子体 SKU 精确匹配（改价）。"""

    by_sku: dict[str, list[tuple[str, str]]] = {}
    for rowid, sku in live:
        by_sku.setdefault(sku, []).append((rowid, sku))

    items: list[PlanItem] = []
    missing: list[str] = []
    needs_confirmation: dict[str, list[str]] = {}

    for raw_sku, target in targets.items():
        wanted = str(raw_sku).strip()
        hits = by_sku.get(wanted)
        if not hits:
            missing.append(wanted)
            continue

        # 同母体下的其它 xN 行：报给人工看，不自动改
        base, _pack = split_pack(wanted)
        siblings = sorted(
            sku for sku in by_sku if sku != wanted and split_pack(sku)[0] == base
        )
        if siblings:
            needs_confirmation[wanted] = siblings

        items.extend(_target_pairs(live, "price", PRICE, hits, target))

    return items, missing, needs_confirmation


def _qty_matches(
    live: dict[tuple[str, str], dict[str, Any]],
    targets: dict[str, float],
) -> tuple[list[PlanItem], list[str]]:
    """按母体 SKU 匹配（改量）：命中它下面所有行，这是库存的语义本身。"""

    items: list[PlanItem] = []
    missing: list[str] = []

    for raw_sku, target in targets.items():
        wanted = str(raw_sku).strip()
        hits = [
            (rowid, sku)
            for (rowid, sku), record in live.items()
            if str(record.get("parent", "")).strip() == wanted
        ]
        if not hits:
            missing.append(wanted)
            continue
        items.extend(_target_pairs(live, "qty", QTY, hits, target))

    return items, missing


def build_items(
    live: dict[tuple[str, str], dict[str, Any]],
    kind: str,
    targets: dict[str, float],
) -> tuple[list[PlanItem], dict[str, Any]]:
    """按用户的 ``{SKU: 数值}`` 生成计划条目。

    **只做精确匹配，不替用户猜。** 搜索用 ``*产品代码`` 通配返回的就是这批
    代码的全部变体（每个变体在导出里是一个独立的 ``(rowid, sku)`` 行），
    所以用户写什么 SKU 就精确命中什么；写错了就是写错了，报 ``missing``
    让调用方自己看，不用前缀去"猜一个最像的"。

    历史上这里有一段 ``startswith`` 前缀兜底 + ``conflicts`` 歧义拒写：
    它在正确的场景下从不触发（精确匹配已经够用），却在写错时替用户挑候选，
    是净负担，已删除。

    **套装交给人工确认**，不自动决定：

    - 改价：只写用户**明确写出**的那一行。``ZMFL0012`` 命中单件行，
      ``ZMFL0012 x2`` 才算点了套装。发现同母体下还有 xN 行时，列进
      ``needs_confirmation`` —— 告诉人工"这里还有套装，要不要一起改"，
      但**不替人工改，也不静默跳过**。
    - 改库存：SKU 指**母体**，命中它下面所有行（父行 + 全部子体行），
      这是库存的语义本身，不涉及套装判断。

    两路各自的匹配逻辑在 ``_price_matches`` / ``_qty_matches``，
    共用的"跳过无值/已是目标值"在 ``_target_pairs``。
    """

    if kind == "price":
        items, missing, needs_confirmation = _price_matches(live, targets)
    else:
        items, missing = _qty_matches(live, targets)
        needs_confirmation = {}

    return items, {
        "missing": sorted(missing),
        # 同母体下还有别的变体（x2/x4…）没被点到：**交人工判断**要不要一起改。
        # 我们不改，也不拦——只把事实摆出来。
        "needs_confirmation": needs_confirmation,
    }
    return items, report


def make_plan(
    kind: str,
    site: str,
    shop: dict[str, str],
    items: list[PlanItem],
    *,
    supersedes: str = "",
    note: str = "",
    only_listed: bool = True,
) -> ChangePlan:
    return ChangePlan(
        plan_id=new_plan_id(kind),
        kind=kind,
        site=site,
        shop=shop,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        items=items,
        supersedes=supersedes,
        note=note,
        only_listed=only_listed,
    )


def _group(plan: ChangePlan) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in plan.items:
        grouped.setdefault(item.rowid, []).append(
            {"sku": item.sku, "field": item.field, "new": item.new}
        )
    return grouped


def index_rows(matrix: list[list[Any]]) -> dict[tuple[str, str], int]:
    """``(母体rowid, 子体SKU) -> 导出文件里的 1-based 行号``。

    子体行没有 Row ID，归属到最近一个带 rowid 的行。
    """

    from soldeazy_mcp_server.providers.soldeazy_workbook import header_row_count

    index: dict[tuple[str, str], int] = {}
    current = ""
    header = header_row_count(matrix)
    for number, row in enumerate(matrix, start=1):
        if number <= header:
            continue
        rowid = str(row[0] or "").strip()
        sku = str(row[3] or "").strip()
        if rowid:
            current = rowid
        if sku:
            index[(current, sku)] = number
    return index


def plan_to_cell_changes(
    plan: ChangePlan, matrix: list[list[Any]]
) -> tuple[list[tuple[str, int, Any]], list[dict[str, Any]]]:
    """把计划条目换成 ``(字段, 行号, 新值)``；定位不到的行单独报出来。"""

    index = index_rows(matrix)
    changes: list[tuple[str, int, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in plan.items:
        row = index.get((item.rowid, item.sku))
        if row is None:
            missing.append(item.as_dict())
            continue
        changes.append((item.field, row, item.new))
    return changes, missing


def _shop_name_matches(expected: str, name: str) -> bool:
    """店铺名比对：导出列写短名（FUN），页面/搜索结果写长名（EBAY FUN）。"""

    if not expected or not name:
        return False
    return name == expected or expected in name or name in expected


def _shop_switched(
    expected_id: str, expected_name: str, current_id: str
) -> str | None:
    """活动账号被切走了没有。"""

    if expected_id and current_id and expected_id != current_id:
        return (
            f"活动渠道账号已从 {expected_name or expected_id} 切到 {current_id}，"
            "请重新预览"
        )
    return None


def _shops_in_matrix(matrix: list[list[Any]]) -> set[str]:
    """导出文件第 2 列出现的店铺名（``dsheet_shop_initial``）。"""

    from soldeazy_mcp_server.providers.soldeazy_workbook import header_row_count

    header = header_row_count(matrix)
    return {
        str(row[1] or "").strip()
        for row in matrix[header:]
        if len(row) > 1 and str(row[1] or "").strip()
    }


def shop_problems(plan: ChangePlan, matrix: list[list[Any]], current_shop: dict | None) -> list[str]:
    """确认导出的数据和计划属于同一个渠道账号。

    两个来源都要对：a) 当前活动账号；b) 导出文件里实际出现的店铺名。
    少任何一条，切号后执行都会悄悄写进别的店铺。
    """

    expected_id = str((plan.shop or {}).get("id") or "").strip()
    expected_name = str((plan.shop or {}).get("name") or "").strip()
    current_id = str((current_shop or {}).get("id") or "").strip()

    problems: list[str] = []
    switched = _shop_switched(expected_id, expected_name, current_id)
    if switched:
        problems.append(switched)

    shops = _shops_in_matrix(matrix)
    missing = bool(expected_name) and bool(shops) and not any(
        _shop_name_matches(expected_name, name) for name in shops
    )
    if missing:
        problems.append(
            f"导出结果里没有计划所属的渠道账号 {expected_name}，实际为 {sorted(shops)}"
        )
    return problems


def shop_scope_problem(plan: ChangePlan, shops: list[str] | None) -> str:
    """对账前确认导出的数据里有没有计划所属的店铺。

    没设 active_shop 时 search 不做店铺过滤（平台语义：不选 = 全部店铺），
    导出的是所有账号混在一起的数据；只要计划店铺的行在里面就能对账，
    完全不含计划店铺才说明读错了范围。
    返回非空字符串表示不能据此下结论。
    """

    expected = str((plan.shop or {}).get("name") or "").strip()
    seen = [name for name in (shops or []) if name]
    if not expected or not seen:
        return ""
    # 导出列写的是短名（FUN），搜索结果里是长名（EBAY FUN），两种都认
    if any(_shop_name_matches(expected, name) for name in seen):
        return ""
    return (
        f"对账中止：导出的是 {seen}，计划属于 {expected}。"
        "这不是改动没生效，是读到了别的渠道账号——请先切换店铺再对账。"
    )


def append_audit(audit_path: Path, plan_id: str, rowid: str, result: dict[str, Any]) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_id": plan_id,
        "rowid": rowid,
        "success": result.get("success"),
        "applied": len(result.get("applied") or []),
        "failed": result.get("failed") or [],
        "warnings": result.get("warnings") or [],
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


#: 计划里的 kind 用 ``qty``，旧的清单文件用 ``stock``，对外按 ``stock`` 说。
_KIND_ALIAS = {"stock": "qty"}


def _plan_metadata_index(directory: Path) -> dict[str, tuple[str, str]]:
    """``plan_id -> (site, kind)``：审计行本身没有站点和类型，靠计划 JSON 补齐。

    计划归档后就都在 ``archive/`` 里，所以用 rglob 一起扫。
    """

    sites: dict[str, tuple[str, str]] = {}
    for path in directory.rglob("*.json"):
        # 计划本体是 <plan_id>.json；同目录还有 <plan_id>.reconcile.json、
        # *.json.sha256 这类附属文件，它们要么缺 site/kind，要么排序在后，
        # 一旦覆盖就会把真计划的站点/类型冲成空（真实数据上踩到过）。
        if path.name.endswith((".reconcile.json", ".audit.jsonl", ".sha256")):
            continue
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 计划文件坏了/读不了：跳过它（补不出站点与类型），但要留痕
            logger.debug("补不出计划元数据 %s：%s", path.name, exc)
            continue
        if not isinstance(plan, dict):
            logger.debug("计划文件 %s 不是对象，已跳过", path.name)
            continue
        plan_id = str(plan.get("plan_id") or "")
        plan_site = str(plan.get("site") or "").upper()
        plan_kind = str(plan.get("kind") or "").lower()
        if not plan_id or not (plan_site or plan_kind):
            continue
        sites[plan_id] = (plan_site, plan_kind)
    return sites


def _iter_audit_entries(path: Path) -> list[dict[str, Any]]:
    """读一份 ``*.audit.jsonl``，坏行跳过而不是毁掉整份历史。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        # 读不了就跳过这一份，但要说清是哪份 —— 静默跳过会让人以为
        # "历史里就是没有这些记录"
        logger.warning("跳过读不了的审计文件 %s：%s", path, exc)
        return []

    entries: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except ValueError:
            # 坏行（写一半/手工改坏）跳过，不让它毁掉整份历史
            logger.debug("审计文件 %s 有一行不是 JSON，已跳过", path.name)
            continue
        if not isinstance(entry, dict):
            logger.debug("审计文件 %s 有一行不是对象，已跳过", path.name)
            continue
        entries.append(entry)
    return entries


def read_audit(
    directory: Path, *, limit: int = 20, site: str = "", kind: str = ""
) -> list[dict[str, Any]]:
    """读新的「按计划执行」审计（``<plan_id>.audit.jsonl``），新的在前。

    审计行只有 plan_id / rowid / applied / failed，没有站点和类型，靠同目录的
    计划 JSON 补齐（计划归档后就都在 ``archive/`` 里，所以用 rglob 一起扫）。
    坏行跳过，不让一条脏记录毁掉整份历史。
    """

    if not directory.is_dir():
        return []

    sites = _plan_metadata_index(directory)
    wanted_site = (site or "").strip().upper()
    wanted_kind = _KIND_ALIAS.get((kind or "").strip().lower(), (kind or "").strip().lower())

    records: list[dict[str, Any]] = []
    for path in directory.rglob("*.audit.jsonl"):
        for entry in _iter_audit_entries(path):
            plan_id = str(entry.get("plan_id") or "")
            entry_site, entry_kind = sites.get(plan_id, ("", ""))
            if wanted_site and entry_site != wanted_site:
                continue
            if wanted_kind and entry_kind != wanted_kind:
                continue
            records.append(
                {
                    **entry,
                    "site": entry_site,
                    "kind": entry_kind,
                    "source": "plan-audit",
                    "file": path.name,
                }
            )

    records.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return records[: max(0, int(limit))]


def datasheet_spans(matrix: list[list[Any]]) -> dict[str, list[int]]:
    """``母体rowid -> 这个数据表占用的全部行号``（含母体行与所有子体行）。

    导出表里一个多变体数据表是连续的几行：母体行有 Row ID，后面跟着
    若干无 Row ID 的子体行，直到下一个带 Row ID 的行为止。
    """

    from soldeazy_mcp_server.providers.soldeazy_workbook import header_row_count

    spans: dict[str, list[int]] = {}
    current = ""
    header = header_row_count(matrix)
    for number, row in enumerate(matrix, start=1):
        if number <= header:
            continue
        rowid = str(row[0] or "").strip()
        if rowid:
            current = rowid
            spans[current] = [number]
        elif current:
            spans[current].append(number)
    return spans


def plan_rows(plan: ChangePlan, matrix: list[list[Any]]) -> list[int]:
    """计划涉及的数据表在导出文件里占用的全部行号。

    必须带上母体行与全部子体行：只传子体行会丢结构，只传母体行则子体不跟随。
    """

    spans = datasheet_spans(matrix)
    keep: set[int] = set()
    for item in plan.items:
        keep.update(spans.get(item.rowid, []))
    return sorted(keep)


def _too_many_datasheets(plan: ChangePlan, audit: Path) -> dict[str, Any] | None:
    """上传前的第一道闸：计划涉及的数据表数不能超过平台批量上限。

    超过就必然只改一部分却不报错——工序会显示成功，对账才发现一堆"查不到"。
    这种"静默少改"比直接失败危险得多，所以在发任何请求之前就拦住。
    """

    expected = len({item.rowid for item in plan.items})
    if expected <= PLATFORM_BULK_LIMIT:
        return None
    return {
        "plan_id": plan.plan_id,
        "ok": False,
        "channel": "upload",
        "error": (
            f"计划涉及 {expected} 个数据表，超过平台单次批量上限 "
            f"{PLATFORM_BULK_LIMIT} 个，已中止（未上传）"
        ),
        "problems": [
            f"请拆成多份计划（每份 ≤{PLATFORM_BULK_LIMIT} 个数据表）分别执行，"
            "或加店铺/站点/SKU 筛选缩小范围"
        ],
        "expected_datasheets": expected,
        "platform_bulk_limit": PLATFORM_BULK_LIMIT,
        "audit": str(audit),
    }


def _reusable_export(workdir: Path, plan: Any, browser: Any) -> Path | None:
    """找一份可复用的导出（同一会话内刚读过现值那份）。

    用户定的口径（2026-11-19）：一次改价只需要**两个**关键 xlsx ——
    原始 和 草稿。预览已经导过一次原始，执行时不该再导第二次。

    只接受 ``browser.last_export_path``：那是"最近一次读取现值"用的文件，
    与计划的行集一致。它的内容新鲜度由 ``_targets_unchanged`` 兜底。
    """

    candidate = getattr(browser, "last_export_path", None)
    if not candidate:
        return None
    path = Path(candidate)
    return path if path.is_file() else None


def _export_path(workdir: Path, plan: Any, browser: Any) -> Path:
    """本次执行实际使用的导出文件路径。

    **必须与 ``_export_and_locale`` 的选择一致**：它可能复用了
    ``last_export_path``，也可能自己新导了一份（``<plan_id>.export.xlsx``）。
    ``_export_and_locale`` 会把这个决定记到 ``browser.last_apply_export``，
    这里优先用它；拿不到才退回默认路径。
    """

    recorded = getattr(browser, "last_apply_export", None)
    if recorded:
        path = Path(recorded)
        if path.is_file():
            return path
    return workdir / f"{plan.plan_id}.export.xlsx"


async def _targets_unchanged(browser: Any, plan: Any, matrix: list[list[Any]]) -> list[str]:
    """核对目标行的现值是否仍等于计划里的 old。

    这是复用导出后**唯一的防线**：不重新导出全量，只用一次合并搜索
    读目标那几十行。若期间他人改过值，这里会如实报出来，而不是
    拿旧快照静默覆盖。

    ``matrix`` 保留在签名里是为了将来能对比本地快照；
    当前只做线上取值比对，故未使用。
    """

    del matrix  # 目前只用线上取值，不需要本地快照

    # 直接在本地拼查询串，避免反向 import（change 模块依赖 apply 模块）
    terms: list[str] = []
    seen: set[str] = set()
    for item in plan.items:
        core = str(item.sku or "").strip()
        if core and core not in seen:
            seen.add(core)
            terms.append(core)
    if not terms or len(terms) > MERGED_QUERY_MAX_SKUS:
        return []

    query = " ".join(f"*{sku}" for sku in terms)
    try:
        found = await browser.search(
            query, plan.site, only_listed=plan.only_listed
        )
    except Exception as exc:  # noqa: BLE001 - 校验失败不该阻断，但要留痕
        logger.warning("执行前现值校验失败，跳过：%s", exc)
        return []

    live = {
        (str(record.get("rowid") or ""), str(record.get("sku") or "")): record
        for record in (found.get("rows") or [])
    }
    drift: list[str] = []
    for item in plan.items:
        record = live.get((item.rowid, item.sku))
        if record is None:
            continue   # 读不到不等于变了，交给对账去判
        actual = _num(record.get("price" if item.field == "price" else "qty"))
        if actual is None:
            continue
        if abs(actual - item.old) > TOLERANCE:
            drift.append(
                f"数据表 {item.rowid} / {item.sku} 的 {item.field} 已从 "
                f"{item.old} 变成 {actual}，快照过期，请重新预览"
            )
    return drift


async def _export_and_locale(
    browser: DatasheetBrowser,
    plan: ChangePlan,
    workdir: Path,
    audit: Path,
) -> tuple[dict[str, Any] | None, list[tuple[str, int, Any]]]:
    """取得原始导出 → 校验店铺 → 校验目标未漂移 → 定位每一行。

    返回 ``(错误结果, 单元格改动)``。错误非 None 时调用方直接返回它。

    **优先复用预览阶段那份导出**：一次改价只需要一个"原始"xlsx。
    只有找不到可复用文件时才重新导出。安全性由 ``_targets_unchanged``
    （合并搜索只读目标行）保证，不靠重复导出。
    """

    from soldeazy_mcp_server.providers.soldeazy_workbook import read_matrix

    # **计划是"全部店铺"时必须清掉残留的 active_shop**，否则后续 `search("")`
    # 会沿用上次切过的那个店铺，导出/同步范围就比预览窄 —— 预览说改 1534 个表，
    # 实际只改了其中一个店的几十个。虽然对账的 `absent` 会暴露，但那已经是一次
    # 错误的上传。放在分支外：复用的导出也要清，否则同步那一步同样会偏。
    if not (plan.shop or {}).get("id") and getattr(browser, "active_shop", None):
        logger.info(
            "计划未指定店铺，清掉残留的 active_shop（原为 %s）以覆盖全部店铺",
            (browser.active_shop or {}).get("name"),
        )
        try:
            browser.active_shop = None  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - 清不掉也不该阻断
            logger.warning("清理 active_shop 失败：%s", exc)

    reusable = _reusable_export(workdir, plan, browser)
    if reusable is not None:
        exported = reusable
        logger.info("复用已有导出，跳过重复导出：%s", exported.name)
    else:
        exported = workdir / f"{plan.plan_id}.export.xlsx"
        await browser.search("", plan.site, only_listed=plan.only_listed)
        # 导出走 cookie/API 通道：页面通道的弹层在部分会话下不稳定
        # （2026-11-11 实机：FUN 上点导出后 #jqizoo 不出现），且 API 通道
        # 的筛选参数直接取自最近一次 search，行集与计划一致。
        await browser.export_via_api(exported)

    # 记下"这次实际用的文件"：后续 build_upload_file 必须基于同一份，
    # 不能再自己拼 plan_id.export.xlsx（复用时会指向不存在的路径）。
    try:
        browser.last_apply_export = exported  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 属性记不上不影响主流程
        logger.debug("记录 last_apply_export 失败：%s", exc)

    _sheet, matrix = read_matrix(exported)

    # 切了渠道账号还拿旧计划执行，会写进别的店铺——这里直接拦住
    guard = shop_problems(plan, matrix, getattr(browser, "active_shop", None))
    if guard:
        return (
            {
                "plan_id": plan.plan_id,
                "ok": False,
                "channel": "upload",
                "error": "渠道账号与计划不一致，已中止（未上传）",
                "problems": guard,
                "audit": str(audit),
            },
            [],
        )

    # 复用的是旧快照 → 必须确认目标行期间没被别人改过
    if reusable is not None:
        drift = await _targets_unchanged(browser, plan, matrix)
        if drift:
            return (
                {
                    "plan_id": plan.plan_id,
                    "ok": False,
                    "channel": "upload",
                    "error": "目标行的现值已与计划不符，快照过期，已中止（未上传）",
                    "problems": drift[:MAX_PROBLEMS],
                    "problems_total": len(drift),
                    "audit": str(audit),
                },
                [],
            )

    changes, missing = plan_to_cell_changes(plan, matrix)
    if missing:
        return (
            {
                "plan_id": plan.plan_id,
                "ok": False,
                "channel": "upload",
                "error": "计划里有行在导出结果中定位不到",
                "missing": missing,
                "audit": str(audit),
            },
            [],
        )

    return None, changes


async def _sync_after_upload(
    browser: DatasheetBrowser,
    plan: ChangePlan,
    expected_datasheets: int,
    *,
    sync_price: bool,
    sync_quantity: bool,
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """BULK SYNC 并核对：返回 ``(同步结果, problems, notes)``。"""

    planned_ids = sorted({item.rowid for item in plan.items})
    # 重新搜索一次：HTTP 通道会顺带拿下 sql_cache_token，同步接口必须用它。
    await browser.search("", plan.site, only_listed=plan.only_listed)

    sync_result = await browser.sync_up_via_api(
        price_postage=sync_price, quantity=sync_quantity, row_ids=planned_ids
    )
    # 同步接口会回传全部勾选行，条数一多就是上万 token——只留样本
    if isinstance(sync_result, dict) and isinstance(sync_result.get("table_row_ids"), list):
        ids = sync_result["table_row_ids"]
        sync_result["table_row_ids"] = ids[:20]
        sync_result["table_row_ids_total"] = len(ids)
        sync_result["table_row_ids_truncated"] = len(ids) > 20

    sync_problems: list[str] = []
    sync_notes: list[str] = []

    sync_queue = await _await_latest_job(
        browser,
        expected_datasheets,
        planned_ids=planned_ids,
        tolerate_mismatch=not plan.only_listed,
        attempts=(
            SYNC_JOB_ATTEMPTS_UNLISTED if not plan.only_listed else SYNC_JOB_ATTEMPTS
        ),
    )
    if sync_queue:
        sync_result["queue"] = sync_queue
        sync_problems.extend(sync_queue.get("problems") or [])
        sync_notes.extend(sync_queue.get("notes") or [])
    else:
        # 没观察到同步任务：未刊登数据表本来就不会产生同步工序（FUN 实测），
        # 记警告不判失败；已刊登计划也先记警告，避免平台行为差异造成误报。
        sync_notes.append(
            "同步接口返回成功但工序队列没观察到同步任务，"
            + (
                "未刊登数据表属预期行为，同步条数以上传条数为准"
                if not plan.only_listed
                else "同步条数无法从工序队列确认"
            )
        )

    # 工序归属判断不成立时（登录名与任务字段对不上），条数比对可能混入别人的任务。
    # 这必须让调用方看见，否则"数字不一致"会被当成我们的 bug 去查。
    ownership_warning = getattr(browser, "last_queue_ownership_warning", "")
    if ownership_warning:
        sync_notes.append(ownership_warning)

    return sync_result, sync_problems, sync_notes


def _sync_status(sync_result: dict[str, Any] | None, problems: list[str]) -> str:
    """同步判决。

    ``ok`` 必须意味着**真的核对到了同步工序**。原来只看「没有 problems」
    就返回 ``ok``：同步接口回 200、但工序队列里根本没出现任务时
    （未刊登数据表就是这样，FUN 实测），会报成 ``synced: ok``，
    让"一条都没核对"看起来像"全部同步成功"。
    这种情况返回 ``unverified``，由 notes 说明原因。
    """

    if not sync_result:
        return "skipped"
    if problems:
        return "partial"
    queue = sync_result.get("queue") or {}
    if queue.get("planned") is None:
        return "unverified"
    return "ok"


def _sync_detail(sync_result: dict[str, Any] | None) -> dict[str, Any]:
    """把同步工序的四类差集压成计数 + 样本，避免判决被几千个 id 撑爆。"""

    if not isinstance(sync_result, dict):
        return {}
    queue = sync_result.get("queue") or {}
    if queue.get("planned") is None:
        return {}
    return {
        "planned": queue.get("planned"),
        "job_rows": queue.get("job_rows"),
        "synced_count": len(queue.get("synced") or []),
        "skipped_no_listing_count": len(queue.get("skipped_no_listing") or []),
        "failed_count": len(queue.get("failed") or []),
        "not_in_job_count": len(queue.get("not_in_job") or []),
        # 每类只带前几个样本，明细在工序管理页面上
        "synced_sample": (queue.get("synced") or [])[:5],
        "skipped_no_listing_sample": (queue.get("skipped_no_listing") or [])[:5],
        "failed_sample": (queue.get("failed") or [])[:5],
        "not_in_job_sample": (queue.get("not_in_job") or [])[:10],
    }


async def apply_plan(
    browser: DatasheetBrowser,
    plan: ChangePlan,
    workdir: Path,
    *,
    sync: bool = True,
    sync_price: bool = True,
    sync_quantity: bool = False,
) -> dict[str, Any]:
    """统一走「导出 → 批量改 → 一次上传 →（可选）同步上平台」。

    上传文件只保留**计划涉及的数据表**（``keep_rows``）——实测整份导出上传时，
    平台会把文件里每一行都 revise 一遍，没打算动的行会被覆盖成导出时的值。

    上传后按工序管理核对：``total_items`` 与计划的数据表数一致，且逐行成功。
    ``sync=True`` 时再勾选同一批数据表做 BULK SYNC，并核对同步条数——
    两者数量不一致是常态（没有在线刊登的数据表同步不了），但必须报出来。

    内部按阶段拆成了 4 个辅助函数（上限检查 / 导出定位 / 同步核对 / 结果组装），
    这里只负责串流程（原来 189 行、复杂度 23）。
    """

    from soldeazy_mcp_server.providers.soldeazy_workbook import build_upload_file

    workdir.mkdir(parents=True, exist_ok=True)
    audit = workdir / f"{plan.plan_id}.audit.jsonl"
    expected_datasheets = len({item.rowid for item in plan.items})

    blocked = _too_many_datasheets(plan, audit)
    if blocked is not None:
        return blocked

    failure, changes = await _export_and_locale(browser, plan, workdir, audit)
    if failure is not None:
        return failure

    if not changes:
        return {
            "plan_id": plan.plan_id,
            "ok": True,
            "channel": "upload",
            "items": 0,
            "note": "所有行已经是目标值，无需上传",
            "audit": str(audit),
        }

    # **用 `_export_and_locale` 实际拿到的那份文件**，不要重新拼路径。
    # 它可能复用了 last_export_path（比如 state_498862.xlsx），
    # 而 plan_id.export.xlsx 在这种情况根本不存在 —— 之前就是这里崩的。
    exported = _export_path(workdir, plan, browser)
    from soldeazy_mcp_server.providers.soldeazy_workbook import read_matrix

    _sheet, matrix = read_matrix(exported)
    keep = plan_rows(plan, matrix)

    target = workdir / f"{plan.plan_id}.done.xlsx"
    report = build_upload_file(exported, target, changes, keep_rows=keep)
    upload = await browser.upload_via_api(target)
    handle = upload.get("handle")

    # 等工序任务结束再核对，避免读到"处理中"
    queue = await _await_queue(browser, handle, expected_datasheets)

    busy = await _active_jobs(browser)
    busy_note = (
        f"工序队列还有 {len(busy)} 个任务在执行中"
        f"（{', '.join(str(j.get('queue_job_idx')) for j in busy[:5])}），"
        "本次会排队，耗时会变长"
        if busy
        else ""
    )

    sync_result: dict[str, Any] | None = None
    sync_problems: list[str] = []
    sync_notes: list[str] = []
    if sync:
        sync_result, sync_problems, sync_notes = await _sync_after_upload(
            browser,
            plan,
            expected_datasheets,
            sync_price=sync_price,
            sync_quantity=sync_quantity,
        )

    append_audit(
        audit,
        plan.plan_id,
        "-",
        {
            "success": bool(queue.get("ok")),
            "applied": [{"rows": len(changes), "datasheets": expected_datasheets}],
            "warnings": (queue.get("problems") or []) + sync_problems,
        },
    )

    # 同步的逐条差集（只在算出时才有）：让"改了 N 个、同步 M 个"可解释
    sync_detail = _sync_detail(sync_result)

    return {
        "plan_id": plan.plan_id,
        "ok": bool(queue.get("ok")) and not sync_problems,
        "channel": "upload",
        "requests": 4 if sync else 2,
        "items": len(changes),
        "rows_touched": len(changes),
        "uploaded_rows": report.get("rows"),
        "expected_datasheets": expected_datasheets,
        "changed_columns": report["changed_columns"],
        "file": report["path"],
        "handle": handle,
        "queue": queue,
        "sync": _sync_status(sync_result, sync_problems),
        "sync_result": sync_result,
        "sync_problems": sync_problems,
        "sync_notes": sync_notes,
        **({"sync_detail": sync_detail} if sync_detail else {}),
        "queue_busy": busy_note,
        "audit": str(audit),
    }


def _job_finished(job: dict[str, Any]) -> bool:
    """工序任务是否已结束（没有待处理条数且写入了结束时间）。"""

    res = job.get("queue_job_res") or {}
    processing = str(res.get("processing_msgs_count", "0"))
    return processing in {"0", ""} and bool(
        str(job.get("queue_job_enddate") or "").strip()
    )


async def _wait_for_job(
    browser: DatasheetBrowser,
    job_id: str,
    *,
    attempts: int,
    wait_seconds: float,
) -> dict[str, Any]:
    """轮询工序列表直到任务结束；翻不到就提前退出（详情接口按 id 仍能查）。"""

    job: dict[str, Any] = {}
    for attempt in range(attempts):
        jobs = await browser.queue_jobs(limit=50)
        job = next((item for item in jobs if str(item.get("queue_job_idx")) == job_id), {})
        if job:
            if _job_finished(job):
                break
        elif attempt >= 2:
            # 列表里翻不到（任务太多或被清理）就别再空等，详情接口按 id 一定能查
            break
        await asyncio.sleep(wait_seconds)
    return job


def _queue_problems(
    job: dict[str, Any],
    rows: list[dict[str, Any]],
    expected_datasheets: int,
) -> list[str]:
    """把「条数不对」「有失败」「逐行没成功」归成给人看的问题清单。"""

    res = job.get("queue_job_res") or {}
    total = str(res.get("total_items", "")) or str(len(rows))
    failed = str(res.get("failure_msgs_count", "0"))

    problems: list[str] = []
    if failed not in {"0", ""}:
        problems.append(f"工序任务有 {failed} 条失败")
    if total and total != str(expected_datasheets):
        problems.append(f"工序处理了 {total} 个数据表，计划是 {expected_datasheets} 个")

    bad_rows = [row for row in rows if "successful" not in row["result"].lower()]
    if bad_rows:
        problems.append(f"{len(bad_rows)} 行结果不是 Revise Successful")
    return problems


async def _await_queue(
    browser: DatasheetBrowser,
    handle: Any,
    expected_datasheets: int,
    *,
    attempts: int = 10,
    wait_seconds: float = 6.0,
) -> dict[str, Any]:
    """轮询工序管理，确认任务完成、条数对得上、逐行成功。"""

    if handle in (None, ""):
        return {"ok": False, "problems": ["上传没有返回 handle，无法核对工序任务"]}

    job_id = str(handle)
    job = await _wait_for_job(
        browser, job_id, attempts=attempts, wait_seconds=wait_seconds
    )

    detail = await browser.queue_result(job_id)
    rows = detail.get("rows") or []

    if not job and not rows:
        return {
            "ok": False,
            "job_id": job_id,
            "problems": ["工序列表里找不到这个任务，按 id 也查不到明细"],
        }

    res = job.get("queue_job_res") or {}
    total = str(res.get("total_items", "")) or str(len(rows))
    success = str(res.get("success_msgs_count", "")) or str(
        sum(1 for row in rows if "successful" in row["result"].lower())
    )
    failed = str(res.get("failure_msgs_count", "0"))
    problems = _queue_problems(job, rows, expected_datasheets)

    return {
        "ok": not problems,
        "job_id": job_id,
        "act": job.get("queue_job_act"),
        "status": job.get("queue_job_status"),
        "created": job.get("queue_job_createdate"),
        "total_items": total,
        "success": success,
        "failed": failed,
        "expected_datasheets": expected_datasheets,
        "rowids": [row["rowid"] for row in rows][:20],
        "rowids_total": len(rows),
        "rowids_truncated": len(rows) > 20,
        "problems": problems,
    }


def _trim_ids(ids: list[str], limit: int = 20) -> dict[str, Any]:
    """回传时只带前 N 个 id。

    实战里单批最多出现过 2557 条，完整列表在返回里重复两次，
    光这一项就吃掉 1.4 万 token，而且对调用方没有价值。
    """

    sample = ids[:limit]
    return {
        "rowids": sample,
        "rowids_total": len(ids),
        "rowids_truncated": len(ids) > limit,
    }


async def _active_jobs(browser: DatasheetBrowser) -> list[dict[str, Any]]:
    """列出仍在执行中的工序任务（没有结束时间的）。"""

    try:
        jobs = await browser.queue_jobs(limit=30)
    except Exception:  # noqa: BLE001 - 查不到不算错误
        return []
    return [
        job
        for job in jobs
        if not str(job.get("queue_job_enddate") or "").strip()
    ]


async def _find_fresh_job(
    browser: DatasheetBrowser, *, attempts: int, wait_seconds: float
) -> dict[str, Any] | None:
    """等工序管理里冒出比基线更新的任务（同步接口不返回任务号）。"""

    try:
        baseline = {
            str(job.get("queue_job_idx")) for job in await browser.queue_jobs(limit=20)
        }
    except Exception as exc:  # noqa: BLE001 - 拿不到基线就不做比对
        logger.warning("读工序基线失败，改为直接取最新任务：%s", exc)
        baseline = set()

    for _ in range(attempts):
        try:
            jobs = await browser.queue_jobs(limit=20)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读工序列表失败，继续等下一轮：%s", exc)
            jobs = []
        fresh = [job for job in jobs if str(job.get("queue_job_idx")) not in baseline]
        if fresh:
            return fresh[0]
        await asyncio.sleep(wait_seconds)
    return None


async def _sync_detail_and_findings(
    browser: DatasheetBrowser,
    job: dict[str, Any],
    planned_ids: Sequence[str],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """逐条差集：这是"条数不一致"能变得可解释的唯一办法。

    返回 ``(差集明细, 问题, 提醒)``。
    """

    problems: list[str] = []
    notes: list[str] = []
    detail: dict[str, Any] = {}

    try:
        result = await browser.queue_result(str(job.get("queue_job_idx")))
    except Exception as exc:  # noqa: BLE001 - 明细拿不到就退回只比条数
        logger.warning("读同步工序明细失败，退回只比条数：%s", exc)
        return {"detail_unavailable": f"{type(exc).__name__}: {exc}"[:120]}, problems, notes

    detail = summarize_sync_shortfall(planned_ids, result.get("rows") or [])

    if detail["failed"]:
        problems.append(
            f"同步失败 {len(detail['failed'])} 个（{_sample_rows(detail['failed'])}）"
        )
    if detail["unexpected_in_job"]:
        # 工序里出现计划外的数据表：很可能抓错了任务
        notes.append(
            f"同步工序里出现 {len(detail['unexpected_in_job'])} 个"
            f"计划外的数据表（{_sample_ids(detail['unexpected_in_job'])}）"
            "——可能读到了别人的任务，条数统计仅供参考"
        )
    if detail["not_in_job"]:
        # 这条最关键：计划里有、任务里没有 —— 说明请求没覆盖到
        problems.append(
            f"{len(detail['not_in_job'])} 个数据表没有进入同步任务"
            f"（{_sample_ids(detail['not_in_job'])}）——"
            "不是失败，是这次同步没覆盖到它们"
        )
    if detail["skipped_no_listing"]:
        message = (
            f"{len(detail['skipped_no_listing'])} 个数据表没有在线刊登，"
            f"平台不处理同步（{_sample_ids(detail['skipped_no_listing'])}）"
        )
        # 有成功的说明只是部分跳过，属预期；一个都没同步上则不是"跳过"，
        # 是整体没生效。
        (notes if detail["synced"] else problems).append(message)

    return detail, problems, notes


async def _await_latest_job(
    browser: DatasheetBrowser,
    expected: int,
    *,
    planned_ids: Sequence[str] = (),
    tolerate_mismatch: bool = False,
    attempts: int = 10,
    wait_seconds: float = 6.0,
) -> dict[str, Any] | None:
    """同步接口不返回任务号，只能等工序管理里冒出新的同步任务。

    以"本轮之前见过的最新 job_idx"为基线，找比它更新的那条。

    **为什么只比条数不够**：`total_items` 只告诉你"同步了几个"，不告诉你
    "少了哪几个、为什么"。用户看到的只是"改了 100 个、同步了 96 个"，
    拿不到可行动的信息。所以拿到任务后，把 ``planned_ids`` 与工序明细
    逐条做差集，分成四类：

    - ``synced``              —— 明细里成功同步的
    - ``skipped_no_listing``  —— 平台说"没有在线刊登"，属预期（未刊登表同步不了）
    - ``failed``              —— 工序明确报失败的，附平台给的原因
    - ``not_in_job``          —— 计划里有、工序明细里**根本找不到**的
      （指向"同步请求的搜索上下文截断"这个未验证假设）
    """

    job = await _find_fresh_job(browser, attempts=attempts, wait_seconds=wait_seconds)
    if job is None:
        return None

    res = job.get("queue_job_res") or {}
    total = str(res.get("total_items", ""))
    failed = str(res.get("failure_msgs_count", "0"))
    problems: list[str] = [f"同步任务有 {failed} 条失败"] if failed not in {"0", ""} else []
    notes: list[str] = []

    detail: dict[str, Any] = {}
    if planned_ids:
        detail, shortfalls, detail_notes = await _sync_detail_and_findings(
            browser, job, planned_ids
        )
        problems.extend(shortfalls)
        notes.extend(detail_notes)

    if total and total != str(expected):
        message = (
            f"同步了 {total} 个数据表，上传的是 {expected} 个"
            "（差额通常是这些数据表没有在线刊登，同步不处理）"
        )
        # 计划本身包含未刊登数据表时，这个差额是预期内的：
        # 只记警告，不判失败，否则测试账号每次都被误判成 ok=false。
        (notes if tolerate_mismatch else problems).append(message)

    return {
        "ok": not problems,
        "job_id": str(job.get("queue_job_idx")),
        "act": job.get("queue_job_act"),
        "total_items": total,
        "success": str(res.get("success_msgs_count", "")),
        "failed": failed,
        "problems": problems,
        "notes": notes,
        **(detail or {}),
    }


def _sample_ids(ids: Sequence[str], limit: int = 5) -> str:
    """把 id 列表压成一句可读的样本，避免判决被几千个 id 撑爆。"""
    sample = list(ids)[:limit]
    suffix = f" 等 {len(ids)} 个" if len(ids) > limit else ""
    return ", ".join(sample) + suffix


def _sample_rows(rows: Sequence[dict[str, Any]], limit: int = 5) -> str:
    sample = [
        f"{row.get('rowid')}:{str(row.get('result') or '')[:40]}" for row in list(rows)[:limit]
    ]
    suffix = f" 等 {len(rows)} 个" if len(rows) > limit else ""
    return "; ".join(sample) + suffix


def summarize_sync_shortfall(
    planned_ids: Sequence[str],
    job_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """把「计划的数据表」与「同步工序明细」逐条比对，分成五类。

    这是让"改了 N 个、同步 M 个"变得可解释的核心：不问"差几个"，
    而是问"差的是哪几个、每个为什么"。

    工序明细每行形如 ``{"rowid": "4512957", "result": "Revise Successful", ...}``。
    平台对"没有在线刊登"的数据表会给出含 ``listing`` / ``刊登`` 字样的结果，
    这类是**预期内**的（未刊登表同步不了），不该算失败。

    **统计口径只针对计划内的 rowid**：工序里出现计划外的行，说明很可能抓错了
    任务（别人的同步任务），这类单独进 ``unexpected_in_job`` —— 它们是"抓错任务"
    的直接证据，但不能混进"我的行同步成功了几条"。
    """

    planned = [str(x).strip() for x in planned_ids if str(x).strip()]
    planned_set = set(planned)
    seen: dict[str, str] = {}
    for row in job_rows:
        rowid = str(row.get("rowid") or "").strip()
        if rowid:
            seen[rowid] = str(row.get("result") or "")

    synced: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for rowid, result in seen.items():
        if rowid not in planned_set:
            continue  # 计划外的行：不算我的统计数据
        lowered = result.lower()
        if "successful" in lowered or "成功" in result:
            synced.append(rowid)
        elif "listing" in lowered or "刊登" in result:
            skipped.append(rowid)
        else:
            failed.append({"rowid": rowid, "result": result[:80]})

    not_in_job = [rowid for rowid in planned if rowid not in seen]
    unexpected = sorted(rowid for rowid in seen if rowid not in planned_set)

    return {
        "synced": sorted(synced),
        "skipped_no_listing": sorted(skipped),
        "failed": sorted(failed, key=lambda item: item["rowid"]),
        "not_in_job": sorted(not_in_job),
        # 计划外出现的 rowid：通常是抓错了任务，单独列出便于识别
        "unexpected_in_job": unexpected,
        "planned": len(planned),
        "job_rows": len(seen),
    }


async def reconcile(
    browser: DatasheetBrowser,
    plan: ChangePlan,
    report_dir: Path,
    *,
    attempts: int = 3,
    wait_seconds: float = 20.0,
) -> dict[str, Any]:
    """执行后对账：轮询到稳定，再下结论。

    平台应用是异步的，读一次就判会误报。策略是：
    - 每轮重新导出实际值逐条比对；
    - 全中即停；
    - 连续两轮结果完全一致也算稳定（说明确实没生效，不再空等）。
    """

    # 对账只读**计划涉及的数据表**，不导全店。
    #
    # 用户的关键判断（2026-11-18）：「我们的方式只上传改动的部分，
    # 那就不需要全量的对账」。上传文件早就收窄到 ``keep_rows`` 了，
    # 唯独对账还在导 1636 个数据表 —— 每轮 18 秒，最多 3 轮。
    #
    # 实测（2026-11-18，FUN/US，同一进程内交替测量，排除缓存效应）：
    #
    #   rowid 3 个 ：冷 19.4s，热 **8.0s / 8.6s**（11 行 / 3 个数据表）
    #   全店       ：冷 37.6s，热 40.8s / 47.6s（7075 行 / 1636 个数据表）
    #
    # 也就是收窄读取约快 5 倍，且顺序反转后结果不变。
    # 注意别拿"冷启动那一次"当结论：先 switch_shop 再首轮会是 19~33s，
    # 那是登录态与下拉的首次成本，不是这条路本身慢。
    #
    # **不预先 search**：搜索参数已经在平台上，每轮读取只需要"重发一次
    # 同样的搜索"刷新值——``respond_only`` 路径的第 1 页就是那次重发。
    # 原来这里先 search 一次、读取里又 search 一次，等于每轮多搜一遍
    # （用户 2026-11-19 指出的浪费）。
    planned_rowids = sorted({item.rowid for item in plan.items})
    query = " ".join(planned_rowids)
    if len(query) > ROWID_QUERY_MAX_CHARS:
        # 数据表太多时查询串会过长；这时退回不按 rowid 过滤（慢但不漏）
        logger.info(
            "对账涉及 %d 个数据表，查询串 %d 字符过长，改用不按 rowid 过滤",
            len(planned_rowids),
            len(query),
        )
        query = ""

    previous: list[dict[str, Any]] | None = None
    rounds = 0
    matched = 0
    mismatches: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []

    for round_no in range(1, attempts + 1):
        rounds = round_no
        # 只读搜索响应，**不导出 xlsx**：对账每轮都要读现值，
        # 导 242KB 文件再 openpyxl 解析纯属浪费（实测 12~36 秒/轮）。
        live = await browser.read_datasheet_values(
            plan.site,
            query,
            search=False,
            only_listed=plan.only_listed,
            respond_only=True,
        )
        scope = shop_scope_problem(plan, getattr(browser, "last_export_shops", None))
        if scope:
            # 读错店铺时不能继续比对：那会把每一行都算成"查不到"
            report = {
                "plan_id": plan.plan_id,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "items": len(plan.items),
                "matched": 0,
                "mismatched": [],
                "mismatched_total": 0,
                "absent": [],
                "absent_total": 0,
                "rounds": round_no,
                "ok": False,
                "error": scope,
                "shops_in_export": getattr(browser, "last_export_shops", []) or [],
                "shop_expect": str((plan.shop or {}).get("name") or ""),
            }
            report_dir.mkdir(parents=True, exist_ok=True)
            path = report_dir / f"{plan.plan_id}.reconcile.json"
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            report["report"] = str(path)
            return report
        matched = 0
        mismatches = []
        absent = []

        for item in plan.items:
            record = live.get((item.rowid, item.sku))
            if record is None:
                absent.append({"rowid": item.rowid, "sku": item.sku, "field": item.field})
                continue
            actual = _num(record["price" if item.field == "price" else "qty"])
            if actual is None:
                absent.append({"rowid": item.rowid, "sku": item.sku, "field": item.field})
                continue
            if abs(actual - item.new) <= TOLERANCE:
                matched += 1
            else:
                mismatches.append(
                    {
                        "rowid": item.rowid,
                        "sku": item.sku,
                        "field": item.field,
                        "expected": item.new,
                        "actual": actual,
                    }
                )

        if not mismatches and not absent:
            break
        if previous is not None and previous == mismatches and not absent:
            break  # 连续两轮一样，判定为确实没生效
        previous = mismatches
        if round_no < attempts:
            await asyncio.sleep(wait_seconds)

    report = {
        "plan_id": plan.plan_id,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": len(plan.items),
        "matched": matched,
        "mismatched": mismatches[:10],
        "mismatched_total": len(mismatches),
        "absent": absent[:10],
        "absent_total": len(absent),
        "rounds": rounds,
        "ok": not mismatches and not absent,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{plan.plan_id}.reconcile.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report["report"] = str(path)
    return report


def read_manifest(
    path: str | Path,
    *,
    limit: int = 20,
    site: str = "",
    kind: str = "",
) -> list[dict[str, Any]]:
    """读**旧链路**留下的 ``soldeazy-writes.jsonl``，最近的在前。

    为什么还留着：新链路（``soldeazy_apply_plan``）只写 ``<plan_id>.audit.jsonl``，
    但停用前的写入记在这份 jsonl 里。``soldeazy_change_history`` 把两个来源合并
    返回，历史才不会在升级后突然"变空"。

    坏行（手工改坏、写一半）直接跳过，不让一条脏记录毁掉整份历史。
    """

    target = Path(path)
    if not target.is_file():
        return []

    wanted_site = (site or "").strip().upper()
    wanted_kind = (kind or "").strip().lower()
    records: list[dict[str, Any]] = []
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                # 坏行跳过，不让一条脏记录毁掉整份历史（留痕便于排查）
                logger.debug("清单 %s 有一行不是 JSON，已跳过", target.name)
                continue
            if not isinstance(record, dict):
                logger.debug("清单 %s 有一行不是对象，已跳过", target.name)
                continue
            if wanted_site and str(record.get("site", "")).upper() != wanted_site:
                continue
            if wanted_kind and str(record.get("kind", "")).lower() != wanted_kind:
                continue
            records.append(record)

    records.reverse()
    return records[: max(0, int(limit))]
