"""站点边界必须从页面读，且写错不许静默退回「全部站点」。

为什么要有这一组测试（2026-11-17 用户纠正）：

原来站点用的是硬编码 ``SITE_CODES`` 表（12 个），而 ``SITE_LABELS`` 有 15 个——
缺 SG / HK / MY。查不到时 ``.get(..., "")`` 返回空串，而空串在平台语义里
是「**不筛选站点**」。于是「改 SG 的价格」会静默改成所有站点的同 SKU。

而店铺从一开始就是从页面读的（``parse_shop_options``）。店铺和站点是同一类
东西（搜索表单的可选边界），必须用同一种方式获取。

核心不变量：
1. 站点选项来自**页面 HTML/下拉**，不是代码常量；
2. 站点为空 → 全站（平台语义，允许）；
3. 站点给了但匹配不上 → **明确报错**，绝不退回空串；
4. HTTP 与 DOM 两条通道用**同一个解析结果**，不许各自猜。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from soldeazy_mcp_server.config import Settings
from soldeazy_mcp_server.errors import SoldeazyError
from soldeazy_mcp_server.providers.soldeazy import (
    SoldeazyBrowser,
    parse_site_options,
)

# 真实页面结构：空 value 是「全部站点」，其余是英文显示名。
# value 全部来自 2026-11-18 的线上实测（`select[name=listsite]` 的真实选项）。
SITE_SELECT_HTML = """
<select name="listsite" id="listsite">
  <option value="">-- 全部站点 --</option>
  <option value="0">US</option>
  <option value="3">UK</option>
  <option value="77">Germany</option>
  <option value="15">Australia</option>
  <option value="216">Singapore</option>
  <option value="201">HongKong</option>
  <option value="207">Malaysia</option>
</select>
"""


def test_parse_site_options_reads_real_options() -> None:
    options = parse_site_options(SITE_SELECT_HTML)

    ids = [o["id"] for o in options]
    names = [o["name"] for o in options]
    assert "" in ids                       # 「全部站点」保留，不丢
    assert "216" in ids                    # SG 的真实 value 来自页面
    assert "201" in ids                    # HK
    assert "207" in ids                    # MY
    assert "Singapore" in names
    assert len(options) == 8


def test_parse_site_options_returns_empty_without_the_select() -> None:
    assert parse_site_options("<html>no form</html>") == []


def _browser(tmp_path, html: str = SITE_SELECT_HTML) -> SoldeazyBrowser:
    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "p"))
    )

    async def fake_list() -> list[dict[str, str]]:
        return parse_site_options(html)

    browser.list_site_options = fake_list  # type: ignore[method-assign]
    return browser


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("US", "0"),
        ("us", "0"),            # 大小写不敏感
        ("  UK  ", "3"),        # 空白容忍
        ("DE", "77"),           # 别名 -> Germany
        ("Germany", "77"),      # 显示名直接命中
        ("SG", "216"),          # 曾经缺的那个：现在从页面读到了
        ("Singapore", "216"),
        ("HK", "201"),
        ("MY", "207"),
        ("207", "207"),         # 直接给 value 也认
    ],
)
def test_resolve_site_code_matches_precisely(tmp_path, requested, expected) -> None:
    browser = _browser(tmp_path)
    assert asyncio.run(browser.resolve_site_code(requested)) == expected


def test_empty_site_means_all_sites(tmp_path) -> None:
    """空站点是合法语义：平台不勾选 = 全部站点。"""

    browser = _browser(tmp_path)
    assert asyncio.run(browser.resolve_site_code("")) == ""
    assert asyncio.run(browser.resolve_site_code("   ")) == ""


@pytest.mark.parametrize("bad", ["USA", "XX", "火星", "Germ"])
def test_unknown_site_raises_instead_of_falling_back_to_all(tmp_path, bad) -> None:
    """**最关键的一条**：匹配不上必须报错，不能退回空串。

    退回空串 = 不筛选站点 = 把改动写到别的站点上。这是静默数据损坏。
    """

    browser = _browser(tmp_path)

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.resolve_site_code(bad))

    assert caught.value.code == "SOLDEAZY_SITE_NOT_FOUND"
    # 报错里要列出可用站点，让人自己改
    available = caught.value.details["available"]
    assert any(item["name"] == "Singapore" for item in available)
    assert "全部站点" in caught.value.message


def test_missing_site_select_refuses_to_guess(tmp_path) -> None:
    """读不到站点下拉时，也不能当成"全部站点"继续。"""

    browser = _browser(tmp_path, html="<html>nothing</html>")

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.resolve_site_code("US"))

    assert caught.value.code == "SOLDEAZY_SITE_LIST_EMPTY"


def test_export_reuses_the_code_resolved_by_search(tmp_path) -> None:
    """导出必须沿用 search 解析出的 site code，不许再查一遍常量表。

    两处取法不一致就会出现"搜的是 SG、导的是全站"，计划行集直接错位。
    """

    browser = _browser(tmp_path)
    browser._last_site_code = "555"
    browser._last_site = "SG"
    browser._last_query = ""
    browser._last_status_value = "All"

    captured: dict = {}

    class _Resp:
        status = 200
        status_code = 200
        content = b"PK-not-really-xlsx"
        text = ""

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, **kwargs):
            captured.update(kwargs.get("data") or {})
            return _Resp()

    async def alive() -> bool:
        return True

    browser.session_alive_http = alive  # type: ignore[method-assign]
    browser._http_client = lambda **_kw: _Client()  # type: ignore[method-assign]

    import tempfile
    from pathlib import Path

    dest = Path(tempfile.mkdtemp()) / "x.xlsx"
    # 返回的 body 不是真 xlsx → 会报 EXPORT_API_FAILED；
    # 我们只关心它**发出去的 form 里 listsite 是不是 search 解析出的那个**
    try:
        asyncio.run(browser.export_via_api(dest))
    except SoldeazyError:
        pass

    assert captured.get("listsite") == "555", captured


def test_site_outside_the_hardcoded_table_is_not_silently_widened(tmp_path) -> None:
    """行为守住：站点不在历史硬编码表里时，不许静默退化成「全部站点」。

    这个测试替代了原来的源码文本断言（``assert "SITE_CODES.get" not in src``），
    因为那种断言只能拦住"字面写法"，拦不住真正的问题。

    真正的问题是：``SITE_CODES.get(站点, "")`` 返回空串，而空串在平台语义里
    是「不筛选站点」。所以这里用一个**历史硬编码表里没有的站点**，
    验证两条路径都不会把它变成全站：

    - 能解析出来 → 返回那个站点的真实 value（来自页面选项）；
    - 解析不出来 → 报错。
    """

    import asyncio
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.errors import SoldeazyError
    from soldeazy_mcp_server.providers.soldeazy import (
        SITE_CODES,
        SITE_LABELS,
        SoldeazyBrowser,
    )

    # 用一个**不在历史表里**的站点（Norway 从未出现在 SITE_CODES 中）。
    # 2026-11-18 补全站点表后，SG/HK/MY 已经在表里了，不能再拿它们当反例。
    assert "NO" not in SITE_CODES
    assert "Norway" not in SITE_LABELS.values()

    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "p"))
    )

    async def fake_list() -> list[dict[str, str]]:
        # 页面里**有** Norway（value=222）——历史表里没有但页面有，必须能解析
        html = SITE_SELECT_HTML.replace(
            "</select>", '<option value="222">Norway</option></select>'
        )
        return parse_site_options(html)

    browser.list_site_options = fake_list  # type: ignore[method-assign]

    # 页面里有 Norway → 用页面原名必须能解析成它的真实 value，而不是空串
    code = asyncio.run(browser.resolve_site_code("Norway"))
    assert code == "222"
    assert code != "", "解析出空串等于「不筛选站点」，会造成跨站改动"

    # 页面里没有的站点 → 必须报错，同样不能是空串
    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.resolve_site_code("ZZ"))
    assert caught.value.code == "SOLDEAZY_SITE_NOT_FOUND"


def test_site_codes_table_is_not_consulted_for_resolution(tmp_path) -> None:
    """即使站点在历史硬编码表里，value 也必须来自**页面**而不是那张表。

    做法：把页面里的 US value 改成和历史表不同的值（"999"），
    如果代码还在查 ``SITE_CODES``，就会返回 "0"；正确实现应返回 "999"。
    """

    import asyncio
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.providers.soldeazy import SITE_CODES, SoldeazyBrowser

    assert SITE_CODES.get("US") == "0"      # 历史表的既定值

    html_with_different_value = SITE_SELECT_HTML.replace(
        '<option value="0">US</option>', '<option value="999">US</option>'
    )
    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "p"))
    )

    async def fake_list() -> list[dict[str, str]]:
        return parse_site_options(html_with_different_value)

    browser.list_site_options = fake_list  # type: ignore[method-assign]

    assert asyncio.run(browser.resolve_site_code("US")) == "999"
