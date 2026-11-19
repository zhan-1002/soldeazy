"""改动方向单一且面很广时，必须显式提醒——这是"参数写错"的典型形态。

实测教训（2026-11-18 实机预览）：拿 0.01 当测试值跑 10 个数据表的改价，
判决里只有"作用范围"和"文件在哪"两句，**没有一句提示"你正在把 10 个数据表
全部降价"** —— 最该被看见的信号反而藏在概览表里，靠人自己读出来。

所以补一条显式提醒。但只在真的可疑时才提醒，否则会变噪音：
- 条数 >= DIRECTION_ALERT_MIN_ROWS（改 1-2 条本来就是单方向，很常见）
- 方向完全一致（有涨有降 = 正常）
"""

from __future__ import annotations

import pytest

from soldeazy_mcp_server.providers.soldeazy_change import (
    DIRECTION_ALERT_MIN_ROWS,
    build_preview_summary,
)
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem


def _plan(pairs: list[tuple[float, float]], *, field: str = "price",
          rowids: list[str] | None = None) -> ChangePlan:
    items = [
        PlanItem(
            (rowids[i] if rowids else f"45{i:05d}"),
            f"SKU{i:03d}", field, old, new,
        )
        for i, (old, new) in enumerate(pairs)
    ]
    return ChangePlan(
        plan_id="p", kind=field, site="US",
        shop={"id": "159", "name": "FUN"}, created_at="x", items=items,
    )


def _summary(plan, **kwargs):
    base = {
        "shop": {"id": "159", "name": "FUN"},
        "site": "US",
        "only_listed": False,
        "needs_confirmation": {},
        "missing": [],
        "shops_in_export": ["FUN"],
    }
    base.update(kwargs)
    return build_preview_summary(plan, **base)


def _direction_lines(data) -> list[str]:
    return [line for line in data["must_confirm"] if "改动方向单一" in line]


def test_all_decreasing_triggers_the_alert() -> None:
    """10 个数据表全部降价 —— 正是"测试值写错"的形态，必须提醒。"""

    data = _summary(_plan([(21.90, 0.01)] * 10))
    lines = _direction_lines(data)

    assert len(lines) == 1
    assert "全部下调" in lines[0]
    assert "10 条" in lines[0]
    assert "10 个数据表" in lines[0]


def test_all_increasing_triggers_the_alert() -> None:
    data = _summary(_plan([(10.0, 99.0)] * 5))
    lines = _direction_lines(data)

    assert len(lines) == 1
    assert "全部上调" in lines[0]


def test_extreme_ratio_is_called_out() -> None:
    """降到原价的几个百分点，几乎一定是小数点写错了。"""

    data = _summary(_plan([(21.90, 0.01)] * 4))
    lines = _direction_lines(data)

    assert lines
    assert "原价的" in lines[0]


def test_mixed_direction_does_not_trigger() -> None:
    """有涨有降是正常改动，不该提醒。"""

    data = _summary(_plan([(10.0, 20.0), (30.0, 25.0), (5.0, 6.0)]))
    assert _direction_lines(data) == []


def test_below_threshold_does_not_trigger() -> None:
    """只改 1-2 条时，单方向很常见，提醒只会变噪音。"""

    assert DIRECTION_ALERT_MIN_ROWS >= 3
    data = _summary(_plan([(10.0, 20.0)] * (DIRECTION_ALERT_MIN_ROWS - 1)))
    assert _direction_lines(data) == []


def test_exactly_at_threshold_triggers() -> None:
    data = _summary(_plan([(10.0, 20.0)] * DIRECTION_ALERT_MIN_ROWS))
    assert len(_direction_lines(data)) == 1


def test_same_datasheets_are_counted_once() -> None:
    """多条改动落在同一个数据表上时，数据表数按去重算。"""

    # 5 条改动，全部落在同一个 rowid
    data = _summary(_plan([(10.0, 20.0)] * 5, rowids=["4512957"] * 5))
    lines = _direction_lines(data)

    assert lines
    assert "5 条" in lines[0]
    assert "1 个数据表" in lines[0]


def test_quantity_direction_also_alerted() -> None:
    """数量方向单一同样要提醒（不只是价格）。"""

    data = _summary(_plan([(100.0, 0.0)] * 4, field="qty"))
    lines = _direction_lines(data)

    assert lines
    assert "数量" in lines[0]
    assert "全部下调" in lines[0]


def test_both_fields_can_alert_independently() -> None:
    """价格全降 + 数量全涨：两条提醒都要有。"""

    items = [
        *[PlanItem(f"45{i:05d}", f"S{i}", "price", 20.0, 10.0) for i in range(3)],
        *[PlanItem(f"46{i:05d}", f"Q{i}", "qty", 10.0, 50.0) for i in range(3)],
    ]
    plan = ChangePlan(
        plan_id="p", kind="price_qty", site="US",
        shop={"id": "159", "name": "FUN"}, created_at="x", items=items,
    )
    data = _summary(plan)
    lines = _direction_lines(data)

    assert len(lines) == 2
    assert any("价格" in line and "全部下调" in line for line in lines)
    assert any("数量" in line and "全部上调" in line for line in lines)


@pytest.mark.parametrize("count", [3, 5, 20])
def test_alert_present_at_various_sizes(count: int) -> None:
    data = _summary(_plan([(10.0, 5.0)] * count))
    assert len(_direction_lines(data)) == 1
