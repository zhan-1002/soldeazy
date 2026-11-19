"""价格与数量是同一次授权的两个结果，不是两条链路。

用户纠正的原话：**"价格和库存，只是一次授权行为中产生的两个结果。他们相互依存，
我们只是不希望改这两者会发生误改，不代表让他们隔阂如此之深，提高了系统复杂度。"**

所以这里守住的是"合在一起，但不会互相污染"：

1. 一个入口、一份计划、一次上传、一次同步 —— 不是三个工具；
2. 但**两路的匹配规则各归各的**：价格按子体精确、数量按母体展开，
   一边的失败不能污染另一边的成功；
3. 防误改靠**匹配规则**（套装需显式 xN、值没变不写），不靠拆工具。
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from soldeazy_mcp_server import tools
from soldeazy_mcp_server.providers.soldeazy_apply import build_items

LIVE = {
    ("1001", "SKU-A"): {
        "rowid": "1001", "parent": "P-A", "sku": "SKU-A",
        "qty": "100", "price": "10.00",
    },
    ("1001", "SKU-A-US"): {
        "rowid": "1001", "parent": "P-A", "sku": "SKU-A-US",
        "qty": "100", "price": "11.00",
    },
    ("1001", "SKU-A x2"): {
        "rowid": "1001", "parent": "P-A", "sku": "SKU-A x2",
        "qty": "100", "price": "20.00",
    },
}


def test_only_one_write_entry_exists() -> None:
    """三个入口合并成一个：不再有 change_price / change_stock。"""

    names = set(tools.mcp._tool_manager._tools)
    assert "soldeazy_change_datasheets" in names
    for retired in (
        "soldeazy_change_price",
        "soldeazy_change_stock",
        "soldeazy_change_price_and_stock",
    ):
        assert retired not in names, f"{retired} 应该已合并进 soldeazy_change_datasheets"


def test_price_and_quantity_are_symmetric_optional_params() -> None:
    """两个入参地位对等，谁都不比谁"主"。"""

    sig = inspect.signature(tools.soldeazy_change_datasheets)
    params = list(sig.parameters)
    assert params.index("prices") == 0
    assert params.index("quantities") == 1
    assert sig.parameters["prices"].default is None
    assert sig.parameters["quantities"].default is None

    # 计划工具同样对称
    psig = inspect.signature(tools.soldeazy_plan_changes)
    assert psig.parameters["prices"].default is None
    assert psig.parameters["quantities"].default is None


@pytest.mark.parametrize(
    ("prices", "quantities"),
    [(None, None), ({}, {})],
)
def test_at_least_one_target_is_required(prices, quantities) -> None:
    """两个都不给要报错——但不能强制必须给两个。"""

    result = asyncio.run(
        tools.soldeazy_change_datasheets(prices=prices, quantities=quantities)
    )
    assert result["success"] is False
    assert result["error"]["code"] == "SOLDEAZY_PLAN_EMPTY"


def test_price_path_only_touches_unit_row_and_reports_siblings() -> None:
    """改价只写明确点名的那一行；同母体还有套装时报给人工确认，不自动改。"""

    items, report = build_items(LIVE, "price", {"SKU-A": 12.0})

    touched = {item.sku for item in items}
    assert "SKU-A" in touched
    assert "SKU-A x2" not in touched          # 套装绝不被"顺手"改掉
    assert report["needs_confirmation"]["SKU-A"] == ["SKU-A x2"]  # 但要让人工看见


def test_quantity_path_expands_all_children_of_the_parent() -> None:
    """改量：按母体展开，该母体下全部子体行都写，含套装行。"""

    items, _ = build_items(LIVE, "qty", {"P-A": 50})

    touched = {item.sku for item in items}
    assert touched == {"SKU-A", "SKU-A-US", "SKU-A x2"}
    assert all(item.field == "qty" for item in items)


def test_two_paths_do_not_contaminate_each_other() -> None:
    """关键的"相互依存但不互相污染"：合并后各自只产出自己的 field。"""

    price_items, _ = build_items(LIVE, "price", {"SKU-A": 12.0})
    qty_items, _ = build_items(LIVE, "qty", {"P-A": 50})
    merged = [*price_items, *qty_items]

    price_fields = {i.field for i in price_items}
    qty_fields = {i.field for i in qty_items}
    assert price_fields == {"price"}
    assert qty_fields == {"qty"}

    # 合并后同一个 SKU 可以同时有两条记录（价格一条、数量一条）
    sku_a = [i for i in merged if i.sku == "SKU-A"]
    assert {i.field for i in sku_a} == {"price", "qty"}


def test_target_already_at_value_is_not_written() -> None:
    """两路都遵守同一个幂等规则：值没变就不写。"""

    price_items, _ = build_items(LIVE, "price", {"SKU-A": 10.00})   # 现值就是 10
    qty_items, _ = build_items(LIVE, "qty", {"P-A": 100})           # 现值就是 100

    assert price_items == []
    assert qty_items == []


def test_unknown_target_reports_missing_without_aborting_the_other_side() -> None:
    """一边找不到，不能连累另一边。"""

    price_items, price_report = build_items(LIVE, "price", {"NOT-EXIST": 9.9})
    qty_items, _ = build_items(LIVE, "qty", {"P-A": 50})

    assert price_items == []
    assert price_report["missing"] == ["NOT-EXIST"]
    assert len(qty_items) == 3          # 数量侧照常产出


def test_tool_refuses_when_write_disabled_even_mixing_both() -> None:
    """合并后仍守写开关：两个都给了也不许绕过。"""

    result = asyncio.run(
        tools.soldeazy_change_datasheets(
            prices={"SKU-A": 12.0},
            quantities={"P-A": 50},
            dry_run=False,
        )
    )
    assert result["success"] is False
    assert result["error"]["code"] in {
        "SOLDEAZY_WRITE_DISABLED",
        "SOLDEAZY_SHOP_REQUIRED",
    }


def test_dry_run_is_still_the_default() -> None:
    """防误改的第一道闸没被合并削弱。"""

    sig = inspect.signature(tools.soldeazy_change_datasheets)
    assert sig.parameters["dry_run"].default is True
