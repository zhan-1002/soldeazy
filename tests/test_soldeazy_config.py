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


def test_default_profile_dir_is_inside_the_repo(monkeypatch, tmp_path) -> None:
    """默认 profile 必须落在**仓库内**的 .soldeazy-data/profile。

    不能落在 cwd 推出的位置：客户端拉起服务时 cwd 不可控
    （可能是 C:\\Windows\\System32），那样会把 cookie 和导出文件写到那里去。
    """

    from soldeazy_mcp_server.config import _package_root

    settings = _clean_settings(monkeypatch, tmp_path)
    profile = settings.profile_path
    root = _package_root()

    assert root is not None, "测试应在本仓库内运行"
    assert profile is not None
    assert profile.is_absolute()
    assert root.resolve() in profile.parents
    assert profile.parent.name == ".soldeazy-data"
    assert profile.name == "profile"
    # 关键：不跟着 cwd 走
    assert tmp_path.resolve() not in profile.parents


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


def test_relative_profile_dir_resolves_against_repo_root(monkeypatch, tmp_path) -> None:
    """相对路径必须展开成绝对路径，否则父目录会算错、计划和审计写到别处。

    解析基准是**仓库根**（由包位置推出），不是 ``Path.cwd()``（2026-11-19 修）：
    推荐的启动方式是 ``python -m soldeazy_mcp_server``，此时 cwd 由客户端决定，
    可能是 ``C:\\Windows\\System32``。按 cwd 解析会把 profile 写到那里去。
    """

    from soldeazy_mcp_server.config import _package_root

    monkeypatch.setenv("SOLDEAZY_PROFILE_DIR", r".soldeazy-data\profile")
    monkeypatch.chdir(tmp_path)          # 故意换到别处，验证不受 cwd 影响
    settings = Settings.load()

    root = _package_root()
    assert root is not None, "测试应在本仓库内运行"
    assert settings.profile_path == (root / ".soldeazy-data" / "profile").resolve()
    assert settings.data_dir == (root / ".soldeazy-data").resolve()
    # 关键：不能跟着 cwd 走
    assert settings.profile_path != (tmp_path / ".soldeazy-data" / "profile").resolve()


def test_absolute_profile_dir_is_never_reinterpreted(monkeypatch, tmp_path) -> None:
    """绝对路径原样使用，不能被"仓库根"再拼一次。"""

    absolute = tmp_path / "somewhere" / "profile"
    monkeypatch.setenv("SOLDEAZY_PROFILE_DIR", str(absolute))
    settings = Settings.load()

    assert settings.profile_path == absolute
    assert settings.data_dir == absolute.parent


def test_relative_temp_dir_resolves_against_repo_root(monkeypatch, tmp_path) -> None:
    """``SOLDEAZY_TEMP_DIR`` 的相对路径也按仓库根解析，与 profile 同一口径。

    如果按 cwd 解析，客户端从 ``C:\\Windows\\System32`` 拉起时会把
    Playwright 内核解压到那里，然后 EPERM。
    """

    from soldeazy_mcp_server.config import _package_root, resolve_dir

    root = _package_root()
    assert root is not None

    monkeypatch.chdir(tmp_path)
    resolved = resolve_dir("my-temp")

    assert resolved == (root / "my-temp").resolve()
    assert tmp_path.resolve() not in resolved.parents


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
