"""匹配只做精确命中；套装交人工确认，系统不替人决定。

用户 2026-11-18 的判断：

> 防止误改机制关于这些匹配的地方，后面证实是无用功。搜索默认为搜索的产品代码
> 加 ``*`` 前缀，系统会返回这些产品代码的变体和人工的操作保持一致。在这个语境下，
> 防误改机制反而成为了累赘。

以及他对两个追问的答复：

- 前缀猜测 → **让人工判断这个套装逻辑即可**
- 套装行为 → **判断出有，让人工确认**

所以这里守住的新边界：

1. **不做前缀猜测**：写错就是 missing，不替用户挑"最像的"候选；
2. **不做 SKU 归一化**：``ZMFL0012x4`` 不再被猜成 ``ZMFL0012 x4``；
3. **套装不自动改**，但必须**报出来让人工决定**（``needs_confirmation``）；
4. 这些都不影响"绝不误改"这个底线——因为不猜，就不会猜错。
"""

from __future__ import annotations

from soldeazy_mcp_server.providers.soldeazy_apply import build_items

LIVE = {
    ("1001", "ZMFL0012"): {
        "rowid": "1001", "parent": "P", "sku": "ZMFL0012", "qty": "100", "price": "21.90",
    },
    ("1001", "ZMFL0012 x2"): {
        "rowid": "1001", "parent": "P", "sku": "ZMFL0012 x2", "qty": "100", "price": "43.80",
    },
    ("1001", "ZMFL0012 x4"): {
        "rowid": "1001", "parent": "P", "sku": "ZMFL0012 x4", "qty": "100", "price": "87.60",
    },
    ("1002", "JJPC0028"): {
        "rowid": "1002", "parent": "P2", "sku": "JJPC0028", "qty": "100", "price": "17.99",
    },
    ("1002", "JJPC0029"): {
        "rowid": "1002", "parent": "P2", "sku": "JJPC0029", "qty": "100", "price": "21.90",
    },
}


def test_exact_match_needs_no_guessing() -> None:
    """搜索返回了全部变体，所以精确写 SKU 一定能命中——这就是"不需要猜"的前提。"""

    items, report = build_items(LIVE, "price", {"ZMFL0012": 26.0})

    assert [i.sku for i in items] == ["ZMFL0012"]
    assert report["missing"] == []


def test_prefix_is_not_expanded_into_candidates() -> None:
    """写 ``JJPC002`` 不会命中 0028/0029：报 missing，不猜、也不拒写。"""

    items, report = build_items(LIVE, "price", {"JJPC002": 20.0})

    assert items == []
    assert report["missing"] == ["JJPC002"]
    # 关键：没有 conflicts 这个概念了
    assert "conflicts" not in report


def test_sku_is_not_normalised() -> None:
    """``ZMFL0012x4``（少个空格）不再被自动认成 ``ZMFL0012 x4``。"""

    items, report = build_items(LIVE, "price", {"ZMFL0012x4": 100.0})

    assert items == []
    assert report["missing"] == ["ZMFL0012x4"]


def test_pack_is_never_touched_without_being_named() -> None:
    """不点名的套装一行都不改——这是"绝不误改"的底线。"""

    items, _ = build_items(LIVE, "price", {"ZMFL0012": 26.0})

    changed = {i.sku for i in items}
    assert "ZMFL0012 x2" not in changed
    assert "ZMFL0012 x4" not in changed
    # x4 的价格 87.60 绝不能被写成 26.00 的倍数或原值
    assert all(i.new == 26.0 for i in items)


def test_siblings_are_reported_for_human_judgement() -> None:
    """有套装存在时，必须报出来让人工判断要不要一起改。"""

    _, report = build_items(LIVE, "price", {"ZMFL0012": 26.0})

    assert report["needs_confirmation"] == {
        "ZMFL0012": ["ZMFL0012 x2", "ZMFL0012 x4"]
    }


def test_naming_a_pack_also_reports_its_siblings() -> None:
    """点了 x2，同样要把单件和 x4 报给人工——两边对称。"""

    _, report = build_items(LIVE, "price", {"ZMFL0012 x2": 50.0})

    assert report["needs_confirmation"]["ZMFL0012 x2"] == [
        "ZMFL0012",
        "ZMFL0012 x4",
    ]


def test_no_siblings_means_no_confirmation_noise() -> None:
    """没有同母体变体时不要产生噪音。"""

    _, report = build_items(LIVE, "price", {"JJPC0028": 20.0})

    assert report["needs_confirmation"] == {}


def test_unrelated_skus_are_never_grouped() -> None:
    """不同母体的 SKU 不算兄弟：改 JJPC0028 不该提到 JJPC0029。"""

    _, report = build_items(LIVE, "price", {"JJPC0028": 20.0})

    assert report["needs_confirmation"] == {}


def test_quantity_path_unchanged_by_pack_logic() -> None:
    """库存按母体展开是语义本身，不属于"套装判断"，行为不变。"""

    items, report = build_items(LIVE, "qty", {"P": 50})

    assert {i.sku for i in items} == {"ZMFL0012", "ZMFL0012 x2", "ZMFL0012 x4"}
    assert report["needs_confirmation"] == {}


def test_report_has_only_two_keys_now() -> None:
    """报告面收窄：从 3 个键（conflicts/skipped_packs/missing）降到 2 个。"""

    _, report = build_items(LIVE, "price", {"ZMFL0012": 26.0})

    assert set(report) == {"missing", "needs_confirmation"}
