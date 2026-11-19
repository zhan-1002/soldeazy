"""配置默认值：数据目录必须落在**工作区内**，且与既有计划目录一致。

为什么这是硬要求（不是偏好）：

- MCP 客户端常把服务进程关进只允许写工作区的沙箱。默认往
  ``%LOCALAPPDATA%`` 写，浏览器启动就会 EPERM，看起来像"没装浏览器"。
- 拆分前，实机数据已经在 ``<仓库>/.soldeazy-data``（登录态 + 计划 + 审计）。
  默认值如果指向别处，升级后就会"登录态凭空丢失、计划文件全不见"。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from soldeazy_mcp_server import runtime
from soldeazy_mcp_server.config import Settings, resolve_dir


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _clean_settings(monkeypatch, tmp_path) -> Settings:
    """去掉所有 SOLDEAZY_* 环境变量，从零加载一份默认配置。"""

    for name in (
        "SOLDEAZY_BASE_URL",
        "SOLDEAZY_PROFILE_DIR",
        "SOLDEAZY_HEADLESS",
        "SOLDEAZY_BROWSER_CHANNEL",
        "SOLDEAZY_NAVIGATION_TIMEOUT",
        "SOLDEAZY_MIN_DELAY",
        "SOLDEAZY_BROWSER_IDLE_SECONDS",
        "SOLDEAZY_USERNAME",
        "SOLDEAZY_PASSWORD",
        "SOLDEAZY_WRITE_ENABLED",
        "SOLDEAZY_EXPORT_PROFILE",
        "SOLDEAZY_MANIFEST_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    # 把 cwd 指到临时仓库根，验证"默认值跟着工作区走"
    monkeypatch.chdir(tmp_path)
    return Settings.load()


def test_default_profile_dir_is_inside_the_workspace(monkeypatch, tmp_path) -> None:
    settings = _clean_settings(monkeypatch, tmp_path)
    profile = settings.profile_path

    assert profile is not None
    assert profile.is_absolute()
    assert tmp_path.resolve() in profile.parents
    assert profile.parent.name == ".soldeazy-data"
    assert profile.name == "profile"


def test_default_layout_matches_the_existing_plan_directory(monkeypatch, tmp_path) -> None:
    """data_dir 必须就是 profile 的父目录，计划目录必须叫 soldeazy-plans。"""

    settings = _clean_settings(monkeypatch, tmp_path)

    assert settings.data_dir == settings.profile_path.parent
    assert settings.data_dir.name == ".soldeazy-data"
    assert (settings.data_dir / "soldeazy-plans").name == "soldeazy-plans"


def test_explicit_profile_dir_still_wins(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SOLDEAZY_PROFILE_DIR", str(tmp_path / "custom" / "profile"))
    settings = Settings.load()

    assert settings.profile_path == tmp_path / "custom" / "profile"
    assert settings.data_dir == tmp_path / "custom"


def test_relative_profile_dir_resolves_against_cwd(monkeypatch, tmp_path) -> None:
    """相对路径不展开就会算错父目录，计划和审计会写到别处去。"""

    monkeypatch.setenv("SOLDEAZY_PROFILE_DIR", r".soldeazy-data\profile")
    monkeypatch.chdir(tmp_path)
    settings = Settings.load()

    assert settings.profile_path == (tmp_path / ".soldeazy-data" / "profile").resolve()
    assert settings.data_dir == (tmp_path / ".soldeazy-data").resolve()


def test_empty_profile_dir_yields_none(monkeypatch) -> None:
    """没配 profile 时保持"没有 profile"的语义，不要凭空造一个路径出来。"""

    monkeypatch.setenv("SOLDEAZY_PROFILE_DIR", "")
    monkeypatch.setattr(
        "soldeazy_mcp_server.config._load_json_config", dict
    )
    monkeypatch.delenv("SOLDEAZY_PROFILE_DIR", raising=False)

    settings = dataclasses.replace(Settings.load(), soldeazy_profile_dir="")
    assert settings.profile_path is None


def test_resolve_dir_handles_tilde_and_absolute(tmp_path) -> None:
    assert resolve_dir("~/x") == Path.home() / "x"
    assert resolve_dir(str(tmp_path)) == tmp_path


def test_temp_dir_is_inside_the_data_dir() -> None:
    """沙箱只允许写工作区：Playwright 解压内核的 TEMP 也必须在工作区内。"""

    assert runtime.TEMP_DIR.is_absolute()
    assert runtime.TEMP_DIR.parent == runtime.settings.data_dir
    assert runtime.TEMP_DIR.name == "temp"
    assert runtime.TEMP_DIR.is_dir()  # runtime 导入时就建好了


def test_temp_env_points_at_that_dir() -> None:
    import os

    for name in ("TEMP", "TMP", "TMPDIR"):
        assert os.environ.get(name) == str(runtime.TEMP_DIR), name


def test_repo_gitignore_covers_the_data_dir() -> None:
    """数据目录里有登录 cookie，绝不能入库。"""

    ignore = (_repo_root() / ".gitignore").read_text(encoding="utf-8")
    assert ".soldeazy-data/" in ignore


@pytest.mark.parametrize("field", ["soldeazy_username", "soldeazy_password"])
def test_credentials_default_to_empty(field: str) -> None:
    """凭据只能来自 .env / 环境变量，不能有硬编码默认值。"""

    fields = {item.name: item for item in dataclasses.fields(Settings)}
    assert fields[field].default == ""


def test_write_tools_default_to_disabled() -> None:
    fields = {item.name: item for item in dataclasses.fields(Settings)}
    assert fields["soldeazy_write_enabled"].default is False
