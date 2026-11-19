"""Soldeazy 工具面测试（独立服务）。

从 ``tests/test_server_surface.py`` 拆出来：这些用例测的是
``soldeazy_mcp_server`` 的 16 个工具、输出闸门与计划链路，
与 eBay 官方 API 服务无关。
"""

import pytest

from soldeazy_mcp_server import server

SOLDEAZY_TOOL_NAMES = {
    "soldeazy_status",
    "soldeazy_open_login",
    "soldeazy_search_datasheet",
    "soldeazy_stock_template",
    "soldeazy_change_history",
    "soldeazy_list_shops",
    "soldeazy_list_sites",
    "soldeazy_switch_shop",
    "soldeazy_create_datasheet",
    "soldeazy_delete_datasheet",
    "soldeazy_upload_photo",
    "soldeazy_plan_changes",
    "soldeazy_confirm_plan",
    "soldeazy_apply_plan",
    "soldeazy_change_datasheets",
    "soldeazy_reconcile_plan",
    "soldeazy_read_skill",
}


def test_standalone_server_exposes_exactly_the_soldeazy_tools():
    """独立服务只有 Soldeazy 一族工具，且没有 eBay 官方工具混进来。"""

    from soldeazy_mcp_server import tools

    exposed = set(tools.mcp._tool_manager._tools)
    assert exposed == SOLDEAZY_TOOL_NAMES
    assert len(exposed) == 17
    assert not any(name.startswith("ebay_") for name in exposed)
    assert set(server.EXPOSED_TOOLS) == SOLDEAZY_TOOL_NAMES


def test_no_retired_preview_or_token_write_tools():
    """旧「预览 → 令牌 → 写入」链路已下线，不能悄悄复活。"""

    from soldeazy_mcp_server import tools

    tools_now = set(tools.mcp._tool_manager._tools)
    assert not ({"soldeazy_update_price", "soldeazy_update_stock"} & tools_now)
    assert not any("preview" in name for name in tools_now)


def test_output_schema_is_disabled_to_save_request_tokens():
    import asyncio

    from soldeazy_mcp_server import tools

    tools_list = asyncio.run(tools.mcp.list_tools())
    assert tools_list
    assert all(tool.outputSchema is None for tool in tools_list)


def test_new_write_tools_default_to_dry_run():
    import inspect

    from soldeazy_mcp_server import tools

    for tool in (
        tools.soldeazy_create_datasheet,
        tools.soldeazy_delete_datasheet,
        tools.soldeazy_upload_photo,
        # 2026-11-12：一步改价/改库存也翻成默认预览，写入必须显式 dry_run=false
        tools.soldeazy_change_datasheets,
        tools.soldeazy_change_datasheets,
    ):
        assert inspect.signature(tool).parameters["dry_run"].default is True


def test_search_datasheet_site_defaults_to_all_sites():
    """不传站点 = 全部站点，不再直接报错要求先问用户。"""

    import inspect

    from soldeazy_mcp_server import tools

    signature = inspect.signature(tools.soldeazy_search_datasheet)
    assert signature.parameters["site"].default == ""

    for tool in (tools.soldeazy_change_datasheets, tools.soldeazy_change_datasheets):
        assert inspect.signature(tool).parameters["site"].default == ""


def test_site_hint_tells_the_user_when_all_sites_are_used():
    from soldeazy_mcp_server import tools

    assert tools._site_hint("US") == {}
    assert tools._site_hint("  us ") == {}
    hint = tools._site_hint("")
    assert hint["site"] == "ALL"
    assert "全部站点" in hint["site_notice"]
    assert "全部站点" in tools._site_hint("   ")["site_notice"]


def test_shop_scope_collects_distinct_shops():
    from soldeazy_mcp_server import tools

    found = {
        "rows": [
            {"shop": "EBAY FUN"},
            {"shop": "EBAY FUN"},
            {"shop": "EBAY LT"},
            {"shop": None},
            {"shop": "  "},
        ]
    }
    assert tools._shop_scope(found) == ["EBAY FUN", "EBAY LT"]
    assert tools._shop_scope({}) == []
    assert tools._shop_scope({"rows": []}) == []


def test_change_tool_signature_is_symmetric_for_price_and_quantity():
    """价格和数量是对称的两个可选入参，没有主次之分。"""

    import inspect

    from soldeazy_mcp_server import tools

    params = list(inspect.signature(tools.soldeazy_change_datasheets).parameters)
    assert params[0] == "prices"
    assert params[1] == "quantities"
    # 两者都必须有默认值 None：只改一样时另一个可以不传
    sig = inspect.signature(tools.soldeazy_change_datasheets)
    assert sig.parameters["prices"].default is None
    assert sig.parameters["quantities"].default is None


def test_plan_tools_reject_empty_plan_id():
    import asyncio

    from soldeazy_mcp_server import tools

    for tool in (
        tools.soldeazy_confirm_plan,
        tools.soldeazy_apply_plan,
        tools.soldeazy_reconcile_plan,
    ):
        result = asyncio.run(tool(""))
        assert result["success"] is False
        assert result["error"]["code"] == "SOLDEAZY_PLAN_MISSING"


def test_apply_plan_has_shop_guard():
    from soldeazy_mcp_server.providers.soldeazy_apply import shop_problems

    assert callable(shop_problems)


def test_cap_rows_defaults_and_caps():
    from soldeazy_mcp_server.tools import _cap_rows

    rows = [{"rowid": str(i), "sku": "X", "title": "很长的标题" * 10} for i in range(500)]

    page = _cap_rows(rows)
    assert len(page["rows"]) == 20
    assert page["total"] == 500
    assert page["has_more"] is True
    assert "title" not in page["rows"][0]  # 默认不带标题

    huge = _cap_rows(rows, limit=99999)
    assert len(huge["rows"]) == 200  # 硬上限 200

    last = _cap_rows(rows, limit=20, offset=480)
    assert len(last["rows"]) == 20
    assert last["has_more"] is False


def test_cap_rows_keeps_title_when_asked():
    from soldeazy_mcp_server.tools import _cap_rows

    rows = [{"rowid": "1", "sku": "abc", "title": "abc"}]
    assert _cap_rows(rows, with_title=True)["rows"][0]["title"] == "abc"


def test_cap_rows_response_is_bounded_in_size():
    """500 条带长标题的数据，裁完后的 JSON 必须远小于全量。"""

    import json

    from soldeazy_mcp_server.tools import _cap_rows

    rows = [
        {
            "rowid": str(i),
            "sku": "JJPC0029",
            "title": "3/8/16 Outlet Surge Protector " * 3,
            "price": 21.9,
            "qty": 100,
        }
        for i in range(500)
    ]
    full = len(json.dumps(rows, ensure_ascii=False))
    capped = len(json.dumps(_cap_rows(rows), ensure_ascii=False))
    assert capped < full / 8


def test_output_guard_blocks_large_payload():
    from soldeazy_mcp_server.tools import _output_too_large

    big = {"rows": [{"x": "y" * 200} for _ in range(100)]}
    blocked = _output_too_large(big)
    assert blocked is not None
    assert blocked["success"] is False
    assert blocked["error"]["code"] == "SOLDEAZY_OUTPUT_TOO_LARGE"
    assert "确认" in blocked["error"]["message"]
    assert blocked["error"]["details"]["estimated_tokens"] > 0


def test_output_guard_allows_when_confirmed():
    from soldeazy_mcp_server.tools import _output_too_large

    big = {"rows": [{"x": "y" * 200} for _ in range(100)]}
    assert _output_too_large(big, allow=True) is None


def test_output_guard_passes_small_payload():
    from soldeazy_mcp_server.tools import _output_too_large

    assert _output_too_large({"ok": True, "items": 8}) is None


def test_list_shops_is_capped_and_hints_keyword(monkeypatch):
    """生产账号 85 个，全量倒进对话很贵：默认只回 20 个并提示用 keyword。"""

    import asyncio

    from soldeazy_mcp_server import tools

    shops = [{"id": str(4000 + i), "name": f"EBAY S{i:02d}"} for i in range(85)]
    shops[10] = {"id": "159", "name": "EBAY FUN"}

    async def fake_list():
        return shops

    monkeypatch.setattr(tools.soldeazy, "list_shop_options", fake_list)

    page = asyncio.run(tools.soldeazy_list_shops())["data"]
    assert page["count"] == 85
    assert len(page["shops"]) == 20
    assert page["has_more"] is True
    assert "keyword" in page["hint"]

    tail = asyncio.run(tools.soldeazy_list_shops(offset=80))["data"]
    assert len(tail["shops"]) == 5
    assert tail["has_more"] is False
    assert "hint" not in tail  # 已经看到尾了就不用再提示

    found = asyncio.run(tools.soldeazy_list_shops(keyword="FUN"))["data"]
    assert found["count"] == 1
    assert found["shops"][0]["id"] == "159"


def test_schema_requires_only_targets_for_change_tools():
    """改数据表工具：prices / quantities 都可选，site 也不强制。"""

    import asyncio

    from soldeazy_mcp_server import tools

    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(tools.mcp.list_tools())}

    # 合并后没有任何必填参数（prices/quantities 至少一个由运行时校验）
    required = schemas["soldeazy_change_datasheets"].get("required") or []
    assert required == []
    assert "prices" in schemas["soldeazy_change_datasheets"]["properties"]
    assert "quantities" in schemas["soldeazy_change_datasheets"]["properties"]

    # 计划工具同理
    plan_required = schemas["soldeazy_plan_changes"].get("required") or []
    assert plan_required == []
    assert "site" not in (schemas["soldeazy_search_datasheet"].get("required") or [])


def test_change_tool_accepts_both_target_files(tmp_path):
    """上千条目标走文件，别让模型逐条生成内联字典。"""

    import inspect

    from soldeazy_mcp_server import tools

    params = inspect.signature(tools.soldeazy_change_datasheets).parameters
    assert "prices_file" in params
    assert "quantities_file" in params
    assert params["prices_file"].default == ""
    assert params["quantities_file"].default == ""

    path = tmp_path / "targets.csv"
    path.write_text("A,10\nB,20\n", encoding="utf-8")

    resolved, info = tools._resolve_targets({}, str(path), kind="price")
    assert resolved == {"A": 10.0, "B": 20.0}
    assert info["targets_count"] == 2
    assert info["targets_from"] == str(path)


def test_targets_and_targets_file_together_is_rejected(tmp_path):
    from soldeazy_mcp_server import tools
    from soldeazy_mcp_server.errors import SoldeazyError

    path = tmp_path / "t.csv"
    path.write_text("A,10\n", encoding="utf-8")

    with pytest.raises(SoldeazyError) as caught:
        tools._resolve_targets({"B": 1.0}, str(path), kind="price")
    assert caught.value.code == "SOLDEAZY_TARGETS_AMBIGUOUS"


def test_targets_file_with_issues_fails_before_any_write(tmp_path):
    """脏文件必须整份拒收，不能带着一半数据去改线上。"""

    from soldeazy_mcp_server import tools
    from soldeazy_mcp_server.errors import SoldeazyError

    path = tmp_path / "bad.csv"
    path.write_text("A=10\nB=abc\n", encoding="utf-8")

    with pytest.raises(SoldeazyError) as caught:
        tools._resolve_targets({}, str(path), kind="price")
    assert caught.value.code == "SOLDEAZY_TARGETS_FILE"
    assert caught.value.details["issues_total"] == 1


def test_targets_file_missing_is_reported(tmp_path):
    from soldeazy_mcp_server import tools
    from soldeazy_mcp_server.errors import SoldeazyError

    with pytest.raises(SoldeazyError) as caught:
        tools._resolve_targets({}, str(tmp_path / "none.csv"), kind="price")
    assert caught.value.code == "SOLDEAZY_TARGETS_FILE"


def test_status_reports_plan_dir_too():
    """登录态和计划各在哪，必须能从一次 status 里看出来。"""

    import asyncio

    from soldeazy_mcp_server import tools

    data = asyncio.run(tools.soldeazy_status())["data"]
    assert "profile_dir" in data
    assert data["plan_dir"].endswith("soldeazy-plans")


def test_default_page_fits_under_the_output_guard():
    """默认 limit 必须落在 4000 字符闸门内，否则闸门等于没有。"""

    from soldeazy_mcp_server.tools import MAX_OUTPUT_CHARS, _cap_rows, _output_too_large

    rows = [
        {
            "row": i + 3,
            "rowid": f"45466{i:03d}",
            "sku": f"SKU{i:04d}",
            "qty": 50,
            "price": 12.5,
            "postage": "0.00",
            "shop": "EBAY FUN",
            "site": "US",
        }
        for i in range(500)
    ]
    payload = {**_cap_rows(rows), "site_notice": "当前未选站点，按全部站点执行"}

    assert len(payload["rows"]) == 20
    # 甚至不需要 allow_large_output 就能正常返回
    assert _output_too_large(payload) is None
    assert len(str(payload)) < MAX_OUTPUT_CHARS


def test_unlisted_plan_turns_sync_shortfall_into_a_warning():
    """FUN 这类全未刊登账号：上传 N 条、同步 0 条是预期内的，不能判失败。"""

    import asyncio

    from soldeazy_mcp_server.providers import soldeazy_apply as mod

    class FakeBrowser:
        """第一次调用是基线（还没有任务），之后冒出同一条同步任务。"""

        def __init__(self) -> None:
            self.calls = 0

        async def queue_jobs(self, limit=20):
            self.calls += 1
            if self.calls == 1:
                return []
            return [
                {
                    "queue_job_idx": "9",
                    "queue_job_act": "DSSync",
                    "queue_job_res": {"total_items": "0", "failure_msgs_count": "0"},
                }
            ]

    strict = asyncio.run(mod._await_latest_job(FakeBrowser(), 8, attempts=1, wait_seconds=0))
    assert strict["ok"] is False
    assert strict["problems"]

    relaxed = asyncio.run(
        mod._await_latest_job(
            FakeBrowser(), 8, tolerate_mismatch=True, attempts=1, wait_seconds=0
        )
    )
    assert relaxed["ok"] is True
    assert relaxed["problems"] == []
    assert relaxed["notes"]


def test_compact_report_surfaces_sync_warnings_without_failing():
    from soldeazy_mcp_server.providers.soldeazy_change import compact_plan_report
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

    plan = ChangePlan(
        plan_id="p", kind="price", site="US", shop={"id": "159", "name": "FUN"},
        created_at="x", items=[PlanItem("1", "S", "price", 21.9, 23.0)],
    )
    applied = {
        "ok": True, "sync": "ok", "sync_problems": [],
        "sync_notes": ["同步了 0 个数据表，上传的是 1 个（差额通常是这些数据表没有在线刊登）"],
        "queue": {"ok": True},
    }
    report = compact_plan_report(
        plan, applied=applied, reconcile={"ok": True, "matched": 1, "items": 1})
    assert report["ok"] is True  # 警告不影响判决
    assert report["problems"] == []
    assert report["warnings_total"] == 1


def test_search_returns_page_limit_and_total_records():
    """分页是隐藏成本：返回里必须真的带 page_limit / total_records。

    用假的搜索页跑一遍，检查**返回的字段**，而不是检查源码里有没有这些名字。
    源码断言在重构后就不再可靠（比如改名成别的方法仍能取到同样的值）。
    """

    import asyncio

    from soldeazy_mcp_server.providers import soldeazy as mod

    class _Locator:
        @property
        def first(self):
            return self

        async def wait_for(self, **_k):
            return None

        async def fill(self, *_a, **_k):
            return None

        async def click(self, **_k):
            return None

        def locator(self, *_a, **_k):
            return self

        def nth(self, *_a, **_k):
            return self

        async def count(self):
            return 2          # 表头 + 一行数据

        async def all_inner_texts(self):
            return ["Row ID", "4569384"]

        async def select_option(self, *_a, **_k):
            return None

        async def is_visible(self):
            return False

    class _Page:
        url = "https://stiger.soldeazy.com/app/soldeazy/datasheet/dsheet_list"

        def locator(self, *_a, **_k):
            return _Locator()

        async def evaluate(self, script, *_a):
            return "200" if "select.limit" in script else "308"

        async def wait_for_load_state(self, *_a, **_k):
            return None

    class _Browser(mod.SoldeazyBrowser):
        def __init__(self) -> None:
            self.settings = type("S", (), {"soldeazy_min_delay": 0})()
            self.base_url = "https://stiger.soldeazy.com"
            self._last_status = ""
            self._last_status_value = ""
            self._last_date = ""
            self._last_date_value = ""
            self._last_sql_token = ""
            self._page = None
            self._context = None

        async def _ensure_page(self):
            return _Page()

        async def _goto(self, *_a, **_k):
            return None

        async def _dismiss_popup(self):
            return None

        async def _locate(self, key, timeout=8000):
            return _Locator()

        async def _clear_date_filter(self):
            return None

        async def _select_status_listed(self):
            return None

        async def _select_status_all(self):
            return None

        async def _select_scope_whole_result(self):
            return None

        async def _select_site(self, site):
            return None

    data = asyncio.run(_Browser()._search_via_page("", "US", "", True))

    # 这两个字段是"让人看见分页成本"的关键，必须在返回里
    assert data["page_limit"] == 200
    assert data["total_records"] == 308
    assert data["row_count"] == len(data["rows"])


# 2026-11-18 删除：`select_datasheets` 已从浏览器里移除（同步接口按完整清单传
# `table_row_ids`，勾选框只是页面取材方式，不值得为它翻几十页 DOM）。
# 对应的逐页勾选测试随之删除。


def test_sync_up_via_api_accepts_explicit_row_ids():
    """跨页勾选后页面上的勾选集合不完整：接口参数必须按完整清单给。"""

    import inspect

    from soldeazy_mcp_server.providers import soldeazy as mod

    sig = inspect.signature(mod.SoldeazyBrowser.sync_up_via_api)
    assert "row_ids" in sig.parameters


def test_search_one_by_one_merges_dedupes_and_reports_missing(monkeypatch):
    """平台合并查询会漏行：逐条搜再合并，重复行去重、空 SKU 明报。"""

    import asyncio

    from soldeazy_mcp_server import tools

    calls: list[str] = []

    async def fake_search(query, site, only_listed=True):
        calls.append(query)
        if "AAA" in query:
            return {"columns": ["Row ID"], "rows": [
                {"rowid": "1", "sku": "AAA", "shop": "FUN"},
                {"rowid": "2", "sku": "AAAUS", "shop": "FUN"},
            ]}
        if "BBB" in query:
            # 与 AAA 的变体行重复，合并时要去掉
            return {"columns": ["Row ID"], "rows": [
                {"rowid": "2", "sku": "AAAUS", "shop": "FUN"}
            ]}
        return {"columns": ["Row ID"], "rows": []}

    monkeypatch.setattr(tools.soldeazy, "search", fake_search)
    monkeypatch.setattr(tools.soldeazy, "active_shop", {"id": "159", "name": "FUN"})

    data = asyncio.run(
        tools._search_one_by_one(["AAA", "BBB", "CCC"], "US", include_unlisted=False)
    )
    assert calls == ["*AAA", "*BBB", "*CCC"]
    assert data["row_count"] == 2
    assert data["search_mode"] == "one_by_one"
    assert data["skus_with_no_rows"] == ["CCC"]
    assert data["skus_with_no_rows_total"] == 1


def test_search_one_by_one_keeps_every_variant_of_a_datasheet(monkeypatch):
    """逐条搜索合并时按 (rowid, sku) 去重，变体展开后不会互相覆盖。

    两个 SKU 落在同一个数据表的不同变体上时，旧逻辑（每数据表一条、
    只留第一个变体）会让第二个 SKU 凭空消失，进而被报成"线上找不到"。
    """

    import asyncio

    from soldeazy_mcp_server import tools

    async def fake_search(query, site, only_listed=True):
        return {
            "columns": ["Row ID"],
            "rows": [
                {"rowid": "77", "sku": "AAA", "shop": "FUN"},
                {"rowid": "77", "sku": "AAA -1", "shop": "FUN", "variant_index": 1},
            ],
        }

    monkeypatch.setattr(tools.soldeazy, "search", fake_search)
    monkeypatch.setattr(tools.soldeazy, "active_shop", {"id": "159", "name": "FUN"})

    data = asyncio.run(tools._search_one_by_one(["AAA"], "US", include_unlisted=False))
    assert [row["sku"] for row in data["rows"]] == ["AAA", "AAA -1"]
    assert data["row_count"] == 2          # 行数
    assert data["datasheet_count"] == 1    # 但只有一个数据表


def test_search_datasheet_one_by_one_routes_around_combined_query(monkeypatch):
    import asyncio

    from soldeazy_mcp_server import tools

    calls: list[str] = []

    async def fake_search(query, site, only_listed=True):
        calls.append(query)
        return {"columns": [], "rows": [
            {"rowid": "1", "sku": query.strip("*"), "shop": "FUN"}
        ]}

    monkeypatch.setattr(tools.soldeazy, "search", fake_search)
    monkeypatch.setattr(tools.soldeazy, "active_shop", {"id": "159", "name": "FUN"})

    result = asyncio.run(
        tools.soldeazy_search_datasheet(skus=["AAA", "BBB"], one_by_one=True)
    )
    assert calls == ["*AAA", "*BBB"]
    assert "search_notice" not in result["data"]
    assert result["data"]["search_mode"] == "one_by_one"

    calls.clear()
    result = asyncio.run(tools.soldeazy_search_datasheet(skus=["AAA", "BBB"]))
    assert calls == ["*AAA *BBB"]  # 默认仍是合并查询
    assert "search_notice" in result["data"]  # 但必须带漏行提醒


def test_expensive_tools_require_confirmation_flag():
    import inspect

    from soldeazy_mcp_server import tools

    for tool in (
        tools.soldeazy_search_datasheet,
        tools.soldeazy_plan_changes,
        tools.soldeazy_apply_plan,
        tools.soldeazy_reconcile_plan,
        tools.soldeazy_change_datasheets,
        tools.soldeazy_change_datasheets,
    ):
        sig = inspect.signature(tool)
        assert "allow_large_output" in sig.parameters
        assert sig.parameters["allow_large_output"].default is False
