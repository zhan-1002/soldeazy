"""Soldeazy 浏览器 provider 的纯解析测试（不启动浏览器）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from soldeazy_mcp_server.providers.soldeazy import (
    COLUMN_ALIASES,
    SELECTORS,
    SITE_CODES,
    SITE_LABELS,
    Locator,
    parse_rows,
    resolve_columns,
    resolve_upload_handle,
)

# 真机表头（2026-11-02，14 个 th，首列是勾选框）
REAL_HEADER = [
    "",
    "序号",
    "数据表 编号",
    "功能选项",
    "状态",
    "渠道 帐号",
    "站点",
    "标题",
    "",
    "SKU",
    "价格",
    "运费 (当地)",
    "促销 刊登",
    "eBay促销",
]

# 真机数据行（16 个 td：多出勾选框 + 一个重复标题格）
ROW_SINGLE = [
    "",
    "1",
    "4567313",
    "edit delete previewSYOSUERV",
    "",
    "EBAY TUD",
    "US",
    "Portable Power Bank 7USB",
    "",
    "Portable Power Bank 7USB",
    "",
    "PCPB0028US",
    "20.99",
    "0",
    "-",
    "-",
]

# 多属性行：SKU / 价格单元格里多行挤在一起
ROW_MULTI = [
    "",
    "2",
    "4567312",
    "edit delete previewSYOSUERV",
    "",
    "EBAY TUD",
    "US",
    "Mini Portable Charger",
    "",
    "Mini Portable Charger",
    "16",
    "PCPB0028US\nPCPB0028US -1\nPCPB0028US -2",
    "20.99\n20.99\n21.99",
    "0",
    "-",
    "-",
]

# 变体展开行：只有 2 个单元格
ROW_EXPAND = ["", "Portable Power Bank 7USB"]


# 影刀工程里那 25 个选择器，必须一个不少地映射到 provider
YINGDAO_SELECTOR_NAMES = (
    "输入框_login_user", "密码框", "Login", "soldeazy-广告弹框",
    "弹框-(不再显示此消息)", "弹框-关闭",
    "SKU输入框", "Channel Site", "Listing Format", "Status", "Date Modified",
    "Search", "搜索结果table", "Scope", "Export", "Bulk Export Options",
    "Add / edit an Export Profile", "Bulk Export Options_export", "Bulk SYNC",
    "同步弹框", "Price Postage", "SYNC UP", "New D-Align bul...",
    "选择文件_filePackage", "Upload Completed",
)


def test_all_yingdao_selectors_are_mapped() -> None:
    names = {entry.name for entry in SELECTORS.values()}
    missing = [name for name in YINGDAO_SELECTOR_NAMES if name not in names]
    assert not missing, missing

    for key, entry in SELECTORS.items():
        assert isinstance(entry, Locator), key
        assert entry.name and entry.xpath.startswith("//"), key
        assert entry.candidates()


def test_export_selectors_cover_the_real_export_dialog() -> None:
    # 实测：导出档案为空时页面 JS 直接 return false，所以必须能选到这两个下拉
    assert SELECTORS["export_dialog"].css == "#jqizoo"
    assert "listExppf" in SELECTORS["export_profile_select"].css
    assert "listExpfmt" in SELECTORS["export_format_select"].css


def test_site_labels_match_the_real_dropdown() -> None:
    # 实测：DE 在下拉里叫 Germany，值 77；US 的值是 0，UK 是 3
    assert SITE_LABELS["DE"] == "Germany"
    assert SITE_LABELS["US"] == "US"
    assert SITE_CODES["DE"] == "77"
    assert SITE_CODES["US"] == "0"
    assert SITE_CODES["UK"] == "3"


def test_resolve_columns_ignores_header_line_breaks() -> None:
    mapping = resolve_columns(REAL_HEADER)
    assert mapping["rowid"] == 2
    assert mapping["site"] == 6
    assert mapping["sku"] == 9
    assert mapping["price"] == 10
    assert mapping["postage"] == 11


def test_every_column_has_aliases() -> None:
    for field, aliases in COLUMN_ALIASES.items():
        assert aliases, field
        assert all(isinstance(alias, str) and alias for alias in aliases), field


def test_upload_handle_is_found_when_the_server_keeps_the_name() -> None:
    body = {"data": {"filePackage": []}, "handle": {"plan.done.xlsx": 99}}
    handle, stored = resolve_upload_handle(body, "plan.done.xlsx")
    assert handle == 99
    assert stored == "plan.done.xlsx"


def test_upload_handle_survives_a_server_side_rename() -> None:
    """同名文件重传时服务端会改名 ``a.xlsx`` → ``a (1).xlsx``。

    ``handle`` 字典的 key 是改名后的名字，用本地名去查会拿到 ``None``，
    于是上传明明成功、工序也跑完了，核对却在第一步判失败，
    还会把工序核对整段跳过 —— 这是实测踩到的真实缺陷。
    """

    body = {
        "data": {"filePackage": [{"name": "restore-from-snapshot (1).xlsx"}]},
        "handle": {"restore-from-snapshot (1).xlsx": 1982313},
    }
    handle, stored = resolve_upload_handle(body, "restore-from-snapshot.xlsx")
    assert handle == 1982313
    assert stored == "restore-from-snapshot (1).xlsx"


def test_upload_handle_ignores_whitespace_and_case_differences() -> None:
    body = {"handle": {"My Plan%20(2).xlsx": 7}}
    handle, _stored = resolve_upload_handle(body, "my plan.xlsx")
    assert handle == 7


def test_upload_handle_falls_back_to_the_only_entry() -> None:
    body = {"handle": {"totally-different-name.xlsx": 5}}
    handle, _stored = resolve_upload_handle(body, "plan.done.xlsx")
    assert handle == 5


def test_upload_handle_is_none_when_several_entries_do_not_match() -> None:
    """多个候选且都对不上时不许瞎猜：猜错会去核对别人的工序任务。"""

    body = {"handle": {"a.xlsx": 1, "b.xlsx": 2}}
    handle, stored = resolve_upload_handle(body, "plan.done.xlsx")
    assert handle is None
    assert stored == ""


def test_upload_handle_is_none_without_a_handle_field() -> None:
    assert resolve_upload_handle({}, "plan.done.xlsx") == (None, "")
    assert resolve_upload_handle({"handle": {}}, "plan.done.xlsx") == (None, "")
    assert resolve_upload_handle(None, "plan.done.xlsx") == (None, "")


def test_parse_rows_uses_the_measured_cell_positions() -> None:
    header, records = parse_rows([REAL_HEADER, ROW_SINGLE])

    assert header[1] == "序号"
    assert len(records) == 1
    record = records[0]
    assert record["rowid"] == "4567313"
    assert record["shop"] == "EBAY TUD"
    assert record["site"] == "US"
    assert record["sku"] == "PCPB0028US"
    assert record["price"] == pytest.approx(20.99)
    assert record["postage"] == pytest.approx(0.0)
    assert record["variants"] == 1


def test_parse_rows_marks_multi_variant_rows() -> None:
    _, records = parse_rows([REAL_HEADER, ROW_MULTI])
    record = records[0]

    assert record["rowid"] == "4567312"
    assert record["variants"] == 3
    assert record["sku"] == "PCPB0028US"      # 取第一行作代表
    assert record["price"] == pytest.approx(20.99)


def test_parse_rows_expands_every_variant_when_asked() -> None:
    """默认只留第一个变体，其余 SKU 在搜索结果里会**凭空消失**。

    2026-11-13 控价就因此把 ``ZMDC0063``（第 5 个变体）误判成"线上找不到"。
    """

    _, records = parse_rows([REAL_HEADER, ROW_MULTI], expand_variants=True)

    assert [r["sku"] for r in records] == [
        "PCPB0028US",
        "PCPB0028US -1",
        "PCPB0028US -2",
    ]
    assert [r["price"] for r in records] == [
        pytest.approx(20.99),
        pytest.approx(20.99),
        pytest.approx(21.99),
    ]
    assert [r["variant_index"] for r in records] == [0, 1, 2]
    # 每个变体都记得自己属于哪个数据表、总共有几个变体
    assert {r["rowid"] for r in records} == {"4567312"}
    assert {r["variants"] for r in records} == {3}


def test_expanded_variants_still_skip_expansion_rows() -> None:
    """变体展开行（只有 2 格）不能因为展开开关而被当成数据行。"""

    _, records = parse_rows(
        [REAL_HEADER, ROW_SINGLE, ROW_MULTI, ROW_EXPAND], expand_variants=True
    )
    assert [r["sku"] for r in records] == [
        "PCPB0028US",
        "PCPB0028US",
        "PCPB0028US -1",
        "PCPB0028US -2",
    ]


def test_single_variant_rows_keep_a_uniform_shape() -> None:
    """展开开关打开时，单变体行也带 ``variant_index=0``。

    统一有键比"有时有有时没有"好：调用方直接读，不用先判断键在不在。
    关掉开关时仍然不带——那是旧口径，不能悄悄改字段。
    """

    _, expanded = parse_rows([REAL_HEADER, ROW_SINGLE], expand_variants=True)
    _, plain = parse_rows([REAL_HEADER, ROW_SINGLE])

    assert expanded[0]["variant_index"] == 0
    assert expanded[0]["variants"] == 1
    assert "variant_index" not in plain[0]
    # 除这一个键之外，两种口径的值完全一样
    assert {k: v for k, v in expanded[0].items() if k != "variant_index"} == plain[0]


def test_datasheet_count_distinguishes_rows_from_datasheets() -> None:
    """变体展开后行数 > 数据表数：批量上限按数据表算，两个口径都得给出。"""

    from soldeazy_mcp_server.providers.soldeazy import _count_datasheets

    _, records = parse_rows([REAL_HEADER, ROW_SINGLE, ROW_MULTI], expand_variants=True)
    assert len(records) == 4
    assert _count_datasheets(records) == 2
    assert _count_datasheets([]) == 0


def test_parse_rows_skips_variant_expansion_rows() -> None:
    _, records = parse_rows([REAL_HEADER, ROW_SINGLE, ROW_EXPAND])
    assert [r["rowid"] for r in records] == ["4567313"]


def test_parse_rows_tolerates_an_empty_page() -> None:
    assert parse_rows([]) == ([], [])
    assert parse_rows([["", "  "]]) == ([], [])


def test_parse_rows_ignores_rows_without_a_datasheet_id() -> None:
    _, records = parse_rows([REAL_HEADER, [""] + ["x"] * 15])
    assert records == []

# 筛选后列表少一列（勾选框不见了），内容锚定仍应取对
ROW_SINGLE_15 = ROW_SINGLE[1:]


def test_parse_rows_survives_a_column_shift() -> None:
    _, records = parse_rows([REAL_HEADER, ROW_SINGLE_15])
    record = records[0]
    assert record["rowid"] == "4567313"
    assert record["shop"] == "EBAY TUD"
    assert record["site"] == "US"
    assert record["sku"] == "PCPB0028US"
    assert record["price"] == pytest.approx(20.99)


def test_shop_scope_hint_admits_when_no_shop_filter_was_applied() -> None:
    """2026-11-13：平台语义是「不选店铺 = 全部店铺」，不是某个默认店铺。

    没过滤时必须这么说清楚，否则调用方会把混合结果误当成自己指定的那个店。
    """

    from soldeazy_mcp_server.providers.soldeazy import _shop_scope_hint

    hint = _shop_scope_hint("")
    assert hint["shop_filter_applied"] is False
    assert "全部店铺" in hint["shop_filter"]


def test_shop_scope_hint_lists_the_shops_actually_returned() -> None:
    from soldeazy_mcp_server.providers.soldeazy import _shop_scope_hint

    records = [{"shop": "EBAY TAY"}, {"shop": "EBAY TAY"}]

    hint = _shop_scope_hint("FUN", records)
    assert hint["shop_filter_applied"] is True
    assert hint["shops_in_result"] == ["EBAY TAY"]
    assert hint["shop_scope_mismatch"] is True     # 说是 FUN，回来的是 TAY

    same = _shop_scope_hint("FUN", [{"shop": "FUN"}])
    assert same["shop_scope_mismatch"] is False


def test_shop_scope_hint_tolerates_prefixed_shop_names() -> None:
    """平台里的店铺名是 EBAY LT 这种，查询用简称 LT，不能算不一致。"""

    from soldeazy_mcp_server.providers.soldeazy import _shop_scope_hint

    hint = _shop_scope_hint("LT", [{"shop": "EBAY LT"}])
    assert hint["shop_scope_mismatch"] is False


def test_launch_error_tells_profile_lock_apart_from_missing_browser():
    """抢不到 profile 绝不能报成"找不到 Chrome"——那是两个完全不同的病。"""

    from pathlib import Path

    from soldeazy_mcp_server.providers.soldeazy import launch_error

    locked = launch_error(
        Path("x"),
        ["chrome: BrowserType.launch_persistent_context: ProcessSingleton: "
         "user data directory is already in use"],
        tried=["chrome"],
    )
    assert locked.code == "SOLDEAZY_PROFILE_LOCKED"
    assert "只有一个会话" in locked.message

    denied = launch_error(Path("x"), ["chrome: EPERM access denied"], tried=["chrome"])
    assert denied.code == "SOLDEAZY_BROWSER_LAUNCH_DENIED"

    missing = launch_error(Path("x"), ["chrome: Executable doesn't exist"], tried=["chrome"])
    assert missing.code == "SOLDEAZY_BROWSER_LAUNCH_FAILED"
    assert missing.details["tried"] == ["chrome"]


def test_looks_logged_in_reads_the_two_markers() -> None:
    from soldeazy_mcp_server.providers.soldeazy import looks_logged_in

    assert looks_logged_in('<table id="tbl_dSheet"></table>') is True
    assert looks_logged_in('<form><input type="password" name="pwd"></form>') is False
    assert looks_logged_in("<html>whatever</html>") is False


def test_cookie_dict_reads_the_session_file(tmp_path) -> None:
    import json
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "session_cookies.json").write_text(
        json.dumps([{"name": "a", "value": "1"}, {"name": "_ht_ses_", "value": "x"}, "junk"]),
        encoding="utf-8",
    )
    browser = SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=str(profile)))
    assert browser._cookie_dict() == {"a": "1", "_ht_ses_": "x"}


def test_session_alive_http_is_false_without_cookies(tmp_path) -> None:
    import asyncio
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "empty"))
    )
    assert asyncio.run(browser.session_alive_http()) is False


def test_export_without_browser_and_without_cookies_fails_loudly(tmp_path) -> None:
    """没开浏览器 + 没 cookie：必须明确说要登录，不能静默导出一份错的。"""

    import asyncio
    from dataclasses import replace

    import pytest

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.errors import SoldeazyError
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "empty"))
    )
    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.export_via_api(tmp_path / "x.xlsx"))
    assert caught.value.code == "SOLDEAZY_LOGIN_REQUIRED"


def test_parse_form_state_reads_selected_and_checked_only() -> None:
    """和浏览器序列化口径一致：checkbox/radio 只取 checked，select 取 selected。"""

    from soldeazy_mcp_server.providers.soldeazy import parse_form_state

    html = """
    <form action="show">
      <input type="hidden" name="page" value="1">
      <input type="text" name="txtproductcode" value="abc">
      <input type="checkbox" name="keep" checked value="yes">
      <input type="checkbox" name="drop" value="no">
      <select name="listactive"><option value=""> </option>
        <option value="All" selected>已刊登(所有)</option></select>
      <select name="multi"><option value="a" selected>A</option>
        <option value="b" selected>B</option><option value="c">C</option></select>
      <select name="nosel"><option value="x">X</option><option value="y">Y</option></select>
      <input type="button" name="btn_search" value="搜索">
    </form>
    """
    state = parse_form_state(html)
    assert state["page"] == ["1"]
    assert state["txtproductcode"] == ["abc"]
    assert state["keep"] == ["yes"]
    assert "drop" not in state
    assert state["listactive"] == ["All"]
    assert state["multi"] == ["a", "b"]
    assert state["nosel"] == ["x"]          # 没 selected 就取第一项（浏览器行为）
    assert state["btn_search"] == ["搜索"]  # 搜索按钮可能是服务端分支依据，保留


def test_parse_result_table_keeps_br_as_variant_separator() -> None:
    from soldeazy_mcp_server.providers.soldeazy import parse_result_table

    html = """
    <table id='tbl_dSheet'><tr><th>Row ID</th><th>SKU</th></tr>
    <tr><td>4512957</td><td>JJPC0029<br>JJPC0029US</td></tr></table>
    """
    matrix = parse_result_table(html)
    assert matrix[0] == ["Row ID", "SKU"]
    assert matrix[1][1] == "JJPC0029\nJJPC0029US"


def test_extract_total_and_token_accept_both_quote_styles() -> None:
    from soldeazy_mcp_server.providers.soldeazy import (
        extract_sql_token,
        extract_total_records,
    )

    assert extract_total_records("<a>1,636 笔记录</a>") == 1636
    assert extract_total_records("nothing") == 0
    html = "<input type=hidden name='sql_cache_token' id='sql_cache_token' value='tok/123'>"
    assert extract_sql_token(html) == "tok/123"
    html2 = '<input name="sql_cache_token" value="tok/456">'
    assert extract_sql_token(html2) == "tok/456"
    assert extract_sql_token("<html></html>") == ""


def test_request_reports_login_required_on_http_redirect() -> None:
    """没有浏览器时，任何被 302 到登录页的请求都必须报 LOGIN_REQUIRED。

    这是双通道统一到 ``_request`` 的**目的**：以前四个调用点各自判 3xx，
    漏判一处就会把登录页 HTML 当成业务数据往下走（比如导出拿到登录页，
    被报成"没返回 xlsx"，掩盖真正的原因）。
    """

    import asyncio
    from dataclasses import replace

    import httpx
    import pytest

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.errors import SoldeazyError
    from soldeazy_mcp_server.providers import soldeazy as mod

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "/app/soldeazy/login?x=1"}
            )
        )
    )

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser._request("post", "https://x/y", form={}))
    assert caught.value.code == "SOLDEAZY_LOGIN_REQUIRED"


def test_request_returns_status_and_body_without_browser() -> None:
    import asyncio
    from dataclasses import replace

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = (await request.aread()).decode()
        return httpx.Response(200, content=b"payload-bytes")

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler)
    )

    status, body = asyncio.run(
        browser._request("post", "https://x/y", form={"mode": "agree"})
    )
    assert status == 200
    assert body == b"payload-bytes"
    assert "mode=agree" in str(seen["body"])


def test_request_translates_multipart_for_httpx() -> None:
    """调用方只写 Playwright 那一种形状，httpx 这边由 ``_request`` 换格式。

    两边的 multipart 形状不一样（``{name,mimeType,buffer}`` vs ``(name,bytes,type)``）。
    如果不在这里换，上传在免浏览器通道会直接把 dict 当文件内容发出去。
    """

    import asyncio
    from dataclasses import replace

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = (await request.aread()).decode("utf-8", "ignore")
        return httpx.Response(200, text="ok")

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler)
    )

    asyncio.run(
        browser._request(
            "post",
            "https://x/upload",
            multipart={
                "filePackage": {
                    "name": "plan.done.xlsx",
                    "mimeType": "application/vnd.ms-excel",
                    "buffer": b"PK\x03\x04data",
                }
            },
        )
    )

    assert "multipart/form-data" in seen["content_type"]
    assert 'name="filePackage"' in seen["body"]
    assert "plan.done.xlsx" in seen["body"]
    assert "PK" in seen["body"]


def test_request_does_not_renavigate_when_already_on_site() -> None:
    """已经在站点内（哪怕是搜索结果页）就不能再导航一次。

    实测（2026-11-18）：把这里的判断从"在站点内即就位"收紧成"必须已在列表页"，
    导出会丢掉当前搜索上下文——FUN 全量 7077 行变成 5777 行，**少 1300 行却不报错**。
    这类回归单测抓不到（MockTransport 里没有页面状态），只能靠行为断言守住。
    """

    import asyncio
    from dataclasses import replace

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    navigations: list[str] = []

    class _Response:
        status = 200

        async def body(self) -> bytes:
            return b"PK\x03\x04ok"

    class _Request:
        async def post(self, *_a, **_k):
            return _Response()

    class _Page:
        url = "https://stiger.soldeazy.com/app/soldeazy/datasheet/dsheet_list?x=1"

        class context:  # noqa: N801 - 模拟 Playwright 的属性形状
            request = _Request()

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    browser._context = object()  # type: ignore[assignment]
    browser._page = _Page()      # type: ignore[assignment]

    async def fake_ensure_page():
        return _Page()

    async def fake_goto(url: str, **_k):
        navigations.append(url)

    browser._ensure_page = fake_ensure_page       # type: ignore[method-assign]
    browser._goto = fake_goto                      # type: ignore[method-assign]

    asyncio.run(browser._request("post", "https://stiger.soldeazy.com/x", form={}))
    assert navigations == []

    # 显式给了 page_path（工序页）时，不在那一页就必须导航过去
    asyncio.run(
        browser._request(
            "post", "https://stiger.soldeazy.com/y", form={},
            page_path="/app/soldeazy/queue-job",
        )
    )
    assert navigations == ["https://stiger.soldeazy.com/app/soldeazy/queue-job"]


def test_bulk_batches_cover_everything_exactly_once() -> None:
    """分批区间必须无重叠、无遗漏地覆盖 1..total —— 这是分批正确性的全部。

    ``partial_result`` 的序号是**倒序**的（序号 1 = 全量最后一条，2026-11-18 实测），
    但只要覆盖完整，方向不影响结果完整性。
    """

    from soldeazy_mcp_server.providers.soldeazy import bulk_batches

    for total in (1, 2999, 3000, 3001, 7000, 20_421, 76_096):
        ranges = bulk_batches(total)
        covered: list[int] = []
        for start, end in ranges:
            covered.extend(range(start, end + 1))
        assert covered == list(range(1, total + 1)), f"total={total} 覆盖不完整"
        assert all(end - start + 1 <= 3000 for start, end in ranges), total


def test_bulk_batches_splits_only_when_needed() -> None:
    from soldeazy_mcp_server.providers.soldeazy import bulk_batches

    assert bulk_batches(0) == []
    assert bulk_batches(1) == [(1, 1)]
    assert bulk_batches(1636) == [(1, 1636)]          # FUN 实测规模：一轮就好
    assert bulk_batches(3000) == [(1, 3000)]
    # ANO 实测 20,421 个：7 轮
    ranges = bulk_batches(20_421)
    assert len(ranges) == 7
    assert ranges[0] == (1, 3000)
    assert ranges[-1][1] == 20_421


def test_batches_do_not_prune_each_other(tmp_path) -> None:
    """分批导出的文件之间不能互相清理。

    实测（2026-11-18，ANO 20,421 个数据表 → 7 批）：原先每批开头都调一次
    ``_prune_old_exports(keep=5)``，到第 6 批就把第 1 批的 xlsx 删掉了，
    而 ``last_export_batch_paths`` 还引用着它 —— 人工核对按批打开时直接失效。

    这里直接测清理策略：分批时要为"这一批 + 保留历史"留出足够额度。
    """

    from soldeazy_mcp_server.providers.soldeazy import (
        KEEP_EXPORT_FILES,
        _prune_old_exports,
    )

    workdir = tmp_path / "reconcile"
    workdir.mkdir()
    # 造 7 个批次文件（模拟 7 批导出）
    batch_files = []
    for index in range(1, 8):
        path = workdir / f"state_100_b{index}.xlsx"
        path.write_bytes(b"x")
        batch_files.append(path)

    # 分批时的调用方式：额度 = 保留历史 + 本次批次数
    import time

    for offset, path in enumerate(batch_files):
        # 制造 mtime 递增，让"最近"顺序确定
        now = time.time() + offset
        import os

        os.utime(path, (now, now))

    kept = _prune_old_exports(workdir, keep=KEEP_EXPORT_FILES + len(batch_files))
    assert kept == 0
    assert all(path.is_file() for path in batch_files), "分批文件被误删"

    # 反面对照：如果按旧的 keep=5 清理，7 个文件会被删掉 2 个
    removed = _prune_old_exports(workdir, keep=KEEP_EXPORT_FILES)
    assert removed == 2


def test_bulk_range_is_closed_and_one_based() -> None:
    """闭区间、1-based：序号 1 是有效起点，不是 0。"""

    from soldeazy_mcp_server.providers.soldeazy import bulk_range

    assert bulk_range(0, 5000) == (1, 3000)
    assert bulk_range(1, 5000) == (3001, 5000)   # 尾部截到 total
    assert bulk_range(2, 5000) == (0, 0)         # 越过末尾


def test_parse_shop_options_reads_id_and_name() -> None:
    from soldeazy_mcp_server.providers.soldeazy import parse_shop_options

    html = """
    <select name='listshop[]'><option value=''> </option>
    <option value='159'>FUN</option><option value='4432'>LT</option></select>
    """
    options = parse_shop_options(html)
    assert options == [{"id": "159", "name": "FUN"}, {"id": "4432", "name": "LT"}]


def test_export_partial_result_sends_the_range_fields() -> None:
    """按区间导出必须真的把 ``bulk_option_from/to`` 和 ``partial_result`` 发出去。

    这是分批方案的全部落地点：写错了不会报错，只会**悄悄导回全量或导错段**。
    之前只有 ``bulk_batches()`` 的纯逻辑测试，表单参数完全没覆盖。
    """

    import asyncio
    from dataclasses import replace
    from urllib.parse import parse_qsl

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    seen: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = (await request.aread()).decode()
        seen["form"] = dict(parse_qsl(body, keep_blank_values=True))
        return httpx.Response(200, content=b"PK\x03\x04xlsx")

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler)
    )
    browser.active_shop = {"id": "159", "name": "FUN"}
    browser._last_site_code = "0"
    browser._last_query = ""
    browser._last_status_value = ""
    browser._last_date_value = ""

    async def alive():
        return True

    browser.session_alive_http = alive  # type: ignore[method-assign]

    import tempfile

    dest = Path(tempfile.mkdtemp()) / "part.xlsx"
    asyncio.run(browser.export_via_api(dest, bulk_from=3001, bulk_to=6000))

    form = seen["form"]
    assert form["bulk_option"] == "partial_result"
    assert form["bulk_option_from"] == "3001"
    assert form["bulk_option_to"] == "6000"


def test_export_whole_result_does_not_send_range_fields() -> None:
    """不给区间时是 whole_result，且**不能**带 from/to。

    带上会被误解成"已按区间取值"——实际上 whole_result 只导前 3000 个，
    那才是真正的静默截断来源。
    """

    import asyncio
    from dataclasses import replace
    from urllib.parse import parse_qsl

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    seen: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(parse_qsl((await request.aread()).decode(), keep_blank_values=True))
        return httpx.Response(200, content=b"PK\x03\x04xlsx")

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler)
    )
    browser.active_shop = {"id": "159", "name": "FUN"}
    browser._last_site_code = "0"

    async def alive():
        return True

    browser.session_alive_http = alive  # type: ignore[method-assign]

    import tempfile

    dest = Path(tempfile.mkdtemp()) / "whole.xlsx"
    asyncio.run(browser.export_via_api(dest))

    form = seen["form"]
    assert form["bulk_option"] == "whole_result"
    assert "bulk_option_from" not in form
    assert "bulk_option_to" not in form


def test_export_rejects_a_half_given_range() -> None:
    """只给一端是用法错误：静默忽略会让人以为按区间导了，实际导了全量。"""

    import asyncio
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.errors import SoldeazyError
    from soldeazy_mcp_server.providers import soldeazy as mod

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    dest = Path("unused.xlsx")

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.export_via_api(dest, bulk_from=1))
    assert caught.value.code == "SOLDEAZY_BAD_RANGE"

    with pytest.raises(SoldeazyError):
        asyncio.run(browser.export_via_api(dest, bulk_to=10))

    # 起点大于终点也要拒
    with pytest.raises(SoldeazyError):
        asyncio.run(browser.export_via_api(dest, bulk_from=100, bulk_to=1))


def test_search_via_http_builds_the_same_filters_as_the_page(tmp_path, monkeypatch) -> None:
    """HTTP 通道必须和 DOM 通道同一口径：日期留空、状态按 only_listed、店铺按 id。"""

    import asyncio
    from dataclasses import replace

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    page_html = """
    <table id="tbl_dSheet"></table>
    <form action="show">
      <input type="text" name="txtproductcode" value="">
      <select name='listshop[]'><option value='159' selected>FUN</option></select>
      <select name="listsite"><option value="" selected> </option></select>
      <select name="listactive"><option value="" selected> </option>
        <option value="All">已刊登(所有)</option></select>
      <select name="listdate"><option value="" selected> </option>
        <option value="month">month</option></select>
      <input type="hidden" name="page" value="1">
      <input type="hidden" name="limit" value="25">
      <input type="button" name="btn_search" value="搜索">
    </form>
    """
    result_html = """
    <input type=hidden name='sql_cache_token' value='tok/9'>
    <a>42 笔记录</a>
    <table id="tbl_dSheet"><tr><th>dsheet_row_id</th><th>dsheet_shop_initial</th>
    <th>dsheet_var_parent_sku</th><th>dsheet_product_code</th><th>dsheet_qty</th>
    <th>dsheet_price</th></tr>
    <tr><th>Row ID</th><th>Sales channel</th><th>P.SKU</th>
    <th>SKU</th><th>Qty</th><th>Price</th></tr>
    <tr><td></td><td>4512957</td><td>x</td><td>FUN</td><td>US</td><td>Title</td>
    <td>a</td><td>JJPC0029</td><td>21.9</td><td>0.00</td><td></td><td></td></tr>
    </table>
    """
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=page_html)
        import urllib.parse

        seen["url"] = str(request.url)
        seen["pairs"] = urllib.parse.parse_qsl(
            (await request.aread()).decode(), keep_blank_values=True
        )
        return httpx.Response(200, text=result_html)

    browser = SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=str(tmp_path)))
    browser.active_shop = {"id": "159", "name": "FUN"}
    monkeypatch.setattr(
        browser, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    found = asyncio.run(browser._search_via_http("", "US", "FUN", only_listed=False))

    pairs = dict(seen["pairs"])
    assert pairs["listactive"] == ""            # include_unlisted → 全部
    assert pairs["listdate"] == ""              # 日期留空
    assert pairs["listshop[]"] == "159"         # 店铺按 id
    assert pairs["limit"] == "200"
    assert found["row_count"] == 1
    assert found["total_records"] == 42
    assert browser._last_sql_token == "tok/9"

    listed = asyncio.run(browser._search_via_http("", "US", "FUN", only_listed=True))
    assert dict(seen["pairs"])["listactive"] == "All"
    assert listed["row_count"] == 1


def test_session_alive_http_stops_at_first_login_redirect() -> None:
    """cookie 过期时登录页会自环 302：只能发 1 个请求就判失效，不能跟 20 跳。"""

    import asyncio
    from dataclasses import replace

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    profile = replace(Settings.load(), soldeazy_profile_dir="")
    browser = mod.SoldeazyBrowser(profile)
    # 伪造一份 cookie，让校验真的发请求
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            302, headers={"location": "/app/soldeazy/login?login_redirect=x"}
        )

    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler), follow_redirects=kw.get("follow_redirects", False)
    )

    assert asyncio.run(browser.session_alive_http()) is False
    assert calls["n"] == 1


def test_session_alive_http_true_on_table_marker() -> None:
    import asyncio
    from dataclasses import replace

    import httpx

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=""))
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='<table id="tbl_dSheet"></table>')
        )
    )
    assert asyncio.run(browser.session_alive_http()) is True


def test_export_redirect_means_login_required(tmp_path) -> None:
    """过期 cookie 下导出被 302 到登录页：报 LOGIN_REQUIRED，不是莫名其妙的 PK 错。"""

    import asyncio
    from dataclasses import replace

    import httpx
    import pytest

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.errors import SoldeazyError
    from soldeazy_mcp_server.providers import soldeazy as mod

    browser = mod.SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=str(tmp_path)))
    browser._cookie_dict = lambda: {"a": "1"}  # type: ignore[method-assign]
    browser._http_client = lambda **kw: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "/app/soldeazy/login?x=1"}
            )
        )
    )
    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.export_via_api(tmp_path / "x.xlsx"))
    assert caught.value.code == "SOLDEAZY_LOGIN_REQUIRED"


def test_idle_release_closes_browser_after_idle() -> None:
    """空闲到点就关浏览器、交还 profile 锁；cookie 由 close() 落盘。"""

    import asyncio
    import dataclasses

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    browser = mod.SoldeazyBrowser(Settings.load())
    browser.settings = dataclasses.replace(
        browser.settings, soldeazy_browser_idle_seconds=0.05
    )
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)
        browser._context = None

    browser.close = fake_close  # type: ignore[method-assign]
    browser._context = object()

    async def run() -> None:
        browser._touch()
        await asyncio.sleep(0.25)

    asyncio.run(run())
    assert closed == [True]
    assert browser._context is None


def test_idle_release_disabled_when_zero() -> None:
    import dataclasses

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    browser = mod.SoldeazyBrowser(Settings.load())
    browser.settings = dataclasses.replace(
        browser.settings, soldeazy_browser_idle_seconds=0
    )
    browser._touch()
    assert browser._idle_task is None


def test_idle_release_defers_while_search_lock_held() -> None:
    """search 跑到一半绝不能被释放：持锁时顺延一轮。"""

    import asyncio
    import dataclasses

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers import soldeazy as mod

    browser = mod.SoldeazyBrowser(Settings.load())
    browser.settings = dataclasses.replace(
        browser.settings, soldeazy_browser_idle_seconds=0.05
    )
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    browser.close = fake_close  # type: ignore[method-assign]
    browser._context = object()

    async def run() -> None:
        async with browser._lock:
            browser._touch()
            await asyncio.sleep(0.12)
            assert closed == []          # 计时器到点但锁还在 → 顺延
        await asyncio.sleep(0.25)        # 放锁后的下一轮才真正释放
        assert closed == [True]

    asyncio.run(run())


def test_status_exposes_session_state_without_credentials(tmp_path) -> None:
    """无凭据时 status 必须明说，而不是让调用方去猜"为什么要人工登录"。"""

    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    settings = replace(
        Settings.load(),
        soldeazy_username="",
        soldeazy_password="",
        soldeazy_profile_dir=str(tmp_path / "profile"),
    )
    browser = SoldeazyBrowser(settings)
    state = asyncio.run(browser.status())

    assert state["credentials_configured"] is False
    assert state["session_file_exists"] is False
    assert state["session_cookie_count"] == 0
    assert state["session_save_error"] == ""


def test_save_session_records_error_instead_of_swallowing(tmp_path) -> None:
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    settings = replace(
        Settings.load(), soldeazy_profile_dir=str(tmp_path / "profile")
    )
    browser = SoldeazyBrowser(settings)
    browser._context = object()  # 有上下文但没有 cookies() 方法
    asyncio.run(browser._save_session())
    assert "AttributeError" in browser.session_save_error


def test_search_defaults_to_listed_only():
    import inspect

    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    sig = inspect.signature(SoldeazyBrowser.search)
    assert sig.parameters["only_listed"].default is True
    assert sig.parameters["only_listed"].kind is inspect.Parameter.KEYWORD_ONLY


def test_listed_status_selector_exists():
    from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

    assert hasattr(SoldeazyBrowser, "_select_status_listed")


def test_number_takes_amount_before_semicolon():
    """运费列常见 `0;8` 这种写法，取分号前那一段。"""

    from soldeazy_mcp_server.providers.soldeazy import _number

    assert _number("0;8") == 0.0
    assert _number("0;8.5") == 0.0
    assert _number("0；8") == 0.0
    assert _number("1.25;9") == 1.25
    assert _number("12.50") == 12.5
    assert _number("-") is None
    assert _number("") is None


def test_search_clears_date_filter_and_defaults_to_listed():
    """规则 1 与规则 2：状态默认已刊登，修改日期默认留空。

    用**假的页面对象**跑一遍 DOM 搜索通道，观察它实际调用了哪些动作，
    而不是检查源码里有没有这些方法名——后者在重构后就不再可靠。
    """

    import asyncio

    from soldeazy_mcp_server.providers import soldeazy as mod

    calls: list[str] = []

    class _Locator:
        def __init__(self, kind: str) -> None:
            self.kind = kind

        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            return None

        async def fill(self, *_a, **_k):
            return None

        async def click(self, **_k):
            return None

        def locator(self, *_a, **_k):
            return self

        async def count(self):
            return 0

        async def all_inner_texts(self):
            return []

        async def is_visible(self):
            return False

        async def select_option(self, *_a, **_k):
            return None

    class _Page:
        url = "https://stiger.soldeazy.com/app/soldeazy/datasheet/dsheet_list"

        def locator(self, *_a, **_k):
            return _Locator("any")

        async def evaluate(self, script, *args):
            return "" if "sql_cache_token" not in script else "tok"

        async def wait_for_load_state(self, *_a, **_k):
            return None

    class _Browser(mod.SoldeazyBrowser):
        def __init__(self) -> None:  # 不碰 settings/playwright
            self.settings = type("S", (), {"soldeazy_min_delay": 0})()
            self.base_url = "https://stiger.soldeazy.com"
            self._last_status = ""
            self._last_status_value = ""
            self._last_date = ""
            self._last_date_value = ""
            self._last_sql_token = ""
            self._page = None
            self._context = None

        async def _ensure_page(self):
            return _Page()

        async def _goto(self, *_a, **_k):
            return None

        async def _dismiss_popup(self):
            return None

        async def _locate(self, key, timeout=8000):
            calls.append(key)
            return _Locator(key)

        async def _clear_date_filter(self):
            calls.append("_clear_date_filter")

        async def _select_status_listed(self):
            calls.append("_select_status_listed")

        async def _select_status_all(self):
            calls.append("_select_status_all")

        async def _select_scope_whole_result(self):
            calls.append("_select_scope_whole_result")

        async def _read_page_limit(self):
            return 25

        async def _read_total_records(self):
            return 0

        async def _select_site(self, site):
            calls.append("_select_site")

    browser = _Browser()
    # 没有结果表 → 走"0 条"分支，不影响我们对动作顺序的断言
    asyncio.run(browser._search_via_page("", "US", "", True))

    assert "_clear_date_filter" in calls, "修改日期必须被清空"
    assert "_select_status_listed" in calls, "默认必须切到「已刊登」"
    # 已刊登时不该去清成"全部"
    assert "_select_status_all" not in calls
