"""执行范围必须等于预览范围：全店铺计划要清掉残留的 active_shop。

用户 2026-11-19 定的口径：
    「范围不范围的是用户的事情」—— 工具不加"改动面过大"之类的提醒。

但有一个**功能性**问题与范围判断无关：
计划是"全部店铺"（`plan.shop = {}`）时，`search("")` 会沿用
`browser.active_shop`。如果那个值还留着上次切过的店铺，
导出/同步就只覆盖那一个店 —— 预览说改 1534 个表，实际只改了几十个。

这不是"范围合不合适"，而是"执行的范围 != 用户批准的范围"。
"""

from __future__ import annotations

import asyncio

from soldeazy_mcp_server.providers import soldeazy_apply as mod
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

import pytest


def _plan(shop: dict[str, str]) -> ChangePlan:
    return ChangePlan(
        plan_id="p1",
        kind="price",
        site="",
        shop=shop,
        created_at="x",
        items=[PlanItem("3117213", "FGS100W110V", "price", 27.48, 27.49)],
        only_listed=False,
    )


class _Browser:
    def __init__(self, active: dict[str, str] | None) -> None:
        self.active_shop = active
        self.searches: list[str] = []
        self.exported = False
        self.last_export_path = None
        self.last_apply_export = None
        self.last_export_shops: list[str] = []

    async def search(self, query, site, *, only_listed=True):
        self.searches.append(query)
        return {"total_records": 0, "rows": []}

    async def export_via_api(self, dest):
        self.exported = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 造一份最小可读的 xlsx，供后续 read_matrix
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["dsheet_row_id", "dsheet_shop_initial", "p", "sku", "qty", "price"])
        ws.append(["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"])
        ws.append(["3117213", "FUN", "FGS100W110V", "FGS100W110V", "50", "27.48"])
        wb.save(dest)
        return {"path": str(dest), "size": 1}


def test_all_shops_plan_clears_a_stale_active_shop(tmp_path) -> None:
    """计划没指定店铺 → 执行前必须把 active_shop 清成 None。"""

    browser = _Browser({"id": "159", "name": "FUN"})
    plan = _plan({})
    audit = tmp_path / "a.jsonl"

    asyncio.run(mod._export_and_locale(browser, plan, tmp_path, audit))

    assert browser.active_shop is None, (
        "全店铺计划仍带着 active_shop，导出会只覆盖那一个店"
    )


def test_named_shop_plan_keeps_active_shop(tmp_path) -> None:
    """计划指定了店铺 → 不能清，那会让导出范围变宽。"""

    browser = _Browser({"id": "4432", "name": "LT"})
    plan = _plan({"id": "4432", "name": "LT"})
    audit = tmp_path / "a.jsonl"

    asyncio.run(mod._export_and_locale(browser, plan, tmp_path, audit))

    assert browser.active_shop == {"id": "4432", "name": "LT"}


def test_clearing_happens_even_when_export_is_reused(tmp_path) -> None:
    """复用导出时也要清：同步那一步同样会 `search("")`。"""

    reused = tmp_path / "state_999.xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["dsheet_row_id", "dsheet_shop_initial", "p", "sku", "qty", "price"])
    ws.append(["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"])
    ws.append(["3117213", "FUN", "FGS100W110V", "FGS100W110V", "50", "27.48"])
    wb.save(reused)

    browser = _Browser({"id": "159", "name": "FUN"})
    browser.last_export_path = reused
    plan = _plan({})
    audit = tmp_path / "a.jsonl"

    asyncio.run(mod._export_and_locale(browser, plan, tmp_path, audit))

    assert browser.active_shop is None
    assert browser.exported is False, "有可复用导出时不该重新导出"
