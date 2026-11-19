"""被吞掉的异常必须留下痕迹。

为什么这条值得单独测：``except OSError: pass`` 是最难排查的一类问题。

最糟的例子曾经是 ``_ensure_temp_dirs``：TEMP 目录建不出来就 ``pass``，
然后 Playwright 在解压内核时报一个看起来完全无关的 ``EPERM``——
排查的人会以为是"没装浏览器"或"权限问题"，根本想不到是临时目录。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from soldeazy_mcp_server.providers import soldeazy as mod
from soldeazy_mcp_server.providers import soldeazy_browser as browser_mod


def test_temp_dir_failure_is_logged(monkeypatch, caplog) -> None:
    """建不出临时目录时要 warning，而不是静默 pass。"""

    def boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    monkeypatch.setenv("TEMP", str(Path("C:/nope/temp")))

    with caplog.at_level(logging.WARNING, logger="soldeazy_mcp.soldeazy"):
        mod.SoldeazyBrowser._ensure_temp_dirs()

    assert any("临时目录建不出来" in record.message for record in caplog.records)
    assert any("EPERM" in record.message for record in caplog.records)


def test_temp_dir_success_stays_quiet(tmp_path, caplog) -> None:
    """正常情况不该产生噪音。"""

    monkeypatch_target = tmp_path / "temp"
    import os

    old = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(monkeypatch_target)
    try:
        with caplog.at_level(logging.WARNING, logger="soldeazy_mcp.soldeazy"):
            mod.SoldeazyBrowser._ensure_temp_dirs()
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    assert tmp_path.joinpath("temp").is_dir()
    assert not [r for r in caplog.records if "临时目录" in r.message]


def test_browser_cache_write_failure_is_logged(monkeypatch, caplog, tmp_path) -> None:
    """写不进浏览器选择缓存只影响"下次启动慢一点"，但必须留痕。"""

    def boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", boom)

    with caplog.at_level(logging.WARNING, logger="soldeazy_mcp.browser"):
        browser_mod.save_cache(tmp_path, {"channel": "chrome", "executable": ""})

    assert any("浏览器选择缓存写不进去" in r.message for r in caplog.records)


def test_audit_corrupt_line_is_skipped_but_logged(tmp_path, caplog) -> None:
    """坏行跳过是对的，但要知道它跳过了。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import read_audit

    directory = tmp_path / "plans"
    directory.mkdir()
    (directory / "p.json").write_text(
        '{"plan_id": "p", "site": "US", "kind": "price"}', encoding="utf-8"
    )
    (directory / "p.audit.jsonl").write_text(
        "not json\n" '{"at": "2026-11-18T00:00:00+00:00", "plan_id": "p"}\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="soldeazy_mcp.apply"):
        records = read_audit(directory)

    assert len(records) == 1                       # 坏行已跳过
    assert records[0]["site"] == "US"              # 好行正常解析
    assert any("不是 JSON" in r.message for r in caplog.records)


def test_manifest_corrupt_line_is_skipped_but_logged(tmp_path, caplog) -> None:
    from soldeazy_mcp_server.providers.soldeazy_apply import read_manifest

    path = tmp_path / "m.jsonl"
    path.write_text('garbage\n{"token": "a"}\n', encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="soldeazy_mcp.apply"):
        records = read_manifest(path)

    assert [r["token"] for r in records] == ["a"]
    assert any("不是 JSON" in r.message for r in caplog.records)


def test_unreadable_audit_file_is_logged_not_silent(tmp_path, caplog, monkeypatch) -> None:
    """整份审计文件读不了时，必须说清是哪一份。"""

    from soldeazy_mcp_server.providers.soldeazy_apply import read_audit

    directory = tmp_path / "plans"
    directory.mkdir()
    (directory / "bad.audit.jsonl").write_text("{}", encoding="utf-8")

    original = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.name.endswith(".audit.jsonl"):
            raise OSError(13, "locked")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with caplog.at_level(logging.WARNING, logger="soldeazy_mcp.apply"):
        records = read_audit(directory)

    assert records == []
    assert any("跳过读不了的审计文件" in r.message for r in caplog.records)


@pytest.mark.parametrize("name", ["TEMP", "TMP", "TMPDIR"])
def test_every_temp_variable_failure_is_reported(monkeypatch, caplog, name: str) -> None:
    """三个环境变量都要覆盖——只报第一个会漏掉后面两个。"""

    for other in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "C:/definitely/not/here")

    def boom(*_args, **_kwargs):
        raise OSError(13, "denied")

    monkeypatch.setattr(Path, "mkdir", boom)

    with caplog.at_level(logging.WARNING, logger="soldeazy_mcp.soldeazy"):
        mod.SoldeazyBrowser._ensure_temp_dirs()

    assert any(name in r.message for r in caplog.records)
