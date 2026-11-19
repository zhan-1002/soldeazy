"""复用导出 + 漂移校验（用户定的口径 2026-11-19）。

用户模型：一次改价只需要**两个**关键 xlsx ——
    原始（预览读它、执行读它、回退用它）
    草稿（只改目标单元格）
而「预览不过是修改草稿的局部展示」，不该再导第二次。

改造前（审计结果）：导出 2 次、生成 4~5 个 xlsx
    soldeazy_change.py:979  read_datasheet_values  → 导出 ①
    soldeazy_apply.py:560   export_via_api          → 导出 ②（内容与 ① 相同）

改造后：执行阶段复用 ①，只在上传前用合并搜索核对目标行是否漂移。

本文件守住三件事：
  1. 有可复用导出时不重新导出
  2. 没有可复用导出时才导出
  3. 目标行漂移时必须报出来（不能拿旧快照静默覆盖）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from soldeazy_mcp_server.providers import soldeazy_apply as mod
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem


def _plan(
    rowid: str = "3117213", sku: str = "FGS100W110V", old: float = 27.48
) -> ChangePlan:
    return ChangePlan(
        plan_id="p1",
        kind="price",
        site="US",
        shop={"id": "159", "name": "FUN"},
        created_at="x",
        items=[PlanItem(rowid, sku, "price", old, 27.49)],
        only_listed=False,
    )


class _Browser:
    """带 last_export_path 的假浏览器；search 返回可控现值。"""

    def __init__(self, export_path: Path | None, live_price: float | None) -> None:
        self.last_export_path = export_path
        self.live_price = live_price
        self.searches: list[str] = []
        self.active_shop = {"id": "159", "name": "FUN"}

    async def search(self, query, site, *, only_listed=True):
        self.searches.append(query)
        if self.live_price is None:
            return {"rows": [], "total_records": 0}
        return {
            "rows": [
                {
                    "rowid": "3117213",
                    "sku": "FGS100W110V",
                    "price": self.live_price,
                    "qty": 50,
                    "shop": "EBAY FUN",
                }
            ],
            "total_records": 1,
        }


def test_reuses_the_existing_export(tmp_path) -> None:
    """有 last_export_path 且文件存在 → 返回它，不新建。"""

    export = tmp_path / "state_1.xlsx"
    export.write_bytes(b"x")
    browser = _Browser(export, live_price=27.48)

    found = mod._reusable_export(tmp_path, _plan(), browser)

    assert found == export


def test_no_reuse_when_file_is_gone(tmp_path) -> None:
    """文件已被清理 → 不复用（否则会读到一个不存在的路径）。"""

    missing = tmp_path / "state_gone.xlsx"
    browser = _Browser(missing, live_price=27.48)

    assert mod._reusable_export(tmp_path, _plan(), browser) is None


def test_no_reuse_without_last_export_path(tmp_path) -> None:
    browser = _Browser(None, live_price=27.48)

    assert mod._reusable_export(tmp_path, _plan(), browser) is None


def test_drift_is_detected_when_value_changed(tmp_path) -> None:
    """线上值已从 27.48 变成 30 → 必须报漂移，不能静默覆盖。"""

    browser = _Browser(tmp_path / "x.xlsx", live_price=30.0)

    drift = asyncio.run(mod._targets_unchanged(browser, _plan(old=27.48), []))

    assert drift, "值变了却没报漂移"
    assert "快照过期" in drift[0]
    assert "27.48" in drift[0]


def test_no_drift_when_value_matches(tmp_path) -> None:
    browser = _Browser(tmp_path / "x.xlsx", live_price=27.48)

    assert asyncio.run(mod._targets_unchanged(browser, _plan(old=27.48), [])) == []


def test_unreadable_target_is_not_treated_as_drift(tmp_path) -> None:
    """读不到目标行 ≠ 值变了：交给对账去判，别在这里误报。"""

    browser = _Browser(tmp_path / "x.xlsx", live_price=None)

    assert asyncio.run(mod._targets_unchanged(browser, _plan(), [])) == []


def test_verification_uses_a_merged_query(tmp_path) -> None:
    """核对走合并搜索（带 `*`），不是逐个搜。"""

    browser = _Browser(tmp_path / "x.xlsx", live_price=27.48)

    asyncio.run(mod._targets_unchanged(browser, _plan(), []))

    assert browser.searches, "没有发起核对搜索"
    query = browser.searches[0]
    assert query.startswith("*")
    assert query.count("*") == 1
