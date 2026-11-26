"""搜索命中 0 条时不该导出，也不该报一个指错方向的错误。

实测（2026-11-19）：
    搜索有结果（如全店 1636 个数据表）→ 导出返回 xlsx（PK 开头，248KB）
    搜索 0 条（不存在的 SKU）        → 导出返回 **1.1MB HTML 页面**

于是走完整链路时报出 ``cookie 导出没有返回 xlsx``。这句话把人引向
cookie / DNS / 网络，但真实原因只是"这个范围里没有可导出的数据表"。

那台机器上「11.20 改价未执行」的报错就有一半是这个：
它按 US/FUN 去查一个只在 DE 才有的 SKU，命中 0 条 → 导出拿回 HTML →
错误说明写成"无法解析 SoldEazy 域名"，方向完全错了。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from soldeazy_mcp_server.config import Settings
from soldeazy_mcp_server.errors import SoldeazyError
from soldeazy_mcp_server.providers import soldeazy as mod


class _Browser(mod.SoldeazyBrowser):
    """把 search / export 都换成可控版本，只验编排逻辑。"""

    def __init__(self, total: int, tmp_path) -> None:
        super().__init__(
            replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "p"))
        )
        self.total = total
        self.searches: list[str] = []
        self.exports: list[str] = []

    async def search(self, query, site, shop="", *, only_listed=True, **_kw):
        self.searches.append(query)
        return {"total_records": self.total, "rows": [], "datasheet_count": self.total}

    async def export_via_api(self, dest, **_kw):
        self.exports.append(str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PK\x03\x04xlsx")
        return {"path": str(dest)}


def test_zero_matches_skips_the_export(tmp_path) -> None:
    """命中 0 条 → 直接回空字典，**一次导出都不发**。"""

    browser = _Browser(total=0, tmp_path=tmp_path)

    values = asyncio.run(
        browser.read_datasheet_values("DE", "*NO-SUCH-SKU", only_listed=False)
    )

    assert values == {}
    assert browser.searches == ["*NO-SUCH-SKU"], "应该搜过一次"
    assert browser.exports == [], "0 条时不该导出"


def test_zero_matches_clears_stale_state(tmp_path) -> None:
    """0 条时要清掉上一轮留下的统计，否则调用方会读到别的查询的数字。"""

    browser = _Browser(total=0, tmp_path=tmp_path)
    browser.last_export_total = 999
    browser.last_export_warnings = ["上一轮的警告"]
    browser.last_export_shops = ["EBAY OLD"]

    asyncio.run(browser.read_datasheet_values("DE", "*NONE", only_listed=False))

    assert browser.last_export_total == 0
    assert browser.last_export_warnings == []
    assert browser.last_export_shops == []


def test_search_false_still_exports(tmp_path) -> None:
    """``search=False`` 时不能套用这个短路。

    那种调用的 expected 来自**上一条** search 记下的命中数，可能属于另一个
    查询。拿它判 0 条会误伤 —— 对账正是用 search=False 的。
    """

    browser = _Browser(total=5, tmp_path=tmp_path)
    browser._last_total_records = 0        # 故意留一个 0

    async def fake_export(dest, **_kw):
        # 造一份**合法**的最小 xlsx：read_matrix 要真的能打开它
        from openpyxl import Workbook

        browser.exports.append(str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        ws = wb.active
        ws.append(["dsheet_row_id", "dsheet_shop_initial", "p", "sku", "qty", "price"])
        ws.append(["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"])
        ws.append(["3117213", "ACME", "P", "SKU-A", "10", "9.9"])
        wb.save(dest)
        return {"path": str(dest), "size": 1}

    browser.export_via_api = fake_export  # type: ignore[method-assign]

    asyncio.run(
        browser.read_datasheet_values("DE", "", search=False, only_listed=False)
    )

    assert browser.exports, "search=False 且命中数未知时，应照常尝试导出"
    assert browser.searches == [], "search=False 不该重新搜索"


def test_export_error_says_what_actually_came_back(tmp_path) -> None:
    """导出拿回 HTML 时，错误要说清是登录页还是"没有数据"，别只说没返回 xlsx。"""

    class _HtmlBrowser(mod.SoldeazyBrowser):
        def __init__(self) -> None:
            super().__init__(
                replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "p"))
            )

        async def _request(self, method, url, **_kw):
            # 模拟平台在无数据时返回列表页 HTML
            return 200, b'<html><table id="tbl_dSheet"></table></html>'

        async def session_alive_http(self) -> bool:
            return True

    browser = _HtmlBrowser()
    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.export_via_api(tmp_path / "x.xlsx"))

    message = caught.value.message
    assert "没有可导出的数据表" in message or "列表页" in message
    # 关键：不能只说"没有返回 xlsx"就完事
    assert "导出失败" in message
