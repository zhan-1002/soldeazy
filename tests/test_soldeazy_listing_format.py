"""刊登方式默认 BIN（长期在线），拍卖（AUC）不碰。

用户口径（2026-11-19）：
    「这里默认为 bin 除非被要求」
    「BIN 是长期在线 listing，AUC 是拍卖」

页面上的真实选项（从 HTML 原文读到，不硬编码猜）：

    <select id='listformat' name='listformat'>
      <option value=''></option>       ← 全部
      <option value='AUC'>AUC</option>  ← 拍卖
      <option value='BIN'>BIN</option>  ← 长期在线
    </select>

注意空串代表「全部」，所以默认值不能是空串 —— 那会把拍卖也带进来。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from soldeazy_mcp_server.config import Settings
from soldeazy_mcp_server.providers import soldeazy as mod


def test_default_listing_format_is_bin() -> None:
    """常量本身：默认必须是 BIN，不能是空串（空串=全部，会把拍卖带进来）。"""

    assert mod.DEFAULT_LISTING_FORMAT == "BIN"
    assert mod.DEFAULT_LISTING_FORMAT != ""


def _browser(tmp_path):
    return mod.SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path))
    )


def _prepare(tmp_path, handler):
    """造一个走 HTTP 通道、站点已解析好的 browser。"""

    browser = _browser(tmp_path)
    browser.active_shop = {"id": "159", "name": "FUN"}
    browser._last_site_code = "0"
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler)
    )

    async def fake_resolve(_site):
        return "0"

    browser.resolve_site_code = fake_resolve  # type: ignore[method-assign]

    async def fake_shops():
        return [{"id": "159", "name": "FUN"}]

    browser.list_shop_options = fake_shops  # type: ignore[method-assign]
    return browser


def test_search_sends_listformat_bin_by_default(tmp_path) -> None:
    """搜索表单里必须真的带上 listformat=BIN。"""

    seen: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qsl

        seen["form"] = dict(parse_qsl((await request.aread()).decode(),
                                      keep_blank_values=True))
        return httpx.Response(200, text="<table id='tbl_dSheet'></table>")

    browser = _prepare(tmp_path, handler)

    asyncio.run(browser.search("*TEST", "US", shop="FUN", only_listed=False))

    assert seen["form"].get("listformat") == "BIN"


def test_listformat_can_be_overridden(tmp_path) -> None:
    """显式传 "" 时应发送空串（= 全部形式），不被默认值覆盖。"""

    seen: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qsl

        seen["form"] = dict(parse_qsl((await request.aread()).decode(),
                                      keep_blank_values=True))
        return httpx.Response(200, text="<table id='tbl_dSheet'></table>")

    browser = _prepare(tmp_path, handler)

    asyncio.run(browser.search("*TEST", "US", shop="FUN", only_listed=False,
                               listing_format=""))

    assert seen["form"].get("listformat") == ""


def test_export_reuses_the_search_listing_format(tmp_path) -> None:
    """导出必须沿用搜索时的刊登方式。

    否则会"搜的是 BIN、导的是全部" —— 计划行集错位，AUC 的行混进来。
    """

    seen: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qsl

        seen["form"] = dict(parse_qsl((await request.aread()).decode(),
                                      keep_blank_values=True))
        return httpx.Response(200, content=b"PK\x03\x04xlsx")

    browser = _browser(tmp_path)
    browser.active_shop = {"id": "159", "name": "FUN"}
    browser._last_site_code = "0"
    browser._last_query = ""
    browser._last_status_value = ""
    browser._last_date_value = ""
    browser._last_listing_format = "BIN"
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler)
    )

    async def alive():
        return True

    browser.session_alive_http = alive  # type: ignore[method-assign]

    dest = tmp_path / "out.xlsx"
    asyncio.run(browser.export_via_api(dest))

    assert seen["form"].get("listformat") == "BIN"


def test_search_records_the_listing_format_for_later_export(tmp_path) -> None:
    """搜索要把用过的刊登方式记下来，供导出沿用。"""

    browser = _browser(tmp_path)
    browser.active_shop = {"id": "159", "name": "FUN"}
    browser._last_site_code = "0"

    async def fake_http(*_a, **_k):
        return {"total_records": 0, "rows": [], "datasheet_count": 0}

    async def fake_resolve(_site):
        return "0"

    browser._search_via_http = fake_http  # type: ignore[method-assign]
    browser.resolve_site_code = fake_resolve  # type: ignore[method-assign]

    asyncio.run(browser.search("*TEST", "US", shop="FUN", only_listed=False,
                               listing_format="AUC"))

    assert browser._last_listing_format == "AUC"
