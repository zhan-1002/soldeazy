"""一步到位的改价/改库存：服务端自己校验，只回判决。

为什么要有这个工具：

原来的流程是「起草 → 确认 → 执行 → 对账」四个工具，每一步都把明细推进对话，
让 agent 读了再判断。生产账号一次几千行，agent 反复读这些明细，实测累积到
**6000 万 token**。真正的浪费不是单次返回大，而是"把校验责任推给模型"。

这里把四步合到服务端：起草、冻结、执行、对账都在内部完成，
只回一个 ``ok`` + 计数 + 问题列表（有条数上限）。明细写文件，
调用方只在 ``ok=false`` 时才需要深挖。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from soldeazy_mcp_server.errors import SoldeazyError
from soldeazy_mcp_server.providers.datasheet_browser import DatasheetBrowser
from soldeazy_mcp_server.providers.soldeazy_plan import (
    MERGED_QUERY_MAX_SKUS as MERGED_QUERY_MAX_SKUS,
)

logger = logging.getLogger("soldeazy_mcp.change")

# 单次返回的问题条数上限：出错时也不该把整份清单倒进对话
MAX_PROBLEMS = 10

# 每个基础 SKU 都可能挂着 x2 / x4 / US 好几个套装，200 个 SKU 就能把判决
# 从 70 token 顶到 2700。只回前几个 + 总数，完整清单在计划文件里。
MAX_PACKS = 10


def cap_packs(packs: Any) -> dict[str, Any]:
    """把 ``needs_confirmation`` 压成「前 MAX_PACKS 个 + 总数」；空则返回空字典。"""

    items = list((packs or {}).items())
    if not items:
        return {}
    return {
        "needs_confirmation": dict(items[:MAX_PACKS]),
        "needs_confirmation_total": len(items),
    }


def _collect_execution_issues(
    applied: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """从执行结果里抽出 (problems, warnings)。

    预期内的差额（未刊登数据表同步不了）进 warnings，不影响判决；
    真正的失败进 problems。
    """

    problems: list[str] = []
    warnings: list[str] = []
    if applied is None:
        return problems, warnings

    if not applied.get("ok"):
        problems.append(str(applied.get("error") or "执行未通过"))
        problems.extend(str(item) for item in (applied.get("problems") or []))
        for item in applied.get("missing") or []:
            problems.append(f"定位不到：{item.get('rowid')}/{item.get('sku')}")

    queue = applied.get("queue") or {}
    if queue and not queue.get("ok"):
        problems.extend(str(p) for p in queue.get("problems") or [])

    problems.extend(str(p) for p in applied.get("sync_problems") or [])
    warnings.extend(str(n) for n in applied.get("sync_notes") or [])

    if applied.get("queue_busy"):
        problems.append(str(applied["queue_busy"]))

    return problems, warnings


def _collect_reconcile_issues(reconcile: dict[str, Any] | None) -> list[str]:
    """从对账结果里抽出问题。

    ``error`` 表示"根本没比对"（比如读错了店铺），不能报成"0 行不一致"。
    """

    if reconcile is None or reconcile.get("ok"):
        return []

    if reconcile.get("error"):
        return [str(reconcile["error"])]

    missed = reconcile.get("mismatched_total", len(reconcile.get("mismatched") or []))
    gone = reconcile.get("absent_total", len(reconcile.get("absent") or []))
    return [f"对账不一致 {missed} 行、查不到 {gone} 行"]


def _summarize_plan(plan: Any) -> dict[str, Any]:
    """计划自身的规模信息（与执行结果无关，先算好备用）。"""
    items = list(getattr(plan, "items", []) or [])
    return {
        "kind": getattr(plan, "kind", ""),
        "plan_id": getattr(plan, "plan_id", ""),
        "items": len(items),
        "datasheets": len({item.rowid for item in items}),
    }


def _verdict(
    applied: dict[str, Any] | None, reconcile: dict[str, Any] | None
) -> tuple[bool, str]:
    """算 ``(是否通过, 停在哪一步)``。"""

    ok = applied is not None and bool(applied.get("ok")) and bool(
        (reconcile or {}).get("ok")
    )
    if ok:
        return True, "done"
    if reconcile is not None:
        return False, "reconcile"
    return False, "apply" if applied is not None else "plan"


def _reconciled_text(reconcile: dict[str, Any] | None) -> str:
    if not reconcile:
        return "skipped"
    return f"{reconcile.get('matched', 0)}/{reconcile.get('items', 0)}"


def _attach_extra(report: dict[str, Any], extra: dict[str, Any] | None) -> None:
    """把「没匹配上的目标」这类必须知情的信息挂到判决上。

    压缩返回不能把它们吞掉——``missing`` 为空时是"全部命中"，
    非空时是"这些没做"，两者的可信度完全不同。
    """

    if not extra:
        return
    for key in ("missing", "unmatched_targets", "plan_sha256"):
        if extra.get(key):
            report[key] = extra[key]
    report.update(cap_packs(extra.get("needs_confirmation")))


def compact_plan_report(
    plan: Any,
    *,
    applied: dict[str, Any] | None,
    reconcile: dict[str, Any] | None,
    verbose: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把四步结果压成一个判决。

    ``kind`` 从 ``plan.kind`` 读，不再由调用方传——它是计划自身的属性，
    多传一个参数只会让两处不一致时无从判断该信哪个。

    ``extra`` 用来带「没匹配上的目标」这类必须让调用方知道的信息——
    压缩返回不能把它们吞掉。
    ``verbose=True`` 时才附带逐条明细——给确实要排查的场景用。

    问题收集拆成了 ``_collect_execution_issues`` / ``_collect_reconcile_issues``，
    判决与字段拼装又拆成 ``_verdict`` / ``_reconciled_text`` / ``_attach_extra``，
    这里只做拼装，不再混着分支判断（原来复杂度 44）。
    """

    problems, warnings = _collect_execution_issues(applied)
    problems.extend(_collect_reconcile_issues(reconcile))
    ok, stage = _verdict(applied, reconcile)

    report: dict[str, Any] = {
        **_summarize_plan(plan),
        "ok": ok,
        "uploaded": bool(applied and applied.get("ok")),
        "synced": (applied or {}).get("sync") or "skipped",
        "reconciled": _reconciled_text(reconcile),
        "stage": stage,
        "problems": problems[:MAX_PROBLEMS],
        "problems_total": len(problems),
    }

    if warnings:
        report["warnings"] = warnings[:MAX_PROBLEMS]
        report["warnings_total"] = len(warnings)

    _attach_extra(report, extra)

    if verbose:
        report["detail"] = {
            "apply": applied,
            "reconcile": reconcile,
            "items": [
                item.as_dict() for item in (getattr(plan, "items", []) or [])[:50]
            ],
        }
    else:
        report["hint"] = (
            "通过，无需再查明细。"
            if ok
            else "未通过。先看 problems；需要逐条核对再传 verbose=true（较贵）。"
        )
    return report


#: 预览摘要里每张表格最多保留多少行（会话里要显示，不能无限长）
PREVIEW_TABLE_ROWS = 20

#: 预览摘要里最多列几个账号 / 站点（多了会被截断并给计数）
PREVIEW_SCOPE_ITEMS = 8

#: 「方向单一」提醒的触发门槛：少于这么多条不提醒。
#:
#: 改 1-2 条价格本来就该是单方向（很常见），提醒它只会变噪音。
#: 而 3 条以上**同一方向**、且跨多个数据表，才像"参数写错"的形态。
DIRECTION_ALERT_MIN_ROWS = 3


def _shop_matches(expected: str, name: str) -> bool:
    """店铺名比对：导出列写短名（ACME），页面/搜索结果写长名（EBAY ACME）。

    与 ``soldeazy_apply._shop_name_matches`` 同一口径——两处必须在
    "什么算同一个店铺"上一致，否则预览说匹配、执行说中止。
    """

    if not expected or not name:
        return False
    return name == expected or expected in name or name in expected


def _direction_text(pairs: list[tuple[float, float]]) -> str:
    """``涨 N / 降 M`` 这种方向摘要。"""

    up = sum(1 for old, new in pairs if new > old)
    down = sum(1 for old, new in pairs if new < old)
    if up and down:
        return f"涨 {up} / 降 {down}"
    return f"涨 {up}" if up else (f"降 {down}" if down else "无变化")


def _build_overview(price_items: list[Any], qty_items: list[Any]) -> list[dict[str, Any]]:
    """表 1：按字段 × 动作聚合的改动概览。"""

    rows = []
    for label, items in (("价格（F 列）", price_items), ("数量（E 列）", qty_items)):
        rows.append(
            {
                "字段": label,
                "条数": len(items),
                "涉及数据表": len({item.rowid for item in items}),
                "方向": _direction_text([(item.old, item.new) for item in items]),
            }
        )
    return rows


def _build_scope(
    shop: dict[str, str],
    site: str,
    only_listed: bool,
    shops_in_export: list[str] | None,
) -> tuple[dict[str, list[str]], bool]:
    """表 2：作用范围，同时给出「参数侧」和「事实侧」。

    两者不是一回事：**用户传了什么** ≠ **导出里实际是什么**。
    历史上真实踩过——用户以为在改 ACME，实际导出里全是 EBAY GLOB
    （没选店铺 = 全部店铺，第一页恰好全是别的店）。
    事实侧来自导出文件第 2 列（``last_export_shops``），不需要额外请求。
    """

    param_shop = str(shop.get("name", "")).strip()
    scope: dict[str, list[str]] = {
        "账号": [param_shop] if param_shop else ["（未指定 = 全部账号）"],
        "站点": [site.strip()] if (site or "").strip() else ["（未指定 = 全部站点）"],
        "状态筛选": ["已刊登(所有)" if only_listed else "不限（含未刊登）"],
    }
    if shops_in_export:
        scope["导出里的账号"] = list(shops_in_export)

    truncated = False
    for key, values in scope.items():
        if len(values) > PREVIEW_SCOPE_ITEMS:
            scope[key] = values[:PREVIEW_SCOPE_ITEMS]
            truncated = True
    return scope, truncated


def _build_sample(items: list[Any]) -> list[dict[str, Any]]:
    """表 3：逐条改动样本（只放前 N 条，明细在 review_after.xlsx 里）。"""

    return [
        {
            "数据表": item.rowid,
            "SKU": item.sku,
            "字段": "价格" if item.field == "price" else "数量",
            "原值": item.old,
            "新值": item.new,
        }
        for item in items[:PREVIEW_TABLE_ROWS]
    ]


def _pack_confirmation(
    needs_confirmation: dict[str, list[str]],
) -> str | None:
    """套装/变体提醒：同母体下没被点名的子体不会改动。"""

    if not needs_confirmation:
        return None
    total = sum(len(value) for value in needs_confirmation.values())
    preview = "; ".join(
        f"{base} 还有 {', '.join(sibs)}"
        for base, sibs in list(needs_confirmation.items())[:3]
    )
    more = "（还有更多，见 plan 文件）" if len(needs_confirmation) > 3 else ""
    return f"**套装/变体**：{total} 个同母体变体没有被点名，不会改动。{preview}{more}"


def _missing_confirmation(missing: list[str]) -> str | None:
    if not missing:
        return None
    return (
        f"**线上找不到**：{len(missing)} 个目标没有匹配到行"
        f"（{', '.join(missing[:5])}"
        + (f" 等 {len(missing)} 个" if len(missing) > 5 else "")
        + "）"
    )


def _direction_alerts(price_items: list[Any], qty_items: list[Any]) -> list[str]:
    """改动方向单一且面很广 —— 这是"参数写错"的典型形态。

    实测教训（2026-11-18）：拿 0.01 当测试值跑预览，10 个数据表**全部降价**，
    而判决里只有"作用范围"和"文件在哪"两句提醒，没有任何一句提示
    "你正在把 10 个数据表全部降价"——真正该被看见的信号反而藏在概览表里。

    只在「单一方向 + 涉及多个数据表」时才提醒，避免正常的小改动被噪音淹没。
    """

    alerts: list[str] = []
    for label, items in (("价格", price_items), ("数量", qty_items)):
        pairs = [(item.old, item.new) for item in items]
        if len(pairs) < DIRECTION_ALERT_MIN_ROWS:
            continue
        up = sum(1 for old, new in pairs if new > old)
        down = sum(1 for old, new in pairs if new < old)
        if up and down:
            continue                       # 有涨有降：正常，不提醒
        if not up and not down:
            continue
        direction = "全部上调" if up else "全部下调"
        datasheets = len({item.rowid for item in items})
        ratios = [new / old for old, new in pairs if old]
        extreme = ""
        if ratios:
            low, high = min(ratios), max(ratios)
            if high <= 0.2:
                extreme = f"（最低到原价的 {low * 100:.0f}%）"
            elif low >= 5:
                extreme = f"（最高到原价的 {high * 100:.0f}%）"
        alerts.append(
            f"**⚠ 改动方向单一**：{label} {len(pairs)} 条覆盖 {datasheets} 个数据表，"
            f"{direction}{extreme}。如果这不是预期，多半是参数写错了（测试值、"
            "小数点位置、或漏了筛选条件），请先核对再执行。"
        )
    return alerts


def _scope_mismatch(
    shop: dict[str, str], shops_in_export: list[str] | None
) -> tuple[bool, str | None]:
    """参数范围与导出事实是否吻合：这是"改错店铺"的直接判别依据。"""

    param_shop = str(shop.get("name", "")).strip()
    mismatched = bool(
        param_shop
        and shops_in_export
        and not any(_shop_matches(param_shop, name) for name in shops_in_export or [])
    )
    if not mismatched:
        return False, None
    listed = ", ".join((shops_in_export or [])[:3])
    ellipsis = "…" if len(shops_in_export or []) > 3 else ""
    return True, (
        f"**⚠ 范围不符**：参数指定账号 {param_shop}，"
        f"但导出里实际是 {listed}{ellipsis}。"
        "改动会落到导出里的这些账号，请先修正店铺参数再重跑。"
    )


def build_preview_summary(
    plan: Any,
    *,
    shop: dict[str, str],
    site: str,
    only_listed: bool,
    needs_confirmation: dict[str, list[str]],
    missing: list[str],
    shops_in_export: list[str] | None = None,
) -> dict[str, Any]:
    """给会话渲染用的预览摘要：几张可直接显示成表格的小表 + 必须人工确认项。

    为什么要有这个（2026-11-18 用户要求）：

    - 预览阶段的返回**要能直接喂给会话并渲染成表格**，而不是一串计数让人猜；
    - 有些东西**必须显式提醒人**才能确认：套装/变体、本次涉及的账号与站点；
    - 但这些内容**多了就要截断**，否则几千行的明细会把会话撑爆。

    所以这里的原则是：**表格只放可读的摘要，明细永远在磁盘文件里**。
    每张表都有 ``truncated`` 标记和总数，调用方知道"还有更多"。

    ``shops_in_export`` 是导出文件里**实际出现**的账号（``last_export_shops``）。
    有了它，"作用范围"就能同时给出「参数侧」和「事实侧」，并在两者不吻合时
    明确提示——那是"改错店铺"的直接信号，比任何事后对账都早。

    主体已拆成 6 个小函数（概览 / 范围 / 样本 / 套装 / 找不到 / 方向告警）。
    拆之前这里是 186 行、圈复杂度 58，改一个提醒的措辞都得先读懂整段。
    """

    items = list(getattr(plan, "items", []) or [])
    price_items = [item for item in items if item.field == "price"]
    qty_items = [item for item in items if item.field == "qty"]

    scope, scope_truncated = _build_scope(shop, site, only_listed, shops_in_export)
    mismatched, mismatch_note = _scope_mismatch(shop, shops_in_export)

    confirmations = [
        note
        for note in (
            _pack_confirmation(needs_confirmation),
            _missing_confirmation(missing),
        )
        if note
    ]
    confirmations.extend(_direction_alerts(price_items, qty_items))
    if mismatch_note:
        confirmations.append(mismatch_note)
    confirmations.append(
        f"**作用范围**：账号 {scope['账号'][0]}、站点 {scope['站点'][0]}、"
        f"{scope['状态筛选'][0]}。范围不对请先改参数再重跑。"
    )
    confirmations.append(
        "**逐行明细在磁盘文件里**：请打开 review_after.xlsx 核对每一行的新旧值。"
    )

    return {
        "preview": {
            "概览": _build_overview(price_items, qty_items),
            "作用范围": scope,
            "逐条样本": _build_sample(items),
        },
        "preview_truncated": {
            "逐条样本": len(items) > PREVIEW_TABLE_ROWS,
            "逐条样本总数": len(items),
            "作用范围": scope_truncated,
        },
        # 参数指定的店铺 vs 导出里实际的店铺不一致 —— 这是"改错店铺"的直接信号。
        # 单独给一个布尔标记，方便调用方（或会话）不读长文本也能判断。
        "scope_mismatch": mismatched,
        "must_confirm": confirmations,
    }


#: 合并搜索里最多放多少个 SKU（定义在 plan 模块，两个 provider 共用）。
#: 这里只是把名字再导出一次，方便调用方从 change 模块取用。


def merged_query(
    prices: dict[str, float] | None, quantities: dict[str, float] | None
) -> str:
    """把要改的目标拼成合并搜索串；不适合合并时返回空串（= 不按 SKU 过滤）。

    **每个词前必须带 `*`**：平台按通配匹配，不带 `*` 会静默漏数据表
    （2026-11-12 生产事故，`QPCH0003` 在合并导出里没有、单查能查到）。

    母体 SKU 也可以直接放进去：搜索**按数据表匹配、命中后返回整表全部行**，
    所以搜母体名能带出它旗下所有子体（实测 ``*PARENT-SKU-A`` → 1 表 / 5 行全在）。

    超过 ``MERGED_QUERY_MAX_SKUS`` 时返回空串，退回"不按 SKU 过滤"，
    行为与改造前一致 —— 大规模改动不会因为查询串过长而漏数据表。
    """

    wanted: list[str] = []
    seen: set[str] = set()
    for source in (prices, quantities):
        for raw in (source or {}):
            core = str(raw or "").strip()
            if not core or core in seen:
                continue
            seen.add(core)
            wanted.append(core)
            if len(wanted) > MERGED_QUERY_MAX_SKUS:
                logger.info(
                    "目标 SKU 超过 %d 个，退回不按 SKU 过滤（导全店）",
                    MERGED_QUERY_MAX_SKUS,
                )
                return ""

    return " ".join(f"*{sku}" for sku in wanted)


async def _review_files(
    browser: DatasheetBrowser,
    plan: Any,
    plan_dir: Path,
) -> dict[str, Any]:
    """生成人工核对用的变更前/后两份文件。

    直接用 ``read_datasheet_values`` 那次导出的**同一份文件**和**同一份变更列表**
    （``plan_to_cell_changes``），保证"人看到的 = 会上传的"，不存在第二套口径。

    核对文件是辅助产物：生成失败不阻断主流程，但要如实说明原因——
    否则人以为自己在核一份真实文件，实际什么都没生成。
    """

    exported = getattr(browser, "last_export_path", None)
    if not exported or not Path(exported).is_file():
        return {"review_unavailable": "没有可用的导出文件，无法生成核对文件"}

    try:
        from soldeazy_mcp_server.providers.soldeazy_apply import (
            plan_rows,
            plan_to_cell_changes,
        )
        from soldeazy_mcp_server.providers.soldeazy_review import build_review_files
        from soldeazy_mcp_server.providers.soldeazy_workbook import read_matrix

        _sheet, matrix = read_matrix(exported)
        changes, missing = plan_to_cell_changes(plan, matrix)
        if missing:
            return {
                "review_unavailable": (
                    f"{len(missing)} 个计划条目在导出表里定位不到，核对文件未生成"
                )
            }

        files = build_review_files(
            exported,
            plan_dir,
            plan.plan_id,
            changes,
            keep_rows=plan_rows(plan, matrix),
        )
        return files.as_dict()
    except Exception as exc:  # noqa: BLE001 - 辅助产物，不能挡住主流程
        logger.warning("生成核对文件失败：%s", exc)
        return {"review_unavailable": f"{type(exc).__name__}: {exc}"[:160]}


def _match_items(
    live: dict[tuple[str, str], dict[str, Any]],
    prices: dict[str, float] | None,
    quantities: dict[str, float] | None,
    build_items: Any,
) -> tuple[list[Any], dict[str, Any]]:
    """两路各自匹配再合并条目：价格按子体、数量按母体。"""

    items: list[Any] = []
    report: dict[str, Any] = {"missing": [], "needs_confirmation": {}}
    for wanted, build_kind in ((prices, "price"), (quantities, "qty")):
        if not wanted:
            continue
        part, part_report = build_items(live, build_kind, wanted)
        items = [*items, *part]
        report = {
            "missing": [*report["missing"], *part_report.get("missing", [])],
            "needs_confirmation": {
                **report["needs_confirmation"],
                **part_report.get("needs_confirmation", {}),
            },
        }
    return items, report


def _nothing_matched(missing: list[str]) -> dict[str, Any]:
    """一个都没匹配上。

    不能报"已经是目标值"——那是把"没找到"说成"不用改"。
    """

    if not missing:
        return {
            "ok": True,
            "plan_id": "",
            "items": 0,
            "note": "所有行已经是目标值，未生成计划",
            "missing": [],
            "problems": [],
        }
    return {
        "ok": False,
        "plan_id": "",
        "items": 0,
        "missing": missing[:MAX_PROBLEMS],
        "missing_total": len(missing),
        "problems": [f"{len(missing)} 个目标在线上找不到对应行"],
        "problems_total": 1,
        "hint": (
            "检查 SKU 写法；如果生产账号里这些是未刊登，"
            "需要 include_unlisted=true 才会被查到。"
            "如果只有套装，请显式写 'SKU x2' 才会改套装。"
        ),
    }


def _plan_kind_for(items: list[Any]) -> str:
    """计划里的 kind 只是给历史/审计看的标签，执行与对账逐条按 item.field 走。"""

    fields = {item.field for item in items}
    if {"price", "qty"} <= fields:
        return "price_qty"
    return "price" if "price" in fields else "qty"


async def _build_preview(
    browser: Any,
    plan: Any,
    *,
    site: str,
    picked: dict[str, str],
    only_listed: bool,
    items: list[Any],
    plan_kind: str,
    build_report: dict[str, Any],
    plan_dir: Path,
    write_draft: Any,
) -> dict[str, Any]:
    """预览阶段：落盘 + 生成两份人工核对文件 + 表格摘要。

    早期版本"预览不落盘"，结果人想看全量只能靠 verbose 撑爆 token；
    现在核对文件是真实产物，打开就能逐行看。
    """

    path = write_draft(plan_dir, plan)
    review = await _review_files(browser, plan, plan_dir)

    # 会话渲染用的表格摘要 + 必须人工确认项（多了会截断，明细在文件里）
    summary = build_preview_summary(
        plan,
        shop=picked,
        site=site,
        only_listed=only_listed,
        needs_confirmation=build_report.get("needs_confirmation") or {},
        missing=build_report.get("missing") or [],
        # 导出里实际出现哪些账号：免费信息（导出第 2 列就是账号短名），
        # 用来对照"参数指定了什么"与"实际读到了什么"
        shops_in_export=list(getattr(browser, "last_export_shops", None) or []),
    )

    preview = {
        "ok": True,
        "dry_run": True,
        "kind": plan_kind,
        "plan_id": plan.plan_id,
        "plan_path": str(path),
        "items": len(items),
        "datasheets": len({item.rowid for item in items}),
        "price_items": sum(1 for item in items if item.field == "price"),
        "qty_items": sum(1 for item in items if item.field == "qty"),
        "only_listed": only_listed,
        # 人工核对就靠这两个文件：变更前 / 变更后，逐行可读
        **review,
        # ↓ 这三块是给人看的：会话把它们渲染成表格和提醒
        **summary,
        "hint": (
            "核对上面三张表（概览 / 作用范围 / 逐条样本），"
            "确认 must_confirm 里的提醒，再打开 review_after 逐行看；"
            "确认无误后传 dry_run=false 执行。"
        ),
    }
    preview.update(cap_packs(build_report.get("needs_confirmation")))
    return preview


def _freeze_and_verify(
    plan: Any, plan_dir: Path, freeze: Any, load_verified: Any
) -> tuple[Any, str]:
    """冻结计划并回读校验哈希。

    freeze() 已经算过一次哈希；回读算出的必须一致，否则说明落盘内容与
    冻结时不是同一份（写盘被截断、磁盘缓存、并发改文件）。这种偏差绝不能
    带着往下执行——上传一旦发生就没有撤销按钮。
    """

    write_draft_result = freeze(plan_dir, plan)
    verified_plan, digest = load_verified(plan_dir, plan.plan_id)
    frozen_sha = (write_draft_result or {}).get("sha256")
    if frozen_sha and digest != frozen_sha:
        raise SoldeazyError(
            "SOLDEAZY_PLAN_UNSTABLE",
            "计划冻结后的哈希与回读不一致，拒绝执行（文件可能在落盘时被改动）",
            details={"frozen": frozen_sha, "verified": digest},
        )
    return verified_plan, digest


def _carry_export_warnings(browser: Any, applied: dict[str, Any]) -> dict[str, Any]:
    """把导出阶段的"无法判断是否截断"留痕透传到判决里。

    总数恰好等于上限时不阻断，但调用方必须知道这次结果的可信度打了折。
    """

    warnings = list(getattr(browser, "last_export_warnings", None) or [])
    if warnings:
        applied["sync_notes"] = [*(applied.get("sync_notes") or []), *warnings]
    return applied


def _ranges(total: int, limit: int) -> list[tuple[int, int]]:
    """把 1..total 切成每段不超过 ``limit`` 的连续区间。"""

    out: list[tuple[int, int]] = []
    start = 1
    while start <= total:
        out.append((start, min(start + limit - 1, total)))
        start += limit
    return out


def _split_plan(plan: Any, make_plan: Any) -> list[Any]:
    """把计划按数据表切成每份 ≤ 上限的子计划。

    **切段的单位是数据表，不是条目**：同一个数据表的母体行 + 全部子体行
    必须落在同一批里，否则上传文件会缺结构（只传子体行平台认不出归属）。

    不需要分批时返回只含原计划的列表——调用方据此走原来的单批路径，
    单批行为一个字节都不变。
    """

    from soldeazy_mcp_server.providers.soldeazy import PLATFORM_BULK_LIMIT

    grouped: dict[str, list[Any]] = {}
    for item in plan.items:
        grouped.setdefault(str(item.rowid), []).append(item)

    rowids = list(grouped)
    if len(rowids) <= PLATFORM_BULK_LIMIT:
        return [plan]

    batches: list[Any] = []
    for index, (start, _end) in enumerate(_ranges(len(rowids), PLATFORM_BULK_LIMIT), 1):
        items: list[Any] = []
        for rowid in rowids[start - 1 : start - 1 + PLATFORM_BULK_LIMIT]:
            items.extend(grouped[rowid])
        batches.append(
            make_plan(
                f"{plan.kind}-b{index}",
                plan.site,
                plan.shop,
                items,
                only_listed=plan.only_listed,
            )
        )
    logger.info(
        "计划 %s 涉及 %d 个数据表，超过单批上限 %d → 拆成 %d 批",
        plan.plan_id,
        len(rowids),
        PLATFORM_BULK_LIMIT,
        len(batches),
    )
    return batches


def _applied_summary(batch: Any, applied: dict[str, Any]) -> dict[str, Any]:
    """一批的上传结果摘要（进判决的 applied 段）。"""

    return {
        "plan_id": getattr(batch, "plan_id", ""),
        "ok": applied.get("ok"),
        "items": applied.get("items"),
        "datasheets": applied.get("expected_datasheets"),
        "sync": applied.get("sync"),
        "problems": (applied.get("problems") or [])[:MAX_PROBLEMS],
    }


def _apply_failure(index: int, batch: Any, applied: dict[str, Any]) -> dict[str, Any]:
    """某批上传失败时的逐批记录。"""

    problems = applied.get("problems") or []
    if not problems and applied.get("error"):
        problems = [applied["error"]]
    return {
        "batch": index,
        "plan_id": getattr(batch, "plan_id", ""),
        "ok": False,
        "problems": problems,
    }


def _recon_summary(index: int, batch: Any, recon: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch": index,
        "plan_id": getattr(batch, "plan_id", ""),
        "ok": bool(recon.get("ok")),
        "matched": recon.get("matched"),
        "items": recon.get("items"),
        "mismatched_total": recon.get("mismatched_total"),
        "absent_total": recon.get("absent_total"),
    }


def _batch_problems(results: list[dict[str, Any]], total_batches: int) -> list[str]:
    """把逐批结果归成给人看的问题清单。"""

    problems: list[str] = []
    failed = [row for row in results if not row.get("ok")]
    if failed:
        first = failed[0]
        problems.append(
            f"第 {first['batch']}/{total_batches} 批未通过"
            f"（计划 {first.get('plan_id') or '-'}）"
        )
        problems.extend((first.get("problems") or [])[:MAX_PROBLEMS])
    if len(results) < total_batches:
        problems.append(
            f"已在第 {len(results)} 批后停止，还有 {total_batches - len(results)} 批未执行"
        )
    return problems


async def _run_batches(
    browser: Any,
    plan: Any,
    batches: list[Any],
    *,
    plan_dir: Path,
    price: bool,
    qty: bool,
    apply_plan: Any,
    reconcile: Any,
    extra: dict[str, Any],
    verbose: bool,
) -> dict[str, Any]:
    """逐批执行，把判决汇总成一份。

    原则：**每批都是原来那一次完整动作**（导出→改→上传→同步→对账），
    只是连续做了几次。不引入跨批状态机，也不改任何一批的语义。

    某批失败就停下来：此前成功的批次已经在线上生效（不该回滚，回滚本身
    又是一次写入），剩下的批次继续跑只会扩大失败面、让判决更难解释。
    所以如实报告"第几批失败、还剩几批没做"。
    """

    results: list[dict[str, Any]] = []
    applied_all: dict[str, Any] = {}

    for index, batch in enumerate(batches, 1):
        applied = await apply_plan(
            browser,
            batch,
            plan_dir,
            sync=True,
            sync_price=price,
            sync_quantity=qty,
        )
        applied = _carry_export_warnings(browser, applied)
        applied_all[f"batch{index}"] = _applied_summary(batch, applied)

        if not applied.get("ok"):
            results.append(_apply_failure(index, batch, applied))
            break

        recon = await reconcile(browser, batch, plan_dir)
        results.append(_recon_summary(index, batch, recon))
        if not recon.get("ok"):
            break

    done = [row for row in results if row.get("ok")]
    failed = [row for row in results if not row.get("ok")]
    ran = len(results)
    problems = _batch_problems(results, len(batches))

    plan_items = sum(len(getattr(batch, "items", []) or []) for batch in batches)
    total_matched = sum(int(row.get("matched") or 0) for row in done)
    ok = not failed and ran == len(batches)

    return {
        "ok": ok,
        "kind": plan.kind,
        "site": plan.site,
        "shop": (plan.shop or {}).get("name", ""),
        "items": plan_items,
        "uploaded": all(
            bool(applied_all.get(f"batch{i}", {}).get("ok")) for i in range(1, ran + 1)
        ),
        "datasheets": sum(
            int(applied_all.get(f"batch{i}", {}).get("datasheets") or 0)
            for i in range(1, ran + 1)
        ),
        "batches": len(batches),
        "batches_done": len(done),
        "batches_failed": len(failed),
        "reconciled": f"{total_matched}/{plan_items}",
        "stage": "done" if ok else "batch",
        "problems": problems[:MAX_PROBLEMS],
        "problems_total": len(problems),
        "batch_detail": results,
        "hint": (
            "多批执行：每批都是一次完整的导出→改→上传→同步→对账。"
            "batch_detail 是逐批结果；失败批次之前已成功的改动在线上生效，不回滚。"
        ),
        **extra,
        **({"apply": applied_all} if verbose else {}),
    }


async def run_change(
    *,
    site: str,
    prices: dict[str, float] | None = None,
    quantities: dict[str, float] | None = None,
    shop: str,
    dry_run: bool,
    verbose: bool,
    only_listed: bool = True,
    deps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次授权的完整执行：起草 → 冻结 → 执行 → 对账，只回判决。

    **价格和数量是同一次授权的两个结果**，不是两条链路：

    - ``prices``     = ``{子体SKU: 新价格}``（按子体精确匹配，套装需显式 ``xN``）
    - ``quantities`` = ``{母体SKU: 新数量}``（按母体展开全部子体行）

    两者可以只给一个，也可以都给。**合并成一份计划、一次导出、一次上传、
    一次同步**——因为上传本来就要整行覆盖（E 列数量 + F 列价格同时写），
    而线上生效只在同步那一步，靠勾选「价格及邮费」/「刊登数量」。

    所以这里不再有 ``kind``：那个参数把"内部怎么匹配"泄漏成了对外概念，
    逼调用方先想清楚"这是改价还是改库存"，反而增加了隔阂。
    调用方只需要说"这些 SKU 价格改成这些、这些母体库存改成这些"。

    两阶段（``dry_run`` 控制）：

    1. **预览**：生成两份可读文件 ``review_before`` / ``review_after``，
       人打开逐行核对。返回值只给计数，明细在文件里。
    2. **执行**：``dry_run=false``。

    人工核对之所以能被落实，不是因为多了一道令牌，而是因为**明细只在文件里**：
    预览返回的计数不足以判断改得对不对，要判断就必须打开 review_after。
    AI 同理——它读计数、人读文件，两边不重复劳动，也不烧 token。

    主体按阶段拆成 ``_match_items`` / ``_build_preview`` / ``_freeze_and_verify``。
    拆之前这里是 206 行、圈复杂度 28，四个阶段的局部变量挤在同一个作用域里。
    """

    build_items = deps["build_items"]
    make_plan = deps["make_plan"]
    write_draft = deps["write_draft"]
    freeze = deps["freeze"]
    apply_plan = deps["apply_plan"]
    reconcile = deps["reconcile"]
    plan_dir = deps["plan_dir"]
    browser = deps["browser"]
    resolve_shop = deps["resolve_shop"]
    load_verified = deps["load_verified"]

    if not prices and not quantities:
        raise SoldeazyError(
            "SOLDEAZY_PLAN_EMPTY",
            "prices 与 quantities 至少要给一个。",
        )

    picked = await resolve_shop(shop)

    # **合并搜索限定范围，再导出这个子集**（2026-11-19 实测定案）。
    #
    # 实测（ACME/US，目标 SKU 散落在 33 个数据表）：
    #   A 空查询 + 导全店     36.45s   1636 表 / 7075 行 / 470KB
    #   C 合并搜索 + 导子集    5.39s     33 表 /  370 行 /  19KB
    # 且 C 的 price/qty 与 A **逐条一致（370 条 0 差异）**。
    #
    # 为什么这条路成立（此前的错误结论已作废，见 TODO.md「术语定义」）：
    # - 搜索**按数据表匹配，命中后返回整张表的全部行**，所以搜母体名
    #   就能带出它旗下的全部子体（实测：*PARENT-SKU-A → 1 表 / 5 行全在）。
    # - 于是 prices 里的子体 SKU 和 quantities 里的母体 SKU 都直接拼进查询即可。
    # - 导出接口的筛选条件取自最近一次 search，所以导出必然是同一子集。
    query = merged_query(prices, quantities)
    live = await browser.read_datasheet_values(
        site, query, only_listed=only_listed
    )

    items, build_report = _match_items(live, prices, quantities, build_items)
    if not items:
        return _nothing_matched(build_report.get("missing") or [])

    plan_kind = _plan_kind_for(items)
    plan = make_plan(plan_kind, site, picked, items, only_listed=only_listed)

    if dry_run:
        return await _build_preview(
            browser,
            plan,
            site=site,
            picked=picked,
            only_listed=only_listed,
            items=items,
            plan_kind=plan_kind,
            build_report=build_report,
            plan_dir=plan_dir,
            write_draft=write_draft,
        )

    write_draft(plan_dir, plan)
    plan, digest = _freeze_and_verify(plan, plan_dir, freeze, load_verified)

    # 同步键由计划内容决定，不传开关：改了什么就推什么，不漏也不多推
    fields = {item.field for item in items}
    extra = {
        "missing": (build_report.get("missing") or [])[:MAX_PROBLEMS],
        "unmatched_targets": len(build_report.get("missing") or []),
        "needs_confirmation": build_report.get("needs_confirmation") or {},
        "plan_sha256": digest,
    }
    batches = _split_plan(plan, make_plan)

    if len(batches) <= 1:
        applied = await apply_plan(
            browser,
            plan,
            plan_dir,
            sync=True,
            sync_price="price" in fields,
            sync_quantity="qty" in fields,
        )
        reconcile_result = await reconcile(browser, plan, plan_dir)
        applied = _carry_export_warnings(browser, applied)
        return compact_plan_report(
            plan,
            applied=applied,
            reconcile=reconcile_result,
            verbose=verbose,
            extra=extra,
        )

    # 多批：每批都是一次完整的「导出 → 改 → 上传 → 同步 → 对账」，
    # 只是连续做了几次。判决按批汇总，不做跨批协调。
    return await _run_batches(
        browser,
        plan,
        batches,
        plan_dir=plan_dir,
        price="price" in fields,
        qty="qty" in fields,
        apply_plan=apply_plan,
        reconcile=reconcile,
        extra=extra,
        verbose=verbose,
    )
