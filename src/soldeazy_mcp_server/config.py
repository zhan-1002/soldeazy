"""配置：只包含 Soldeazy 链路需要的设置。

环境变量统一用 ``SOLDEAZY_*`` 前缀，避免与机器上其他服务撞名；
字段名也保持一致，``.env`` 与部署脚本可以直接对照。

读取顺序：**环境变量 > ``config.local.json`` 的 ``soldeazy`` 段 > 默认值**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _load_json_config() -> dict[str, Any]:
    explicit = os.getenv("SOLDEAZY_CONFIG_FILE", "").strip()
    candidates = [Path(explicit)] if explicit else [Path.cwd() / "config.local.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def _env(name: str, fallback: Any = "") -> Any:
    value = os.getenv(name)
    return value if value not in (None, "") else fallback


def _env_bool(name: str, fallback: bool) -> bool:
    value = str(_env(name, "1" if fallback else "0")).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _default_data_dir() -> Path:
    """默认数据目录：**工作区内**的 ``.soldeazy-data``。

    为什么不用 ``%LOCALAPPDATA%``：MCP 客户端常给服务进程套沙箱，沙箱只允许写
    工作区，写本地数据目录会 EPERM。放在工作区内既满足沙箱，又能靠 .gitignore
    排除（里面是登录 cookie，不能入库）。

    相对路径一律按 ``Path.cwd()`` 解析——``uv run`` 是从项目根启动的，
    所以 ``cwd`` 就是仓库根，不必依赖客户端注入绝对路径。
    """

    return (Path.cwd() / ".soldeazy-data").resolve()


def resolve_dir(raw: str) -> Path:
    """把配置里的目录解析成绝对路径（相对路径按 cwd，支持 ``~``）。"""

    path = Path(str(raw or ".")).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


@dataclass(frozen=True)
class Settings:
    """Soldeazy 运行参数。

    字段名带 ``soldeazy_`` 前缀，与对应的环境变量一一对应（``SOLDEAZY_BASE_URL``
    ↔ ``soldeazy_base_url``），部署时不必两处记不同名字。
    """

    soldeazy_base_url: str = "https://stiger.soldeazy.com"
    soldeazy_profile_dir: str = ""
    soldeazy_headless: bool = True
    soldeazy_browser_channel: str = "chrome"
    soldeazy_navigation_timeout: float = 45.0
    soldeazy_min_delay: float = 2.0
    #: 浏览器空闲多少秒后自动释放（交还 profile 锁）。0 = 不自动释放。
    soldeazy_browser_idle_seconds: float = 300.0
    soldeazy_username: str = ""
    soldeazy_password: str = ""
    soldeazy_write_enabled: bool = False
    soldeazy_export_profile: str = "55"
    soldeazy_manifest_path: str = ""

    @classmethod
    def load(cls) -> Settings:
        raw = _load_json_config()
        # 允许 config.local.json 里用 soldeazy 段落集中放，环境变量仍优先
        section = raw.get("soldeazy") if isinstance(raw.get("soldeazy"), dict) else {}

        def text(name: str, key: str, fallback: str) -> str:
            return str(_env(name, section.get(key) or fallback)).strip()

        def number(name: str, key: str, fallback: float) -> float:
            return float(_env(name, section.get(key, fallback)))

        def bounded(
            name: str, key: str, fallback: float, low: float, high: float
        ) -> float:
            return max(low, min(number(name, key, fallback), high))

        return cls(
            soldeazy_base_url=text(
                "SOLDEAZY_BASE_URL", "base_url", "https://stiger.soldeazy.com"
            ),
            soldeazy_profile_dir=text(
                "SOLDEAZY_PROFILE_DIR",
                "profile_dir",
                str(_default_data_dir() / "profile"),
            ),
            soldeazy_headless=_env_bool(
                "SOLDEAZY_HEADLESS", bool(section.get("headless", True))
            ),
            soldeazy_browser_channel=text(
                "SOLDEAZY_BROWSER_CHANNEL", "browser_channel", "chrome"
            ),
            soldeazy_navigation_timeout=bounded(
                "SOLDEAZY_NAVIGATION_TIMEOUT", "navigation_timeout", 45, 5.0, 180.0
            ),
            soldeazy_min_delay=bounded(
                "SOLDEAZY_MIN_DELAY", "min_delay", 2, 0.0, 60.0
            ),
            soldeazy_browser_idle_seconds=bounded(
                "SOLDEAZY_BROWSER_IDLE_SECONDS",
                "browser_idle_seconds",
                300,
                0.0,
                3600.0,
            ),
            soldeazy_username=text("SOLDEAZY_USERNAME", "username", ""),
            soldeazy_password=text("SOLDEAZY_PASSWORD", "password", ""),
            soldeazy_write_enabled=_env_bool(
                "SOLDEAZY_WRITE_ENABLED", bool(section.get("write_enabled", False))
            ),
            soldeazy_export_profile=text(
                "SOLDEAZY_EXPORT_PROFILE", "export_profile", "55"
            ),
            soldeazy_manifest_path=text("SOLDEAZY_MANIFEST_PATH", "manifest_path", ""),
        )

    @property
    def data_dir(self) -> Path:
        """计划、审计、临时导出都放在 profile 的兄弟目录下。"""

        return resolve_dir(self.soldeazy_profile_dir or ".").parent

    @property
    def profile_path(self) -> Path | None:
        """profile 绝对路径；没配就是 None（与拆分前的语义一致）。"""

        raw = (self.soldeazy_profile_dir or "").strip()
        return resolve_dir(raw) if raw else None

    def public_status(self) -> dict[str, Any]:
        return {
            "base_url": self.soldeazy_base_url,
            "profile_configured": bool(self.soldeazy_profile_dir),
            "write_enabled": self.soldeazy_write_enabled,
            "credentials_configured": bool(
                self.soldeazy_username and self.soldeazy_password
            ),
        }
