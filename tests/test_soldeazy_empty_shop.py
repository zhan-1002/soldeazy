"""站点和店铺都留空时必须能正常执行，不能报 SHOP_REQUIRED。

用户 2026-11-19 的判定：

    工作区 AI 反复追问「同一 SKU 在多个店铺都有，改价范围怎么定」，
    而根因是工具直接拒绝：不给 shop 就抛 SOLDEAZY_SHOP_REQUIRED。

平台语义本来就是「不选店铺 = 全部店铺」，和搜索一致。
按全部店铺执行不危险：返回里的 ``preview.作用范围`` 会如实列出命中的账号。
"""

from __future__ import annotations

import asyncio

from soldeazy_mcp_server import tools
from soldeazy_mcp_server.errors import SoldeazyError


class _FakeBrowser:
    """最小替身：只记录 shop 解析结果。"""

    active_shop: dict[str, str] | None = None


def test_empty_shop_resolves_to_all_shops_not_an_error() -> None:
    """没给 shop、也没有 active_shop → 返回空 dict（= 全部店铺）。"""

    original = tools.soldeazy.active_shop
    tools.soldeazy.active_shop = None
    try:
        resolved = asyncio.run(tools._resolve_shop_id(""))
    finally:
        tools.soldeazy.active_shop = original

    assert resolved == {}


def test_empty_shop_falls_back_to_active_shop() -> None:
    """没给 shop 但有 active_shop → 用它（保持原有行为）。"""

    original = tools.soldeazy.active_shop
    tools.soldeazy.active_shop = {"id": "159", "name": "FUN"}
    try:
        resolved = asyncio.run(tools._resolve_shop_id(""))
    finally:
        tools.soldeazy.active_shop = original

    assert resolved == {"id": "159", "name": "FUN"}


def test_switch_shop_still_requires_a_name() -> None:
    """显式调用 switch_shop("") 仍应报错——那不是"全部店铺"的入口。

    两者语义不同：``_resolve_shop_id`` 处理"这次调用没指定"，
    ``switch_shop`` 是"主动切到某个账号"，空值没有意义。
    """

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    browser = SoldeazyBrowser(Settings.load())
    try:
        try:
            asyncio.run(browser.switch_shop(""))
        except SoldeazyError as exc:
            assert exc.code == "SOLDEAZY_SHOP_REQUIRED"
        else:  # pragma: no cover - 不应走到
            raise AssertionError("switch_shop('') 应该报错")
    finally:
        pass


def test_plan_tolerates_an_empty_shop() -> None:
    """计划里 shop 为空 dict 时，店铺校验应该跳过而不是报错。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import shop_problems
    from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem

    plan = ChangePlan(
        plan_id="p",
        kind="price",
        site="",
        shop={},                       # ← 全部店铺
        created_at="x",
        items=[PlanItem("3117213", "FGS100W110V", "price", 27.48, 27.49)],
        only_listed=False,
    )
    matrix = [
        ["dsheet_row_id", "dsheet_shop_initial", "p", "sku", "qty", "price"],
        ["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"],
        ["3117213", "FUN", "FGS100W110V", "FGS100W110V", "50", "27.48"],
    ]

    # 没有期望店铺，就无从比对，不该报错
    assert shop_problems(plan, matrix, {"id": "159", "name": "FUN"}) == []
