"""对账只读计划涉及的数据表，不导全店。

用户的关键判断（2026-11-18）：
    「我们的方式是不是只上传改动的部分，那就不需要全量的对账」

上传文件早就收窄到 keep_rows（只含计划涉及的数据表），
唯独对账还在导 1636 个数据表 —— 每轮 18 秒，最多 3 轮。

实测支撑（2026-11-18）：
    单个 rowid  '3117213'              → 1 个数据表，精确命中
    多个 rowid  '3117213 3117211 ...'  → 3 个数据表，全部命中
    **带通配**  '*3117213'             → 0 个（通配反而失败）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from soldeazy_mcp_server.providers import soldeazy_apply as mod
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem


def _plan(rowids: list[str], *, skus: list[str] | None = None) -> ChangePlan:
    names = skus or [f"S{i}" for i in range(len(rowids))]
    items = [
        PlanItem(rowid, names[index % len(names)], "price", 9.9, 12.5)
        for index, rowid in enumerate(rowids)
    ]
    return ChangePlan(
        plan_id="p",
        kind="price",
        site="US",
        shop={"id": "159", "name": "FUN"},
        created_at="x",
        items=items,
    )


class _Browser:
    """记录 reconcile 传下来的 query，并返回可对账的假现值。"""

    def __init__(self, plan: ChangePlan) -> None:
        self.plan = plan
        self.queries: list[str] = []
        self.active_shop = {"id": "159", "name": "FUN"}
        self.last_export_shops = ["FUN"]
        self.last_export_warnings: list[str] = []
        self.respond_only_calls: list[bool] = []

    async def search(self, query, site, *, only_listed=True):
        self.queries.append(query)
        return {"total_records": 0}

    async def read_datasheet_values(
        self, site="US", query="", *, search=True, only_listed=True,
        allow_batches=False, respond_only=False,
    ):
        self.queries.append(query)
        self.respond_only_calls.append(respond_only)
        return {
            (item.rowid, item.sku): {
                "rowid": item.rowid,
                "sku": item.sku,
                "parent": item.sku,
                "price": item.new,
                "qty": 10,
            }
            for item in self.plan.items
        }


def test_reconcile_reads_only_the_planned_datasheets(tmp_path) -> None:
    """读的范围必须是计划里的 rowid，而不是空查询（全店）。"""

    plan = _plan(["3117213", "3117211", "3117210"])
    browser = _Browser(plan)

    report = asyncio.run(mod.reconcile(browser, plan, tmp_path, attempts=1, wait_seconds=0))

    assert report["ok"] is True
    assert report["matched"] == 3
    # 至少有一次用的是 rowid 查询，且**不是**空查询
    rowid_queries = [q for q in browser.queries if q]
    assert rowid_queries, f"没有任何 rowid 查询，实际: {browser.queries}"
    assert all(q != "" for q in rowid_queries[-1:]), "最后一轮仍在导全店"


def test_reconcile_does_not_search_twice_per_round(tmp_path) -> None:
    """每轮只该搜一次（重发刷新），不该先 search 再在读取里又 search。

    用户 2026-11-19 指出：搜索参数已经在平台上，对账只需要重发同样的搜索
    刷新值，不必重新"建立"搜索条件再读一遍。
    """

    plan = _plan(["3117213"])
    browser = _Browser(plan)

    asyncio.run(mod.reconcile(browser, plan, tmp_path, attempts=1, wait_seconds=0))

    # 这一轮只应产生 1 次 search（由 read 内部发起）
    assert len(browser.queries) == 1, f"每轮应只搜 1 次，实际 {len(browser.queries)} 次"
    """对账每轮都要读现值，**不该导出 xlsx**。

    实测（2026-11-19）：导出 12~36 秒、落 242KB，再 openpyxl 解析；
    而搜索响应里已经有 price/qty。对账走 ``respond_only=True`` 就省掉这一整段。
    """

    plan = _plan(["3117213"])
    browser = _Browser(plan)

    asyncio.run(mod.reconcile(browser, plan, tmp_path, attempts=1, wait_seconds=0))

    assert browser.respond_only_calls, "没有调用 read_datasheet_values"
    assert all(browser.respond_only_calls), (
        f"对账应全程 respond_only=True，实际: {browser.respond_only_calls}"
    )


def test_rowid_query_has_no_wildcard(tmp_path) -> None:
    """rowid 查询串**不能带 `*`**。

    实测（2026-11-18）：``*3117213`` 返回 0 个数据表，
    而 ``3117213`` 精确命中 1 个。带了通配就等于什么都读不到，
    对账会把每一行都判成"查不到"。
    """

    plan = _plan(["3117213", "3117211"])
    browser = _Browser(plan)

    asyncio.run(mod.reconcile(browser, plan, tmp_path, attempts=1, wait_seconds=0))

    for query in browser.queries:
        assert "*" not in query, f"查询串里不该有通配: {query!r}"


def test_reconcile_falls_back_to_full_export_when_too_many(tmp_path) -> None:
    """数据表太多导致查询串过长时，退回全店导出（慢但不漏）。"""

    many = [f"{4000000 + i}" for i in range(2000)]
    plan = _plan(many)
    browser = _Browser(plan)

    asyncio.run(mod.reconcile(browser, plan, tmp_path, attempts=1, wait_seconds=0))

    assert "" in browser.queries, "超长时应退回空查询（全店导出）"


def test_rowid_query_limit_is_sane() -> None:
    """上限要能容下常见的几十个数据表，又不至于把查询串撑爆。"""

    assert mod.ROWID_QUERY_MAX_CHARS >= 500
    # 20 个 7 位 rowid + 空格 ≈ 160 字符，远在限内
    typical = " ".join(str(3117210 + i) for i in range(20))
    assert len(typical) < mod.ROWID_QUERY_MAX_CHARS
