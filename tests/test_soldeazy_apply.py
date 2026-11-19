"""计划构建器的单元测试：SKU 解析、母体展开、冲突不猜。"""

from __future__ import annotations

from soldeazy_mcp_server.providers.soldeazy_apply import build_items, plan_rows
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem


def live_fixture() -> dict:
    return {
        ("4512957", "JJPC0029"): {"rowid": "4512957", "parent": "JJPC0029", "sku": "JJPC0029",
                                  "qty": 100, "price": 21.9},
        ("4522717", "JJPC0028"): {"rowid": "4522717", "parent": "排插-P", "sku": "JJPC0028",
                                  "qty": 100, "price": 17.99},
        ("4522717", "JJPC0029"): {"rowid": "", "parent": "排插-P", "sku": "JJPC0029",
                                  "qty": 100, "price": 21.9},
        ("4522717", "JJPC0030"): {"rowid": "", "parent": "排插-P", "sku": "JJPC0030",
                                  "qty": 100, "price": 16.9},
    }


def test_price_exact_match_writes_only_that_child() -> None:
    items, report = build_items(live_fixture(), "price", {"JJPC0029": 30.0})
    # JJPC0029 出现在两个数据表里，两条都要改
    assert len(items) == 2
    assert {item.rowid for item in items} == {"4512957", "4522717"}
    assert all(item.field == "price" for item in items)
    assert report["missing"] == []


def test_price_skips_unchanged_value() -> None:
    items, _ = build_items(live_fixture(), "price", {"JJPC0028": 17.99})
    assert items == []


def test_price_prefix_no_longer_guesses() -> None:
    """前缀不再被"猜"：写 JJPC002 精确命中不了，就是 missing。

    搜索用 ``*产品代码`` 通配已经返回了全部变体，精确匹配一定够用；
    再用前缀去猜只会在写错时替用户挑候选。所以这里从"报 conflicts 拒写"
    改成"报 missing 让调用方自己看"。
    """

    items, report = build_items(live_fixture(), "price", {"JJPC002": 50.0})

    assert items == []
    assert report["missing"] == ["JJPC002"]
    assert "conflicts" not in report


def test_stock_by_parent_expands_all_children() -> None:
    items, report = build_items(live_fixture(), "qty", {"排插-P": 50})
    assert len(items) == 3
    assert {item.sku for item in items} == {"JJPC0028", "JJPC0029", "JJPC0030"}
    assert all(item.rowid == "4522717" for item in items)
    assert all(item.field == "qty" for item in items)
    assert report["missing"] == []


def test_stock_unknown_parent_is_reported() -> None:
    items, report = build_items(live_fixture(), "qty", {"没有这个母体": 10})
    assert items == []
    assert report["missing"] == ["没有这个母体"]


def test_stock_skips_rows_already_at_target() -> None:
    items, _ = build_items(live_fixture(), "qty", {"排插-P": 100})
    assert items == []


def test_plan_rows_includes_child_rows_of_variant_datasheet() -> None:
    matrix = [
        ["Row ID", "shop", "P.SKU", "SKU", "Qty", "Price", "Post", "EOF"],
        ["4512957", "FUN", "JJPC0029", "JJPC0029", 100, 21.9, 0, 0],
        ["4522717", "FUN", "排插-P", "JJPC0028", 100, 17.99, 0, 0],
        [None, None, "排插-P", "JJPC0029", 100, 21.9, None, "o"],
        [None, None, "排插-P", "JJPC0030", 100, 16.9, None, "o"],
        ["4569384", "FUN", "MCPSKU001", "MCPSKU001", 10, 19.99, None, None],
    ]
    plan = ChangePlan(
        plan_id="p1", kind="price", site="US", shop={"id": "159", "name": "FUN"},
        created_at="2026-11-07", items=[
            PlanItem(rowid="4522717", sku="JJPC0029", field="price", old=21.9, new=30.0),
        ],
    )
    rows = plan_rows(plan, matrix)
    # 母体行 + 它下面两个子体行
    assert rows == [3, 4, 5]
    assert 2 not in rows and 6 not in rows


def test_plan_rows_single_variant_only_that_row() -> None:
    matrix = [
        ["Row ID", "shop", "P.SKU", "SKU", "Qty", "Price", "Post", "EOF"],
        ["4512957", "FUN", "JJPC0029", "JJPC0029", 100, 21.9, 0, 0],
        ["4569384", "FUN", "MCPSKU001", "MCPSKU001", 10, 19.99, None, None],
    ]
    plan = ChangePlan(
        plan_id="p2", kind="qty", site="US", shop={"id": "159", "name": "FUN"},
        created_at="2026-11-07", items=[
            PlanItem(rowid="4512957", sku="JJPC0029", field="qty", old=100, new=50),
        ],
    )
    assert plan_rows(plan, matrix) == [2]


def test_shop_guard_flags_switched_account() -> None:
    from soldeazy_mcp_server.providers.soldeazy_apply import shop_problems
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan

    matrix = [
        ["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku"],
        ["Row ID", "Sales channel", "P.SKU"],
        ["4512957", "FUN", "JJPC0029"],
    ]
    plan = ChangePlan(
        plan_id="p", kind="price", site="US",
        shop={"id": "159", "name": "FUN"}, created_at="2026-11-09",
    )
    # 活动账号还是 FUN -> 不报问题
    assert shop_problems(plan, matrix, {"id": "159", "name": "FUN"}) == []

    # 切到 LT -> 必须报
    problems = shop_problems(plan, matrix, {"id": "4432", "name": "LT"})
    assert problems and "切到" in problems[0]


def test_shop_guard_flags_export_from_other_shop() -> None:
    from soldeazy_mcp_server.providers.soldeazy_apply import shop_problems
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan

    matrix = [
        ["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku"],
        ["Row ID", "Sales channel", "P.SKU"],
        ["4568704", "LT", "2401"],
    ]
    plan = ChangePlan(
        plan_id="p", kind="price", site="US",
        shop={"id": "159", "name": "FUN"}, created_at="2026-11-09",
    )
    problems = shop_problems(plan, matrix, {"id": "159", "name": "FUN"})
    assert any("没有计划所属" in item for item in problems)


def test_shop_guard_accepts_long_and_short_shop_names() -> None:
    """导出列是短名 FUN、页面是长名 EBAY FUN，不该因为格式不同就中止上传。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import shop_problems
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan

    matrix = [
        ["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku"],
        ["Row ID", "Sales channel", "P.SKU"],
        ["4512957", "EBAY FUN", "JJPC0029"],
    ]
    plan = ChangePlan(
        plan_id="p", kind="price", site="US",
        shop={"id": "159", "name": "FUN"}, created_at="2026-11-09",
    )
    assert shop_problems(plan, matrix, {"id": "159", "name": "FUN"}) == []


def test_unlisted_plans_wait_fewer_rounds_for_the_sync_job():
    """未刊登计划平台上基本不产生同步任务：空等轮数必须比已刊登少。

    用**真实调用**验证，不检查源码文本：跑一次 apply_plan，看它传给
    `_await_latest_job` 的 attempts 是多少。这样即使内部重构成别的写法
    （比如把轮数算好后传入），测试也照样有效。
    """

    import asyncio
    import tempfile
    from pathlib import Path

    from soldeazy_mcp_server.providers import soldeazy_apply as mod
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

    # 未刊登计划 **完全不等待**：平台上它们同步不上去（FUN 实机：历史工序任务
    # 全是 DSUploadTask，从来没有同步类任务），等多少轮都是空烧。
    assert mod.SYNC_JOB_ATTEMPTS_UNLISTED == 0
    assert mod.SYNC_JOB_ATTEMPTS == 10

    def _plan(only_listed: bool) -> ChangePlan:
        return ChangePlan(
            plan_id="p", kind="price", site="US",
            shop={"id": "159", "name": "FUN"}, created_at="x",
            items=[PlanItem("4569384", "S", "price", 21.9, 25.0)],
            only_listed=only_listed,
        )

    captured: dict[str, int] = {}

    async def fake_await_latest(browser, expected, *, attempts, **_kwargs):
        captured["attempts"] = attempts
        return None

    class _Browser:
        active_shop = {"id": "159", "name": "FUN"}

        async def search(self, *a, **k):
            return {"rows": [], "total_records": 1}

        async def export_via_api(self, dest):
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.append(["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku",
                       "dsheet_product_code", "dsheet_qty", "dsheet_price"])
            ws.append(["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"])
            ws.append(["4569384", "FUN", "P", "S", "100", "21.9"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            wb.save(dest)
            return {"path": str(dest)}

        async def upload_via_api(self, path):
            return {"success": True, "handle": "9"}

        async def sync_up_via_api(self, **kwargs):
            return {"success": True, "status": 200, "rows": 1,
                    "table_row_ids": ["4569384"], "hints": []}

        async def queue_jobs(self, limit=20):
            return [{"queue_job_idx": "9", "queue_job_enddate": "x",
                     "queue_job_res": {"total_items": "1", "success_msgs_count": "1",
                                       "failure_msgs_count": "0",
                                       "processing_msgs_count": "0"}}]

        async def queue_result(self, job_id):
            return {"rows": [{"rowid": "4569384", "result": "Revise Successful"}]}

    original = mod._await_latest_job
    mod._await_latest_job = fake_await_latest
    try:
        for only_listed, expected in ((True, mod.SYNC_JOB_ATTEMPTS),
                                      (False, mod.SYNC_JOB_ATTEMPTS_UNLISTED)):
            captured.clear()
            asyncio.run(
                mod.apply_plan(
                    _Browser(), _plan(only_listed),
                    Path(tempfile.mkdtemp()), sync=True
                )
            )
            assert captured["attempts"] == expected, (only_listed, captured)
    finally:
        mod._await_latest_job = original


def test_shop_scope_problem_matches_by_plan_shop_name():
    from soldeazy_mcp_server.providers.soldeazy_apply import shop_scope_problem
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan

    plan = ChangePlan(
        plan_id="p", kind="price", site="US", shop={"id": "159", "name": "FUN"},
        created_at="x", items=[],
    )
    assert shop_scope_problem(plan, ["FUN"]) == ""          # 导出列是短名
    assert shop_scope_problem(plan, ["EBAY FUN"]) == ""     # 搜索结果里是长名
    assert "FUN" in shop_scope_problem(plan, ["EBAY TAY"])
    # 拿不到店铺信息时不下结论，交给原来的逐行比对
    assert shop_scope_problem(plan, []) == ""
    assert shop_scope_problem(plan, None) == ""


def test_reconcile_aborts_when_export_is_from_another_shop(tmp_path):
    """没设 active_shop 时导出的是平台默认店铺，不能据此判"改动没生效"。"""

    import asyncio

    from soldeazy_mcp_server.providers.soldeazy_apply import reconcile
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

    plan = ChangePlan(
        plan_id="price-shop", kind="price", site="US",
        shop={"id": "159", "name": "FUN"}, created_at="x",
        items=[PlanItem("4512957", "JJPC0029", "price", 21.9, 23.0)],
    )

    class FakeBrowser:
        last_export_shops = ["EBAY TAY"]

        async def search(self, *args, **kwargs):
            return {"rows": []}

        async def read_datasheet_values(self, *args, **kwargs):
            return {}

    report = asyncio.run(reconcile(FakeBrowser(), plan, tmp_path))

    assert report["ok"] is False
    assert report["absent_total"] == 0        # 不是"查不到"
    assert "对账中止" in report["error"]
    assert report["shops_in_export"] == ["EBAY TAY"]
    assert (tmp_path / "price-shop.reconcile.json").is_file()


def test_reconcile_matches_normally_when_shop_is_right(tmp_path):
    import asyncio

    from soldeazy_mcp_server.providers.soldeazy_apply import reconcile
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

    plan = ChangePlan(
        plan_id="price-ok", kind="price", site="US",
        shop={"id": "159", "name": "FUN"}, created_at="x",
        items=[PlanItem("4512957", "JJPC0029", "price", 21.9, 23.0)],
    )

    class FakeBrowser:
        last_export_shops = ["EBAY FUN"]

        async def search(self, *args, **kwargs):
            return {"rows": []}

        async def read_datasheet_values(self, *args, **kwargs):
            return {("4512957", "JJPC0029"): {"price": 23.0, "qty": 50}}

    report = asyncio.run(reconcile(FakeBrowser(), plan, tmp_path))

    assert report["ok"] is True
    assert report["matched"] == 1
    assert "error" not in report


def test_compact_report_reports_shop_mistake_instead_of_zero_mismatch():
    from soldeazy_mcp_server.providers.soldeazy_change import compact_plan_report
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

    plan = ChangePlan(
        plan_id="p", kind="price", site="US", shop={"id": "159", "name": "FUN"},
        created_at="x", items=[PlanItem("1", "S", "price", 21.9, 23.0)],
    )
    reconcile = {
        "ok": False, "matched": 0, "items": 1, "mismatched_total": 0, "absent_total": 0,
        "error": "对账中止：导出的是 ['EBAY TAY']，计划属于 FUN。",
    }
    report = compact_plan_report(
        plan, applied={"ok": True, "queue": {"ok": True}}, reconcile=reconcile)

    assert report["ok"] is False
    assert any("对账中止" in p for p in report["problems"])
    assert not any("对账不一致" in p for p in report["problems"])


def test_apply_plan_signature_has_sync_switches():
    import inspect

    from soldeazy_mcp_server.providers.soldeazy_apply import apply_plan

    params = inspect.signature(apply_plan).parameters
    assert params["sync"].default is True
    assert params["sync_price"].default is True
    assert params["sync_quantity"].default is False


def test_desktop_ua_has_no_headless_token():
    from soldeazy_mcp_server.providers.soldeazy import DESKTOP_UA

    assert "Headless" not in DESKTOP_UA
    assert DESKTOP_UA.startswith("Mozilla/5.0")
    assert "Chrome/" in DESKTOP_UA


def test_sync_job_reports_count_mismatch():
    """同步条数少于上传条数时要说明原因，而不是静默放过。"""

    import asyncio

    from soldeazy_mcp_server.providers import soldeazy_apply as mod

    class FakeBrowser:
        def __init__(self) -> None:
            self.calls = 0

        async def queue_jobs(self, limit: int = 20):
            self.calls += 1
            if self.calls == 1:
                return [{"queue_job_idx": "old"}]
            return [
                {"queue_job_idx": "old", "queue_job_act": "DSUploadTask"},
                {
                    "queue_job_idx": "new",
                    "queue_job_act": "DSDalignTask",
                    "queue_job_res": {
                        "total_items": "22",
                        "success_msgs_count": "22",
                        "failure_msgs_count": "0",
                    },
                },
            ]

    result = asyncio.run(mod._await_latest_job(FakeBrowser(), expected=25, attempts=2, wait_seconds=0))
    assert result is not None
    assert result["total_items"] == "22"
    assert result["ok"] is False
    assert any("没有在线刊登" in p for p in result["problems"])


def test_sync_status_says_unverified_when_no_queue_was_observed():
    """同步接口回 200 但工序队列里没有任务时，不能报 ``ok``。

    实测（FUN，未刊登数据表）：同步接口成功、上传也对，但根本不会产生
    同步工序。原来 ``_sync_status`` 只看「没有 problems」就返回 ``ok``，
    于是「一条都没核对」被显示成「同步成功」。
    """

    from soldeazy_mcp_server.providers.soldeazy_apply import _sync_status

    assert _sync_status(None, []) == "skipped"
    assert _sync_status({"success": True}, []) == "unverified"
    assert _sync_status({"success": True, "queue": {}}, []) == "unverified"
    # 有 queue 但没算出 planned（说明没观察到任务）同样是未核对
    assert _sync_status({"queue": {"notes": ["x"]}}, []) == "unverified"


def test_sync_status_is_ok_only_with_an_observed_queue():
    from soldeazy_mcp_server.providers.soldeazy_apply import _sync_status

    observed = {"queue": {"planned": 5, "synced": ["1", "2", "3", "4", "5"]}}
    assert _sync_status(observed, []) == "ok"
    assert _sync_status(observed, ["有 1 条失败"]) == "partial"


def test_sync_detail_is_empty_when_nothing_was_planned():
    """没观察到任务时不要吐一堆 0，那会让人以为「同步了 0 条」。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import _sync_detail

    assert _sync_detail(None) == {}
    assert _sync_detail({"success": True}) == {}
    assert _sync_detail({"queue": {}}) == {}


def test_sync_detail_counts_each_shortfall_class():
    from soldeazy_mcp_server.providers.soldeazy_apply import _sync_detail

    detail = _sync_detail(
        {
            "queue": {
                "planned": 10,
                "job_rows": 8,
                "synced": ["a", "b", "c", "d", "e", "f"],
                "skipped_no_listing": ["g"],
                "failed": ["h"],
                "not_in_job": ["i", "j"],
            }
        }
    )
    assert detail["planned"] == 10
    assert detail["job_rows"] == 8
    assert detail["synced_count"] == 6
    assert detail["skipped_no_listing_count"] == 1
    assert detail["failed_count"] == 1
    assert detail["not_in_job_count"] == 2
    assert detail["not_in_job_sample"] == ["i", "j"]


def test_trim_ids_keeps_sample_and_total():
    from soldeazy_mcp_server.providers.soldeazy_apply import _trim_ids

    ids = [str(4523000 + i) for i in range(2557)]
    trimmed = _trim_ids(ids)
    assert len(trimmed["rowids"]) == 20
    assert trimmed["rowids_total"] == 2557
    assert trimmed["rowids_truncated"] is True

    small = _trim_ids(["1", "2"])
    assert small["rowids"] == ["1", "2"]
    assert small["rowids_truncated"] is False


def test_trim_ids_caps_token_cost():
    """2557 条完整列表约 1.4 万 token，截断后应降到百位数以内。"""

    import json

    from soldeazy_mcp_server.providers.soldeazy_apply import _trim_ids

    ids = [str(4523000 + i) for i in range(2557)]
    raw = json.dumps(ids)
    trimmed = json.dumps(_trim_ids(ids), ensure_ascii=False)
    assert len(trimmed) < len(raw) / 20


def _live_rows(rows):
    return {(r["rowid"], r["sku"]): r for r in rows}


MIXED_ROWS = [
    {"rowid": "1001", "sku": "ZMFL0012", "parent": "ZMFL0012", "price": 24.9, "qty": 100},
    {"rowid": "1002", "sku": "ZMFL0012 x2", "parent": "ZMFL0012", "price": 48.9, "qty": 100},
    {"rowid": "1003", "sku": "ZMFL0012 x4", "parent": "ZMFL0012", "price": 95.9, "qty": 100},
]


def test_split_pack_recognises_suffix():
    from soldeazy_mcp_server.providers.soldeazy_apply import split_pack

    assert split_pack("ZMHB0004US x4") == ("ZMHB0004US", 4)
    assert split_pack("ZMFL0012x2") == ("ZMFL0012", 2)
    assert split_pack("ZMFL0012") == ("ZMFL0012", None)
    assert split_pack("EF-1374310") == ("EF-1374310", None)


def test_base_sku_changes_unit_only_and_reports_siblings():
    """基础 SKU 只改明确写出的那一行；同母体的套装**报给人工确认**，不自动改。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import build_items

    items, report = build_items(_live_rows(MIXED_ROWS), "price", {"ZMFL0012": 26.0})

    assert [i.sku for i in items] == ["ZMFL0012"]
    assert items[0].new == 26.0
    # 有套装存在 → 交人工判断，但一行都不替人改
    assert report["needs_confirmation"] == {
        "ZMFL0012": ["ZMFL0012 x2", "ZMFL0012 x4"]
    }


def test_explicit_pack_changes_only_that_pack():
    from soldeazy_mcp_server.providers.soldeazy_apply import build_items

    items, report = build_items(_live_rows(MIXED_ROWS), "price", {"ZMFL0012 x2": 58.9})

    assert [i.sku for i in items] == ["ZMFL0012 x2"]
    assert items[0].new == 58.9
    # 同母体下还有单件和 x4，同样要报给人工看
    assert report["needs_confirmation"]["ZMFL0012 x2"] == ["ZMFL0012", "ZMFL0012 x4"]


def test_explicit_pack_requires_exact_sku():
    """套装必须写对：``ZMFL0012x4``（无空格）不再被自动归一。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import build_items

    items, report = build_items(_live_rows(MIXED_ROWS), "price", {"ZMFL0012x4": 100.0})

    assert items == []
    assert report["missing"] == ["ZMFL0012x4"]


def test_base_sku_never_falls_back_to_single_pack():
    """只有套装、没有单件时，基础 SKU 报缺失，绝不替用户改套装。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import build_items

    only_packs = _live_rows(MIXED_ROWS[1:])
    items, report = build_items(only_packs, "price", {"ZMFL0012": 30.0})

    assert items == []
    assert report["missing"] == ["ZMFL0012"]


def test_two_targets_unit_and_pack_together():
    from soldeazy_mcp_server.providers.soldeazy_apply import build_items

    items, _report = build_items(
        _live_rows(MIXED_ROWS), "price", {"ZMFL0012": 26.0, "ZMFL0012 x2": 52.0}
    )

    changed = {i.sku: i.new for i in items}
    assert changed == {"ZMFL0012": 26.0, "ZMFL0012 x2": 52.0}
