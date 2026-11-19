"""C 方案：合并搜索限定范围 → 导出子集。

用户定案（2026-11-19）：「按 c 实现，排除之前错误实现的干扰统统作废」。

实测依据（FUN/US，目标 SKU 散落在 33 个数据表）：

    A 空查询 + 导全店    36.45s   1636 表 / 7075 行 / 470KB
    C 合并搜索 + 导子集   5.39s     33 表 /  370 行 /  19KB
    且 C 的 price/qty 与 A **逐条一致（370 条 0 差异）**

为什么可行（此前错误结论已作废）：
    - 搜索**按数据表匹配，命中后返回整张表的全部行**
      （实测 *GT-D1-FUN-P → 1 表 / 5 行，5 个子体全在）
    - 所以母体 SKU、子体 SKU 都能直接拼进查询
    - 导出接口的筛选条件取自最近一次 search，导出的必然是同一子集
"""

from __future__ import annotations

from soldeazy_mcp_server.providers.soldeazy_change import (
    MERGED_QUERY_MAX_SKUS,
    merged_query,
)


def test_every_term_has_a_wildcard() -> None:
    """每个词前必须带 `*`。

    不带通配会静默漏数据表（2026-11-12 生产事故：QPCH0003 在合并导出里没有、
    单查能查到）。
    """

    query = merged_query({"FGS100W110V": 28.48, "FGS50W110V": 22.77}, None)

    assert query == "*FGS100W110V *FGS50W110V"
    assert all(term.startswith("*") for term in query.split())


def test_parent_sku_is_included_as_is() -> None:
    """母体 SKU 直接拼进去即可 —— 搜它会带出整表，子体自然都在。

    实测：``*GT-D1-FUN-P`` → 1 个数据表 / 5 行，5 个子体 SKU 全在。
    """

    query = merged_query(None, {"GT-D1-FUN-P": 60})

    assert query == "*GT-D1-FUN-P"


def test_prices_and_quantities_are_merged() -> None:
    """改价（子体）和改量（母体）合并成一次搜索，只需一次导出。"""

    query = merged_query({"FGS100W110V": 28.48}, {"GT-D1-FUN-P": 60})
    terms = query.split()

    assert "*FGS100W110V" in terms
    assert "*GT-D1-FUN-P" in terms
    assert len(terms) == 2


def test_duplicates_are_dropped() -> None:
    """同一个词出现在两处时只搜一次。"""

    assert merged_query({"ABC": 1.0}, {"ABC": 2.0}) == "*ABC"


def test_no_targets_gives_empty_query() -> None:
    """没有目标时返回空串（不按 SKU 过滤），不是 "*"。"""

    assert merged_query(None, None) == ""
    assert merged_query({}, {}) == ""
    assert merged_query({"  ": 1.0}, None) == ""
    assert merged_query({"": 1.0}, {"": 2.0}) == ""


def test_too_many_targets_falls_back_to_full_export() -> None:
    """目标过多时退回导全店，避免查询串过长导致漏数据表。

    返回空串 = 不按 SKU 过滤，与改造前行为一致。
    """

    many = {f"SKU{i:04d}": 1.0 for i in range(MERGED_QUERY_MAX_SKUS + 1)}
    assert merged_query(many, None) == ""

    just_enough = {f"SKU{i:04d}": 1.0 for i in range(MERGED_QUERY_MAX_SKUS)}
    assert merged_query(just_enough, None) != ""


def test_query_for_a_dozen_skus_stays_short() -> None:
    """十几个 SKU 是真实场景，查询串要足够短（实测约 150 字符）。"""

    targets = {f"FGS{100 + i}W110V": 28.48 for i in range(12)}
    query = merged_query(targets, None)

    assert query
    assert len(query) < 400, f"查询串过长: {len(query)}"
