"""两处实机测试中发现的真实缺陷，各补一组回归。

### 缺陷 1：浏览器启动失败后无法自愈

原实现：``_ensure_page`` 里只给 ``self._playwright`` 赋值，失败时不清理。
于是调用方"重试"时，``self._page`` 仍为 None，却又复用那个**半坏**的
playwright 实例 —— 重试必然继续失败。

实测症状（2026-11-18）：连续重试 5 次全失败，但**单独起一个进程就能成功**。

### 缺陷 2：站点表只覆盖 12/27

``SITE_CODES`` 缺 SG/HK/MY 等，而查不到时退回空串（= 全部站点）。
实测线上真实有 28 个站点（含空值"全部"），现在全部补齐。
"""

from __future__ import annotations

from soldeazy_mcp_server.providers.soldeazy import (
    SITE_CODES,
    SITE_LABELS,
    SoldeazyBrowser,
)

# ── 缺陷 1：启动失败要能重试 ──────────────────────────────────────────


class _BrokenPlaywright:
    """模拟"start() 成功但 launch 失败"的半坏实例。"""

    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def test_failed_launch_clears_the_playwright_instance(monkeypatch, tmp_path) -> None:
    """启动失败后必须把 self._playwright 清掉，否则重试复用坏实例必失败。"""

    import asyncio
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings
    from soldeazy_mcp_server.errors import SoldeazyError

    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "profile"))
    )

    broken = _BrokenPlaywright()

    async def fake_launch(_directory):
        raise SoldeazyError("SOLDEAZY_TEST_LAUNCH", "模拟启动失败")

    # 替换 playwright 启动与 launch
    import soldeazy_mcp_server.providers.soldeazy as mod

    class _FakeApi:
        async def start(self):
            return broken

    monkeypatch.setattr(mod, "_selector_available", lambda: True)
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: _FakeApi(), raising=False
    )
    monkeypatch.setattr(browser, "_launch_context", fake_launch)

    try:
        asyncio.run(browser._ensure_page())
    except Exception:  # noqa: BLE001 - 预期失败
        pass

    # 关键断言：半坏实例已被拆掉，且被 stop 过
    assert browser._playwright is None, "失败后 self._playwright 没清掉，重试会复用坏实例"
    assert browser._context is None
    assert browser._page is None
    assert broken.stopped is True, "半坏实例没被 stop，会泄漏"


def test_teardown_does_not_touch_the_session_file(tmp_path) -> None:
    """``_teardown_browser`` 是"失败清理"，不该写会话文件。

    正常退出（``close()``）才落盘 cookie；失败清理时没有有效会话可存。
    """

    import asyncio
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings

    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "profile"))
    )
    session_file = browser._session_file()
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("[]", encoding="utf-8")
    before = session_file.read_text(encoding="utf-8")

    asyncio.run(browser._teardown_browser())

    assert session_file.read_text(encoding="utf-8") == before


# ── 缺陷 2：站点表覆盖度 ──────────────────────────────────────────────


def test_site_table_matches_the_28_real_options() -> None:
    """线上实测 28 项 = 27 个真实站点 + 1 个"全部"。"""

    assert len(SITE_CODES) == 27, f"应为 27 个站点，实际 {len(SITE_CODES)}"


def test_all_site_values_are_unique() -> None:
    """两个站点共用一个 value 会导致"选 A 改了 B"。"""

    values = list(SITE_CODES.values())
    assert len(values) == len(set(values)), "SITE_CODES 里有重复 value"


def test_known_sites_from_the_live_probe_are_present() -> None:
    """这些值是 2026-11-18 从线上页面逐个读下来的，不能随便改。"""

    expected = {
        "US": "0",
        "CA": "2",
        "UK": "3",
        "AU": "15",
        "AT": "16",
        "BE": "23",
        "FR": "71",
        "DE": "77",
        "IT": "101",
        "NL": "146",
        "ES": "186",
        "CH": "193",
        "HK": "201",
        "IN": "203",
        "IE": "205",
        "MY": "207",
        "PL": "212",
        "RU": "215",
        "SG": "216",
        "JP": "903",
    }
    for code, value in expected.items():
        assert SITE_CODES.get(code) == value, f"{code} 的 value 变了"


def test_every_label_maps_to_a_known_code() -> None:
    """``SITE_LABELS`` 是别名表，每个别名都要能翻到一个真实站点。

    否则用户写 ``SG`` 会被翻成 ``Singapore`` 却找不到对应 value——
    比"根本不认识 SG"更让人困惑。
    """

    orphans = [code for code in SITE_LABELS if code not in SITE_CODES]
    assert orphans == [], f"这些别名没有对应的站点 value：{orphans}"


def test_every_label_value_is_the_english_site_name() -> None:
    """别名表的 value 必须是页面上的**原名**（用于精确匹配下拉）。"""

    names = set(SITE_LABELS.values())
    # 抽查几个典型的
    assert SITE_LABELS["DE"] == "Germany"
    assert SITE_LABELS["SG"] == "Singapore"
    assert SITE_LABELS["HK"] == "HongKong"
    assert SITE_LABELS["MY"] == "Malaysia"
    assert "Germany" in names


def test_no_empty_value_in_the_code_table() -> None:
    """空 value 表示"全部站点"，绝不能混进真实站点表。"""

    assert "" not in SITE_CODES.values()
    assert "" not in SITE_CODES
