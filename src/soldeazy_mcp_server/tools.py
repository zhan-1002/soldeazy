"""Soldeazy 数据表工具面（16 个 MCP 工具）。

只依赖本包自己的 config / errors / contracts / providers，不读取任何第三方
API 的凭据，只暴露 ``soldeazy_*`` 一族工具。

工具按用途分四组：

- **会话与边界**：``status`` / ``open_login`` / ``list_shops`` / ``list_sites`` /
  ``switch_shop`` —— 先说清"能操作哪些账号和站点"，再动数据。
- **读取**：``search_datasheet`` —— 只读回显，用于核对。
- **写入**：``change_datasheets``（一步到位）/ ``plan_changes`` + ``confirm_plan``
  + ``apply_plan`` + ``reconcile_plan``（分步确认）。
- **数据表维护**：``create_datasheet`` / ``delete_datasheet`` / ``upload_photo`` /
  ``stock_template`` / ``change_history``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

from soldeazy_mcp_server.contracts import success
from soldeazy_mcp_server.errors import SoldeazyError, tool_error
from soldeazy_mcp_server.providers.soldeazy import _shop_scope_hint
from soldeazy_mcp_server.providers.soldeazy_plan import PlanError
from soldeazy_mcp_server.runtime import logger, mcp, settings, soldeazy

READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
READ_ONLY_NETWORK = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
LOCAL_SESSION_ACTION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
REMOTE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
LOCAL_FILE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _search_text(skus: list[str] | None, raw: str) -> str:
    """Build the space-joined wildcard query the Soldeazy search box expects."""

    if raw.strip():
        return raw.strip()
    terms: list[str] = []
    for sku in skus or []:
        core = str(sku).strip()
        if core:
            terms.append(core if core.startswith("*") else f"*{core}")
    return " ".join(terms)


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def soldeazy_status() -> dict[str, Any]:
    """Report the Soldeazy browser profile, credentials and write-tool state.

    Use this before any Soldeazy call: it says whether a profile exists, whether
    credentials are configured, and whether the write tools are enabled.
    """
    try:
        state = await soldeazy.status()
        # 计划/审计目录由 profile 目录推导（见 _plan_dir），一起回显，
        # 免得"登录态在这、计划却写到别处"这种数据位置问题靠猜。
        state["plan_dir"] = str(_plan_dir())
        return success("soldeazy_status", "soldeazy", state)
    except SoldeazyError as exc:
        return tool_error("soldeazy_status", exc)


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def soldeazy_read_skill(skill_name: str = "") -> dict[str, Any]:
    """Read one packaged Soldeazy Agent Skill (流程知识), or list them all.

    **改价/改量之前先读这个。** 工具 docstring 说不完整条链路，
    而"站点和店铺留空时改动范围怎么定""超 3000 上限怎么办"这类判断题，
    只靠一次报错很难推对，容易反复反问用户。

    不传 ``skill_name`` 时返回清单（名字 + 一句话说明），
    传了就返回全文。也可用 MCP 资源接口读 ``skill://<name>/SKILL.md``。
    """
    try:
        from soldeazy_mcp_server.skill_registry import SkillRegistry

        registry = SkillRegistry()
        if not (skill_name or "").strip():
            return success(
                "soldeazy_read_skill",
                "skill-resource",
                {
                    "skills": [
                        {
                            "name": entry.name,
                            "uri": entry.uri,
                            "description": str(
                                entry.frontmatter.get("description") or ""
                            ),
                        }
                        for entry in registry.list()
                    ],
                },
            )
        entry = registry.get(skill_name)
        text, mime_type = registry.read(entry.uri)
        return success(
            "soldeazy_read_skill",
            "skill-resource",
            {
                "skill": entry.as_dict(),
                "mime_type": mime_type,
                "content": text,
            },
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_read_skill", exc)


@mcp.tool(annotations=LOCAL_SESSION_ACTION)
async def soldeazy_open_login() -> dict[str, Any]:
    """Open the Soldeazy sign-in page in the persistent profile.

    With ``SOLDEAZY_USERNAME``/``SOLDEAZY_PASSWORD`` the form is filled and
    submitted; otherwise the window stays open for a manual sign-in. The browser
    is kept open for later calls until the server stops.
    """
    try:
        return success("soldeazy_open_login", "soldeazy", await soldeazy.interactive_login())
    except SoldeazyError as exc:
        return tool_error("soldeazy_open_login", exc)


def _cap_rows(
    rows: list[dict[str, Any]], *, limit: int = 20, offset: int = 0, with_title: bool = False
) -> dict[str, Any]:
    """把行列表裁成安全的一页。

    生产账号一次搜索可能几千行，全量回传会在对话里吃掉几十万 token。
    这里统一限流：默认 20 条、最多 200 条，标题默认不带。

    默认值必须让整页落在 ``MAX_OUTPUT_CHARS`` 以内：50 条约 2,099 token、
    必然被闸门打回，模型就不得不每次补 ``allow_large_output=true``，
    闸门形同虚设。20 条约 840 token，能正常返回。
    """

    page_size = max(1, min(int(limit or 20), 200))
    start = max(0, int(offset or 0))
    window = rows[start : start + page_size]
    if not with_title:
        window = [{k: v for k, v in row.items() if k != "title"} for row in window]
    return {
        "rows": window,
        "total": len(rows),
        "offset": start,
        "limit": page_size,
        "truncated": start + page_size < len(rows),
        "has_more": start + page_size < len(rows),
    }


# 单次返回的字符上限。超过就拒绝，要求调用方显式确认——
# 这是"大幅消耗 token 前必须先征得同意"的落地点。
MAX_OUTPUT_CHARS = 4000


def _estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return round(ascii_chars / 4 + (len(text) - ascii_chars) * 1.5)


def _shrink_preview(payload: dict[str, Any]) -> dict[str, Any]:
    """预览返回过大时**降级而不是拒绝**。

    预览是"决定要不要执行"的唯一依据，被闸门拒掉等于什么都看不到——
    用户看到的是"返回太大"，而不是"你将要改这 34 条"。
    实测（2026-11-18）：4 个 SKU 的预览就有 1734 token，
    因为 ``must_confirm`` 里的套装/方向提醒是整句中文，比样本行更占体积。

    所以这里按顺序砍**可再生的部分**，留下判决本身：

    1. 逐条样本：砍到 5 条（完整明细本来就在 review_after.xlsx 里）；
    2. ``must_confirm``：保留前 2 条（其余在 plan 文件里）；
    3. 概览与作用范围：不砍，它们才回答"改哪些、改多少"。

    砍完仍超大就退回原样，让闸门照常报错——那时说明连判决都太大了。
    """

    shrunk = json.loads(json.dumps(payload, ensure_ascii=False))
    preview = shrunk.get("preview") or {}
    sample = preview.get("逐条样本")
    if isinstance(sample, list) and len(sample) > 5:
        preview["逐条样本"] = sample[:5]
        truncated = shrunk.setdefault("preview_truncated", {})
        truncated["逐条样本"] = True
        truncated["逐条样本总数"] = len(sample)
        truncated["降级原因"] = "返回体积超限，样本已截断；完整明细见 review_after.xlsx"
    confirms = shrunk.get("must_confirm")
    if isinstance(confirms, list) and len(confirms) > 2:
        shrunk["must_confirm"] = confirms[:2]
        shrunk["must_confirm_total"] = len(confirms)
        shrunk["must_confirm_truncated"] = True
    return shrunk


def _output_too_large(payload: dict[str, Any], *, allow: bool = False) -> dict[str, Any] | None:
    """返回过大时给出"先征得同意"的错误；允许或未超限则返回 None。"""

    text = json.dumps(payload, ensure_ascii=False)
    if allow or len(text) <= MAX_OUTPUT_CHARS:
        return None
    return tool_error(
        "soldeazy_output_guard",
        SoldeazyError(
            "SOLDEAZY_OUTPUT_TOO_LARGE",
            "这次返回约 {tokens} token，超过单次上限（≈{cap} token）。"
            "需要继续的话，请先和用户确认，再带 allow_large_output=true 重调；"
            "或者缩小范围：limit/offset 翻页、关掉 verbose、缩小 targets。".format(
                tokens=_estimate_tokens(text), cap=_estimate_tokens("x" * MAX_OUTPUT_CHARS)
            ),
            details={
                "chars": len(text),
                "estimated_tokens": _estimate_tokens(text),
                "threshold_tokens": _estimate_tokens("x" * MAX_OUTPUT_CHARS),
            },
        ),
    )


async def _search_one_by_one(
    skus: list[str], site: str, *, include_unlisted: bool
) -> dict[str, Any]:
    """逐条搜索再合并：平台合并查询实测会漏个别数据表（2026-11-12 生产确认），
    单条搜索不会。按 (rowid, sku) 去重；搜不到行的 SKU 单独列出来，
    让调用方看见缺口而不是安静地少几行。
    """

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    missing: list[str] = []
    columns: list[str] = []
    for sku in skus:
        found = await soldeazy.search(
            _search_text([sku], ""), site, only_listed=not include_unlisted
        )
        columns = columns or list(found.get("columns") or [])
        rows = found.get("rows") or []
        if not rows:
            missing.append(str(sku))
        for row in rows:
            key = (str(row.get("rowid") or ""), str(row.get("sku") or ""))
            if key not in seen:
                seen.add(key)
                merged.append(row)

    shop_name = (soldeazy.active_shop or {}).get("name") or ""
    data: dict[str, Any] = {
        "site": site,
        "shop": shop_name,
        "search": " ".join(str(sku) for sku in skus),
        "search_mode": "one_by_one",
        "columns": columns,
        "row_count": len(merged),
        "datasheet_count": len(
            {str(row.get("rowid") or "") for row in merged if row.get("rowid")}
        ),
        "rows": merged,
        **_shop_scope_hint(shop_name, merged),
    }
    if missing:
        data["skus_with_no_rows"] = missing[:10]
        data["skus_with_no_rows_total"] = len(missing)
    return data


@mcp.tool(annotations=READ_ONLY_NETWORK)
async def soldeazy_search_datasheet(
    site: str = "",
    skus: list[str] | None = None,
    search_text: str = "",
    limit: int = 20,
    offset: int = 0,
    with_title: bool = False,
    include_unlisted: bool = False,
    allow_large_output: bool = False,
    one_by_one: bool = False,
) -> dict[str, Any]:
    """Read datasheet rows (Row ID, SKU, price, postage, qty) for one site.

    ``skus`` are product codes; each becomes a ``*CODE`` wildcard so the bare
    SKU and its ``US``/``x2``/``x4`` variants come back in one query. Pass
    ``search_text`` to use the box verbatim instead.

    ``site`` 可省略：**不传就是全部站点**（Soldeazy 站点下拉「不勾选 = 全部」），
    省掉先反问用户一轮。返回里会带 ``site_notice`` 告知实际范围。

    **店铺一定要传 `shop`**：既没传 `shop`、也没有 `switch_shop` 设过当前账号时，
    平台不会做店铺过滤（平台语义：不选店铺 = 全部店铺），返回的是所有账号混在一起的行。返回里的 ``shop_filter_applied``
    和 ``shops_in_result`` 会如实告诉你实际是哪个店的。

    **返回条数默认上限 20 条**（``limit``，最多 200）。生产账号一次可能几千条，
    全量返回会在对话里吃掉几十万 token；确实需要全量时用 ``offset`` 翻页。
    ``with_title`` 默认关闭——标题是单行最占字的部分。

    ``one_by_one=true`` 时改成**逐条搜索再合并**（按 rowid+sku 去重）：
    平台的多词合并搜索实测会静默漏数据表（2026-11-12 生产确认，
    即影刀时代挂着"待确认"的 B2），核对类用途请用这个或空搜索取全量。
    """
    # 空查询是合法的：等于「这个渠道账号下的全部数据表」，
    # 计划预览内部就是这么读的，工具层不该比它还严。
    try:
        combined = not (one_by_one and skus and not (search_text or "").strip())
        if combined:
            query = _search_text(skus, search_text)
            data = await soldeazy.search(query, site, only_listed=not include_unlisted)
        else:
            query = " ".join(str(sku) for sku in skus)
            data = await _search_one_by_one(skus, site, include_unlisted=include_unlisted)
        rows = data.get("rows") or []
        payload = {
            **{k: v for k, v in data.items() if k != "rows"},
            **_cap_rows(rows, limit=limit, offset=offset, with_title=with_title),
            **_site_hint(site),
        }
        if combined and len(query.split()) > 1:
            # 2026-11-12 生产实测：平台的多词合并搜索/导出会漏个别数据表
            # （单查、按 ID 查都在，合并结果里没有）。核对类用途请逐条搜，
            # 或直接空搜索拿全量再本地过滤——写入链路本来就是这么做的。
            payload["search_notice"] = (
                "多个 SKU 合并搜索在平台侧实测会漏个别数据表"
                "（单条/按 ID 搜索能查到）。核对请传 one_by_one=true 逐条搜索，"
                "或空搜索取全量后本地过滤。"
            )
        blocked = _output_too_large(payload, allow=allow_large_output)
        if blocked is not None:
            return blocked
        return success("soldeazy_search_datasheet", "soldeazy", payload)
    except SoldeazyError as exc:
        return tool_error("soldeazy_search_datasheet", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy search error")
        return tool_error(
            "soldeazy_search_datasheet",
            SoldeazyError("SOLDEAZY_SEARCH_ERROR", f"搜索失败: {type(exc).__name__}"),
        )


def _shop_scope(found: dict[str, Any]) -> list[str]:
    """搜索结果里出现的渠道账号集合（用于确认写回时还是同一批店铺）。"""

    names = {(row.get("shop") or "").strip() for row in found.get("rows") or []}
    return sorted(name for name in names if name)


def _site_hint(site: str) -> dict[str, Any]:
    """不指定站点时按「全部站点」执行，并明确告诉调用方。

    Soldeazy 的站点下拉是「不勾选 = 全部」，所以传空站点就是不动那个下拉框，
    一次搜索/导出/上传就能覆盖全部站点——不需要逐站点循环，成本不翻倍。
    这样用户不说明站点时也不用先反问一轮，省一整次往返的 token。
    """

    if (site or "").strip():
        return {}
    return {
        "site": "ALL",
        "site_notice": "未指定站点，已按「全部站点」执行（不勾选站点即全部）。",
    }


def _manifest_path() -> str:
    configured = (settings.soldeazy_manifest_path or "").strip()
    if configured:
        return configured
    return str(settings.data_dir / "soldeazy-writes.jsonl")


async def _site_scope(keyword: str) -> dict[str, Any]:
    """列出**当前登录能操作的站点**（从页面下拉实时读，不查硬编码表）。

    和 `soldeazy_list_shops` 对称：店铺和站点都是搜索表单上的可选边界，
    必须从同一个地方（页面）取，否则表格会漂移、导致静默跨站。
    """

    options = await soldeazy.list_site_options()
    # 空 id 是「全部站点」那一项，单独标出来而不是混进列表
    all_sites = [o for o in options if not o["id"]]
    selectable = [o for o in options if o["id"]]
    if keyword:
        needle = keyword.strip().upper()
        selectable = [
            o for o in selectable if needle in o["name"].upper() or needle in o["id"]
        ]
    return {
        "count": len(selectable),
        "active": soldeazy.active_shop or {},
        "all_sites_option": [o["name"] for o in all_sites],
        "sites": selectable,
    }


@mcp.tool(annotations=READ_ONLY_NETWORK)
async def soldeazy_list_sites(keyword: str = "") -> dict[str, Any]:
    """List the sites (marketplaces) this login can operate, read from the page.

    **站点和店铺一样，边界由平台决定**——所以这里读的是搜索表单
    ``select[name=listsite]`` 的真实选项，不是代码里写死的表。
    （历史上硬编码表漏了 SG/HK/MY，导致"改 SG 的价格"静默改到全站点。）

    返回每项的 ``id``（提交给平台的 value）和 ``name``（下拉显示名，
    如 ``Germany`` / ``Singapore``）。用户写 ``sg`` / ``Singapore`` /
    ``77`` 都能被精确识别；写错了会明确报错，绝不退回"全部站点"。

    `keyword` 按显示名或 value 过滤。
    """

    try:
        data = await _site_scope(keyword)
        return success("soldeazy_list_sites", "soldeazy", data)
    except SoldeazyError as exc:
        return tool_error("soldeazy_list_sites", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy site list error")
        return tool_error(
            "soldeazy_list_sites",
            SoldeazyError("SOLDEAZY_SITES_ERROR", f"读取站点列表失败: {type(exc).__name__}"),
        )


@mcp.tool(annotations=READ_ONLY_NETWORK)
async def soldeazy_list_shops(
    keyword: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """List the channel accounts (eBay shops) this login may operate.

    Returns each shop's id and short name; `keyword` filters by name. The list
    comes from the datasheet search form, which reflects the site's own
    authorisation rather than a guessed inventory.

    **默认只回 20 个**（生产账号可能有上百个，全量倒进对话会吃掉大量 token）。
    只记得部分名字时先传 `keyword`（如 `EBAY`、`US`），比翻页更省。
    """

    try:
        shops = await soldeazy.list_shop_options()
        active = soldeazy.active_shop or {}
        if keyword:
            needle = keyword.strip().upper()
            shops = [s for s in shops if needle in s["name"].upper() or needle in s["id"]]
        total = len(shops)
        page = max(1, min(int(limit), 200))
        start = max(0, int(offset))
        window = shops[start : start + page]
        payload = {
            "count": total,
            "active": active,
            "shops": window,
            "has_more": start + len(window) < total,
        }
        if start + len(window) < total and not keyword:
            payload["hint"] = "账号较多：传 keyword 只查需要的那个，比翻页省 token。"
        return success("soldeazy_list_shops", "soldeazy", payload)
    except SoldeazyError as exc:
        return tool_error("soldeazy_list_shops", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy shop list error")
        return tool_error(
            "soldeazy_list_shops",
            SoldeazyError("SOLDEAZY_SHOPS_ERROR", f"读取渠道账号失败: {type(exc).__name__}"),
        )


@mcp.tool(annotations=LOCAL_SESSION_ACTION)
async def soldeazy_switch_shop(shop: str) -> dict[str, Any]:
    """Switch the active channel account for later calls.

    Accepts the short name (as shown in the platform's shop dropdown, e.g.
    ``ACME``) or the numeric id. After switching, search and preview tools
    default to this shop instead of asking again.
    """
    try:
        picked = await soldeazy.switch_shop(shop)
        return success("soldeazy_switch_shop", "soldeazy", {"active": picked})
    except SoldeazyError as exc:
        return tool_error("soldeazy_switch_shop", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy switch error")
        return tool_error(
            "soldeazy_switch_shop",
            SoldeazyError("SOLDEAZY_SWITCH_ERROR", f"切换渠道账号失败: {type(exc).__name__}"),
        )


def _require_write_enabled() -> None:
    if not settings.soldeazy_write_enabled:
        raise SoldeazyError(
            "SOLDEAZY_WRITE_DISABLED",
            "写工具未开启：确认只读链路验证通过后设置 SOLDEAZY_WRITE_ENABLED=1。",
        )


async def _resolve_shop_id(shop: str) -> dict[str, str]:
    """把渠道账号简称/id 解析成 ``{id, name}``；空值表示**全部店铺**。

    ``shop`` 留空是合法输入（用户 2026-11-19 定）：
    - 给了 ``shop`` → 按它切
    - 没给但有 active_shop → 用当前账号
    - 都没有 → **返回空 dict = 全部店铺**，不报错

    原来这里抛 ``SOLDEAZY_SHOP_REQUIRED``，导致"站点和店铺都留空"这条最自然的
    用法直接失败，客户端 AI 拿不到任何范围信息，只能反复反问用户
    「同一 SKU 在多个店铺都有，改价范围怎么定」。

    按全部店铺执行并不危险：平台语义本来就是「不选 = 全部」，
    而返回里的 ``preview.作用范围`` 会如实列出实际命中的账号。
    """

    if shop:
        return await soldeazy.switch_shop(shop)
    if soldeazy.active_shop:
        return soldeazy.active_shop
    return {}


def _resolve_targets(
    targets: dict[str, float] | None, targets_file: str, *, kind: str
) -> tuple[dict[str, float], dict[str, Any]]:
    """``targets`` 与 ``targets_file`` 二选一，返回 (目标字典, 回显信息)。

    为什么要文件入口：一次改动常常是成百上千个 SKU，内联字典要模型逐条生成，
    而且会整段留在上下文里反复重发。给一个路径，调用方只发 20 来个 token。
    """

    from soldeazy_mcp_server.providers.soldeazy_change import MAX_PROBLEMS
    from soldeazy_mcp_server.providers.soldeazy_targets import read_targets_file

    path = (targets_file or "").strip()
    inline = dict(targets or {})

    if path and inline:
        raise SoldeazyError(
            "SOLDEAZY_TARGETS_AMBIGUOUS",
            "targets 与 targets_file 只能给一个，否则不知道该信哪份。",
        )
    if not path:
        return inline, {}

    batch = read_targets_file(path, kind=kind)
    if batch.issues:
        raise SoldeazyError(
            "SOLDEAZY_TARGETS_FILE",
            f"目标文件有 {len(batch.issues)} 条问题，未执行任何改动。",
            details={
                "source": batch.source,
                "issues": batch.issues[:MAX_PROBLEMS],
                "issues_total": len(batch.issues),
            },
        )
    if not batch.targets:
        raise SoldeazyError(
            "SOLDEAZY_PLAN_EMPTY",
            "目标文件里没有可用行。",
            details={"source": batch.source},
        )

    info: dict[str, Any] = {
        "targets_from": batch.source,
        "targets_count": len(batch.targets),
    }
    if batch.skipped:
        info["targets_skipped_total"] = len(batch.skipped)
    return batch.targets, info


@mcp.tool(annotations=REMOTE_WRITE)
async def soldeazy_change_datasheets(
    prices: dict[str, float] | None = None,
    quantities: dict[str, float] | None = None,
    site: str = "",
    shop: str = "",
    dry_run: bool = True,
    verbose: bool = False,
    include_unlisted: bool = False,
    allow_large_output: bool = False,
    prices_file: str = "",
    quantities_file: str = "",
) -> dict[str, Any]:
    """**改数据表**：价格和数量是同一次授权的两个结果，一次上传、一次同步。

    为什么只有一个工具：上传是整行覆盖，E 列（数量）和 F 列（价格）本来就会
    同时写；线上生效只发生在同步那一步，靠勾选「价格及邮费」/「刊登数量」。
    所以"改价"和"改库存"不是两条链路，而是同一份计划的两种条目。

    * ``prices``     = ``{子体SKU: 新价格}`` —— 按子体**精确**匹配。
      套装（``SKU x2``）必须显式写出才会改；基础 SKU 只改单件行。
    * ``quantities`` = ``{母体SKU: 新数量}`` —— 按母体展开，该母体下
      **全部子体行**都写这个数量（绝对值，不做打包换算）。

    只给一个就是只改那样；两个都给就是一次改完，同步时两个键一起推。
    值已经是目标的（差 < 0.005）不会写入，所以重复执行是安全的。

    **默认 dry_run=true 只出预览**：生成两份可读文件
    （``review_before`` / ``review_after``，只保留涉及的行，右侧「变更标记」列
    写明 ``E: 100→50``），人工打开逐行核对。

    核对完传 ``dry_run=false`` 执行。**人工核对能落实，是因为明细只在文件里**：
    预览返回值只有计数（改了几条、几份表、跳过多少套装），不足以判断改得对不对；
    要判断就得打开 review_after。AI 读计数、人读文件，两边不重复劳动、不烧 token。

    线上现值在预览之后变过怎么办：执行时会重新导出、重新定位每一行，
    定位不到就整批拒绝（``missing``），不会带着过期数据上传。

    **成百上千条时用文件**：``prices_file`` / ``quantities_file`` 给 Excel/CSV
    路径（``SKU,值`` 一行一条），对应内联参数传空，几千条不会进对话。

    ``site`` 可省略：**不传 = 全部站点**；给了就必须精确命中平台上的站点，
    写错会报错并列出可用站点（不会静默退回全站，避免改到别的站点）。
    """

    try:
        from soldeazy_mcp_server.providers.soldeazy_apply import (
            apply_plan,
            build_items,
            make_plan,
            reconcile,
        )
        from soldeazy_mcp_server.providers.soldeazy_change import run_change
        from soldeazy_mcp_server.providers.soldeazy_plan import (
            freeze,
            load_verified,
            write_draft,
        )

        has_prices = bool(prices) or bool((prices_file or "").strip())
        has_quantities = bool(quantities) or bool((quantities_file or "").strip())
        if not has_prices and not has_quantities:
            raise SoldeazyError(
                "SOLDEAZY_PLAN_EMPTY",
                "prices 与 quantities 至少要给一个（内联字典或文件路径）。",
            )
        if not dry_run:
            _require_write_enabled()

        resolved_prices: dict[str, float] = {}
        resolved_quantities: dict[str, float] = {}
        targets_info: dict[str, Any] = {}

        if has_prices:
            resolved_prices, price_info = _resolve_targets(
                prices, prices_file, kind="price"
            )
            targets_info["price_targets_count"] = price_info.get(
                "targets_count", len(resolved_prices)
            )
            if price_info.get("targets_from"):
                targets_info["price_targets_from"] = price_info["targets_from"]

        if has_quantities:
            resolved_quantities, qty_info = _resolve_targets(
                quantities, quantities_file, kind="qty"
            )
            targets_info["qty_targets_count"] = qty_info.get(
                "targets_count", len(resolved_quantities)
            )
            if qty_info.get("targets_from"):
                targets_info["qty_targets_from"] = qty_info["targets_from"]

        data = await run_change(
            site=site,
            prices=resolved_prices or None,
            quantities=resolved_quantities or None,
            shop=shop,
            dry_run=dry_run,
            verbose=verbose,
            only_listed=not include_unlisted,
            deps={
                "build_items": build_items,
                "make_plan": make_plan,
                "write_draft": write_draft,
                "freeze": freeze,
                "load_verified": load_verified,
                "apply_plan": apply_plan,
                "reconcile": reconcile,
                "plan_dir": _plan_dir(),
                "browser": soldeazy,
                "resolve_shop": _resolve_shop_id,
            },
        )
        data.update(_site_hint(site))
        data.update(targets_info)
        # 预览被闸门拒掉等于什么都看不到——而它正是"决定要不要执行"的依据。
        # 所以超限时先砍可再生的部分（样本 / 提醒条数），判决本身保留。
        blocked = _output_too_large(data, allow=allow_large_output)
        if blocked is not None and dry_run:
            data = _shrink_preview(data)
            blocked = _output_too_large(data, allow=allow_large_output)
        if blocked is not None:
            return blocked
        return success("soldeazy_change_datasheets", "soldeazy", data)
    except SoldeazyError as exc:
        return tool_error("soldeazy_change_datasheets", exc)
    except Exception as exc:  # pragma: no cover
        logger.exception("Unhandled Soldeazy change error")
        return tool_error(
            "soldeazy_change_datasheets",
            SoldeazyError(
                "SOLDEAZY_CHANGE_ERROR", f"改数据表失败: {type(exc).__name__}"
            ),
        )


@mcp.tool(annotations=REMOTE_WRITE)
async def soldeazy_create_datasheet(
    item_ids: str = "",
    shop: str = "",
    template: str = "",
    site: str = "US",
    title: str = "",
    sku: str = "",
    price: float = 0.0,
    qty: int = 0,
    category: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create a new datasheet, either copied from live listings or built from scratch.

    Two modes:

    * ``item_ids`` given → ``create_datasheet_from_listing_spy``, which copies the
      listing content and returns the full 212-field payload;
    * no ``item_ids`` → creates a blank datasheet (``template`` presets the
      listing parameters), then optionally fills title/SKU/price/qty/category.

    ``dry_run`` defaults to true and only reports what would happen.
    """

    try:
        picked = await _resolve_shop_id(shop)
        plan: dict[str, Any] = {
            "shop": picked,
            "site": site,
            "template": template,
            "item_ids": item_ids,
        }

        if item_ids:
            plan["mode"] = "from_listing"
            if dry_run:
                return success("soldeazy_create_datasheet", "soldeazy", {**plan, "dry_run": True})
            _require_write_enabled()
            created = await soldeazy.create_from_listing_spy(item_ids, picked["id"])
            return success(
                "soldeazy_create_datasheet", "soldeazy", {**plan, "dry_run": False, **created}
            )

        fields: dict[str, Any] = {}
        if title:
            fields["dsheet_titleexternal"] = title
        if category:
            fields["dsheet_ebcategory"] = category
        if sku:
            fields["dsheet_full_item_model"] = sku
            fields["dsku_product_code[]"] = sku
        if price:
            fields["dsku_price[]"] = price
        if qty:
            fields["dsku_qty[]"] = qty

        plan.update({"mode": "blank", "fields": fields})
        if dry_run:
            return success("soldeazy_create_datasheet", "soldeazy", {**plan, "dry_run": True})

        _require_write_enabled()
        blank = await soldeazy.create_blank_datasheet(
            shop_idx=picked["id"], site_code=site, template_idx=template
        )
        saved = await soldeazy.save_datasheet_fields(blank["rowid"], fields) if fields else None
        return success(
            "soldeazy_create_datasheet",
            "soldeazy",
            {**plan, "dry_run": False, "rowid": blank["rowid"], "saved": saved},
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_create_datasheet", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy create error")
        return tool_error(
            "soldeazy_create_datasheet",
            SoldeazyError("SOLDEAZY_CREATE_ERROR", f"创建数据表失败: {type(exc).__name__}"),
        )


@mcp.tool(annotations=REMOTE_WRITE)
async def soldeazy_delete_datasheet(rowids: str, dry_run: bool = True) -> dict[str, Any]:
    """Delete one or more datasheets by row id (comma separated).

    Deletion submits the list form with ``table_action=bulk-delete`` and
    ``mode=dsheet_datatable``; the row must be searched first or the server
    silently ignores the request.
    """

    try:
        ids = [part.strip() for part in (rowids or "").split(",") if part.strip()]
        if not ids:
            raise SoldeazyError("SOLDEAZY_NO_ROWID", "请给出要删除的数据表编号。")
        if dry_run:
            return success(
                "soldeazy_delete_datasheet",
                "soldeazy",
                {"rowids": ids, "dry_run": True},
            )
        _require_write_enabled()
        result = await soldeazy.delete_datasheets(ids)
        return success(
            "soldeazy_delete_datasheet", "soldeazy", {**result, "dry_run": False}
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_delete_datasheet", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy delete error")
        return tool_error(
            "soldeazy_delete_datasheet",
            SoldeazyError("SOLDEAZY_DELETE_ERROR", f"删除数据表失败: {type(exc).__name__}"),
        )


@mcp.tool(annotations=REMOTE_WRITE)
async def soldeazy_upload_photo(
    file_path: str = "",
    image_url: str = "",
    rowid: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Upload a product image, or attach an already-reachable image URL.

    * ``file_path`` → posts to ``datasheet_photo/upload_photos`` and returns the
      stored file name plus the CDN address;
    * ``image_url`` → validated with a HEAD request and used as-is (hot link);
    * ``rowid`` given → writes the result into ``dsheet_gallery`` and saves.

    ``dry_run`` defaults to true: nothing is uploaded and nothing is saved.
    """

    try:
        plan: dict[str, Any] = {"rowid": rowid, "file_path": file_path, "image_url": image_url}
        if not file_path and not image_url:
            raise SoldeazyError("SOLDEAZY_PHOTO_SOURCE", "请给出 file_path 或 image_url。")
        if dry_run:
            return success("soldeazy_upload_photo", "soldeazy", {**plan, "dry_run": True})

        _require_write_enabled()
        if file_path:
            source = Path(file_path).expanduser()
            if not source.is_file():
                raise SoldeazyError("SOLDEAZY_PHOTO_MISSING", f"文件不存在: {source}")
            uploaded = await soldeazy.upload_photo(source)
            gallery = uploaded["file_name"]
            plan.update({"uploaded": uploaded, "gallery": gallery})
        else:
            response = await soldeazy.head_url(image_url)
            if response != 200:
                raise SoldeazyError(
                    "SOLDEAZY_PHOTO_UNREACHABLE",
                    f"图片地址不可访问: HTTP {response}",
                )
            gallery = image_url
            plan.update({"gallery": gallery})

        if rowid:
            saved = await soldeazy.save_datasheet_fields(rowid, {"dsheet_gallery": gallery})
            plan["saved"] = saved
        return success("soldeazy_upload_photo", "soldeazy", {**plan, "dry_run": False})
    except SoldeazyError as exc:
        return tool_error("soldeazy_upload_photo", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy photo error")
        return tool_error(
            "soldeazy_upload_photo",
            SoldeazyError("SOLDEAZY_PHOTO_ERROR", f"图片处理失败: {type(exc).__name__}"),
        )


def _plan_dir() -> Path:
    """不可变计划的存放目录（与 manifest 同级的 soldeazy-plans/）。"""

    return settings.data_dir / "soldeazy-plans"


@mcp.tool(annotations=READ_ONLY_NETWORK)
async def soldeazy_plan_changes(
    prices: dict[str, float] | None = None,
    quantities: dict[str, float] | None = None,
    site: str = "",
    shop: str = "",
    supersedes: str = "",
    allow_large_output: bool = False,
) -> dict[str, Any]:
    """Draft a change plan (read-only): what would change, row by row.

    和 ``soldeazy_change_datasheets`` 同一套语义，只是**不执行**——只起草、
    落一份 JSON 草稿，等人用 ``soldeazy_confirm_plan`` 冻结。

    * ``prices``     = ``{子体SKU: 新价格}``
    * ``quantities`` = ``{母体SKU: 新数量}``

    只起草其中一样就只传那一个；两个都传就是一份混合计划。
    ``site`` 可省略：**不传 = 全部站点**；给了必须精确命中。

    Nothing is written. The plan stays a rewritable draft until
    ``soldeazy_confirm_plan`` freezes it.
    """

    try:
        from soldeazy_mcp_server.providers.soldeazy_apply import build_items, make_plan
        from soldeazy_mcp_server.providers.soldeazy_plan import write_draft

        if not prices and not quantities:
            raise SoldeazyError(
                "SOLDEAZY_PLAN_EMPTY", "prices 与 quantities 至少要给一个。"
            )

        picked = await _resolve_shop_id(shop)
        live = await soldeazy.read_datasheet_values(site)

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

        fields = {item.field for item in items}
        kind = (
            "price_qty"
            if {"price", "qty"} <= fields
            else "price"
            if "price" in fields
            else "qty"
        )
        plan = make_plan(kind, site, picked, items, supersedes=supersedes)

        # 没有任何改动就不落计划文件：否则会留下一份 items=0 的草稿，
        # 后面 confirm/reconcile 撞上去只会报一句看不懂的"找不到计划"。
        path = str(write_draft(_plan_dir(), plan)) if items else ""

        payload = {
            "plan_id": plan.plan_id if items else "",
            "path": path,
            "kind": kind,
            **_site_hint(site),
            "shop": picked,
            "rows_in_scope": len(live),
            "planned_items": len(items),
            "price_items": len([item for item in items if item.field == "price"]),
            "qty_items": len([item for item in items if item.field == "qty"]),
            "items": [item.as_dict() for item in items[:20]],
            "items_truncated": len(items) > 20,
            "items_total": len(items),
            "missing": report["missing"][:10],
            "missing_total": len(report["missing"]),
            # 同母体下还有别的变体没点到：**交人工判断**要不要一起改
            "needs_confirmation": dict(
                list(report["needs_confirmation"].items())[:10]
            ),
            "needs_confirmation_total": len(report["needs_confirmation"]),
            "next": (
                "确认无误后调用 soldeazy_confirm_plan 冻结"
                if items
                else "没有需要改的行，未生成计划文件"
            ),
        }
        blocked = _output_too_large(payload, allow=allow_large_output)
        if blocked is not None:
            return blocked
        return success("soldeazy_plan_changes", "soldeazy", payload)
    except SoldeazyError as exc:
        return tool_error("soldeazy_plan_changes", exc)
    except Exception as exc:  # pragma: no cover - browser safety boundary
        logger.exception("Unhandled Soldeazy plan error")
        return tool_error(
            "soldeazy_plan_changes",
            SoldeazyError("SOLDEAZY_PLAN_ERROR", f"生成计划失败: {type(exc).__name__}"),
        )


def _require_plan_id(plan_id: str) -> str:
    """计划 id 必须非空——空 id 会让后续查找报出看不懂的错误。"""

    value = (plan_id or "").strip()
    if not value:
        raise SoldeazyError(
            "SOLDEAZY_PLAN_MISSING",
            "没有可用的计划 id：先调用 soldeazy_plan_changes 生成计划。"
            "（如果它返回 planned_items=0，说明本来就没有需要改的行。）",
        )
    return value


@mcp.tool(annotations=LOCAL_SESSION_ACTION)
async def soldeazy_confirm_plan(plan_id: str) -> dict[str, Any]:
    """Freeze a draft plan: write sha256, mark read-only, refuse further edits.

    After this call the JSON can no longer be changed by any tool; execution
    verifies the hash every time. If the content turns out wrong, draft a new
    plan with ``supersedes`` and archive the old one.
    """

    try:
        from soldeazy_mcp_server.providers.soldeazy_plan import freeze, load_raw

        plan_id = _require_plan_id(plan_id)
        directory = _plan_dir()
        path = directory / f"{plan_id}.json"
        if not path.exists():
            raise SoldeazyError("SOLDEAZY_PLAN_MISSING", f"找不到计划 {plan_id}")
        raw = load_raw(path)
        from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

        plan = ChangePlan(
            plan_id=raw["plan_id"],
            kind=raw["kind"],
            site=raw.get("site", ""),
            shop=raw.get("shop") or {},
            created_at=raw.get("created_at", ""),
            items=[PlanItem(**item) for item in raw.get("items", [])],
            supersedes=raw.get("supersedes", ""),
            note=raw.get("note", ""),
            # 旧计划没有这个字段时按「包含全部」处理（见 soldeazy_plan.load_verified）
            only_listed=bool(raw.get("only_listed", False)),
        )
        if not plan.items:
            raise SoldeazyError("SOLDEAZY_PLAN_EMPTY", "计划里没有任何改动，不冻结。")
        info = freeze(directory, plan)
        return success("soldeazy_confirm_plan", "soldeazy", {**info, "plan_id": plan_id})
    except PlanError as exc:
        return tool_error(
            "soldeazy_confirm_plan",
            SoldeazyError("SOLDEAZY_PLAN_INVALID", str(exc)[:200]),
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_confirm_plan", exc)
    except Exception as exc:
        logger.exception("Unhandled Soldeazy confirm error")
        return tool_error(
            "soldeazy_confirm_plan",
            SoldeazyError("SOLDEAZY_CONFIRM_ERROR", str(exc)[:200]),
        )


@mcp.tool(annotations=REMOTE_WRITE)
async def soldeazy_apply_plan(
    plan_id: str,
    dry_run: bool = True,
    sync: bool = True,
    allow_large_output: bool = False,
) -> dict[str, Any]:
    """Execute a frozen plan: export → batch edit → one upload → queue check.

    The plan file is never modified; every attempt is appended to an audit log.
    ``dry_run`` defaults to true: it verifies the hash and reports scope only.

    同步键**从计划内容推导**，不用调用方指定：计划里有价格条目就推「价格及邮费」，
    有数量条目就推「刊登数量」，两者都有就两个都推（一次同步同时生效）。
    这样既不会漏推，也不会把没改的字段顺手覆盖到线上。
    """

    try:
        from soldeazy_mcp_server.providers.soldeazy_apply import apply_plan
        from soldeazy_mcp_server.providers.soldeazy_plan import load_verified

        plan_id = _require_plan_id(plan_id)
        directory = _plan_dir()
        plan, digest = load_verified(directory, plan_id)

        scope = {
            "plan_id": plan_id,
            "sha256": digest,
            "kind": plan.kind,
            "site": plan.site,
            "shop": plan.shop,
            "items": len(plan.items),
            "price_items": len([item for item in plan.items if item.field == "price"]),
            "qty_items": len([item for item in plan.items if item.field == "qty"]),
            "datasheets": len({item.rowid for item in plan.items}),
        }
        # 同步键由**计划实际改了什么**决定，不靠调用方猜：
        # 只改价就只推价格，只改量就只推数量，两者都有就两个都推。
        # 旧默认（sync_price=True / sync_quantity=False）对改量计划是错的——
        # 会把价格推上去、数量反而不推。
        fields = {item.field for item in plan.items}
        want_price = "price" in fields
        want_qty = "qty" in fields
        scope["sync_keys"] = [
            name
            for name, wanted in (("价格及邮费", want_price), ("刊登数量", want_qty))
            if wanted
        ]
        if dry_run:
            return success(
                "soldeazy_apply_plan",
                "soldeazy",
                {
                    **scope,
                    "sync_price": want_price,
                    "sync_quantity": want_qty,
                    "dry_run": True,
                },
            )

        _require_write_enabled()
        result = await apply_plan(
            soldeazy, plan, directory,
            sync=bool(sync), sync_price=want_price, sync_quantity=want_qty,
        )
        payload = {**scope, **result, "dry_run": False, "next": "调用 soldeazy_reconcile_plan 对账"}
        blocked = _output_too_large(payload, allow=allow_large_output)
        if blocked is not None:
            return blocked
        return success("soldeazy_apply_plan", "soldeazy", payload)
    except PlanError as exc:
        return tool_error(
            "soldeazy_apply_plan",
            SoldeazyError("SOLDEAZY_PLAN_INVALID", str(exc)[:200]),
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_apply_plan", exc)
    except Exception as exc:
        logger.exception("Unhandled Soldeazy apply error")
        return tool_error(
            "soldeazy_apply_plan",
            SoldeazyError("SOLDEAZY_APPLY_ERROR", str(exc)[:200]),
        )


@mcp.tool(annotations=READ_ONLY_NETWORK)
async def soldeazy_reconcile_plan(
    plan_id: str, allow_large_output: bool = False
) -> dict[str, Any]:
    """Compare the live values against a frozen plan and write a report file.

    The report is a new file (``<plan_id>.reconcile.json``); the plan itself is
    untouched and stays the single source of what was intended.
    """

    try:
        from soldeazy_mcp_server.providers.soldeazy_apply import reconcile
        from soldeazy_mcp_server.providers.soldeazy_plan import load_verified

        plan_id = _require_plan_id(plan_id)
        directory = _plan_dir()
        plan, digest = load_verified(directory, plan_id)
        report = await reconcile(soldeazy, plan, directory)
        payload = {**report, "sha256": digest}
        blocked = _output_too_large(payload, allow=allow_large_output)
        if blocked is not None:
            return blocked
        return success("soldeazy_reconcile_plan", "soldeazy", payload)
    except PlanError as exc:
        return tool_error(
            "soldeazy_reconcile_plan",
            SoldeazyError("SOLDEAZY_PLAN_INVALID", str(exc)[:200]),
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_reconcile_plan", exc)
    except Exception as exc:
        logger.exception("Unhandled Soldeazy reconcile error")
        return tool_error(
            "soldeazy_reconcile_plan",
            SoldeazyError("SOLDEAZY_RECONCILE_ERROR", str(exc)[:200]),
        )


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def soldeazy_change_history(
    limit: int = 20,
    site: str = "",
    kind: str = "",
) -> dict[str, Any]:
    """Read the local Soldeazy write history (newest first).

    两个来源合并后按时间倒序返回：

    * ``plan-audit`` —— 现在的主链路，``soldeazy_apply_plan`` 每执行一个计划
      就写一行 ``<plan_id>.audit.jsonl``（含 applied/failed/warnings）；
    * ``legacy-manifest`` —— 已停用的旧写入链路留下的 ``soldeazy-writes.jsonl``，
      只为让历史记录还能查到。

    ``site``/``kind`` 过滤（kind 用 price 或 stock，``qty`` 与 ``stock`` 等价）。
    坏行跳过，不让一条脏记录毁掉整份历史。
    """
    try:
        from soldeazy_mcp_server.providers.soldeazy_apply import (
            read_audit,
            read_manifest,
        )

        if kind and kind not in {"price", "stock", "qty"}:
            raise SoldeazyError("SOLDEAZY_HISTORY_KIND", "kind 只能是 price 或 stock。")
        cap = max(1, min(int(limit), 200))
        manifest_path = _manifest_path()
        plan_dir = _plan_dir()
        legacy = read_manifest(manifest_path, limit=cap, site=site, kind=kind)
        current = read_audit(plan_dir, limit=cap, site=site, kind=kind)
        records = sorted(
            [*current, *legacy],
            key=lambda record: str(record.get("at") or ""),
            reverse=True,
        )[:cap]
        return success(
            "soldeazy_change_history",
            "local-audit",
            {
                "manifest": str(manifest_path),
                "plan_dir": str(plan_dir),
                "count": len(records),
                "records": records,
                "sources": {"plan_audit": len(current), "legacy_manifest": len(legacy)},
            },
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_change_history", exc)
    except Exception as exc:  # pragma: no cover - 磁盘异常
        return tool_error(
            "soldeazy_change_history",
            SoldeazyError("SOLDEAZY_HISTORY_ERROR", f"读取变更历史失败: {exc}"),
        )


@mcp.tool(annotations=LOCAL_FILE_WRITE)
async def soldeazy_stock_template(out_path: str, skus: list[str]) -> dict[str, Any]:
    """Write the stock sheet that the operator fills in by hand.

    One row per actual SKU with an empty ``库存`` column; only that column is
    read back, so the operator never edits SKU spellings. Requires the
    ``soldeazy`` extra (openpyxl).
    """
    try:
        from soldeazy_mcp_server.providers.soldeazy_stock import write_stock_template

        target = write_stock_template(out_path, skus)
        return success(
            "soldeazy_stock_template",
            "local-writer",
            {"path": str(target), "rows": len(skus)},
        )
    except SoldeazyError as exc:
        return tool_error("soldeazy_stock_template", exc)
    except Exception as exc:  # pragma: no cover - 依赖缺失/磁盘错误
        return tool_error(
            "soldeazy_stock_template",
            SoldeazyError("SOLDEAZY_TEMPLATE_ERROR", f"生成库存模板失败: {exc}"),
        )
