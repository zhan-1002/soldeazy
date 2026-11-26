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


def _package_root() -> Path | None:
    """包所在仓库的根目录（``<repo>/src/soldeazy_mcp_server`` 的上两级）。

    能算出来就用它当默认基准，**不依赖进程的当前工作目录**。
    返回 None 表示当前是安装进 site-packages 的形态，此时无"仓库根"可言。
    """

    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent   # config.py → 包 → src → 仓库根
    return candidate if (candidate / "pyproject.toml").is_file() else None


def _env_file_candidates() -> list[Path]:
    """候选 ``.env`` 路径，按优先级排序。

    ``load_dotenv()`` 不带参数时只从**进程当前工作目录**往上找。MCP 客户端
    启动服务进程时给的 cwd 往往是客户端自己的工作目录（实测 Cursor 的
    工作区根），根本到不了仓库根 —— 于是 ``.env`` 里的凭据读不到，
    ``credentials_configured`` 一直是 false，登录只能人工点。

    所以显式按「``SOLDEAZY_ENV_FILE`` 指定 → 仓库根 → cwd」找。
    """

    candidates: list[Path] = []
    explicit = os.getenv("SOLDEAZY_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    root = _package_root()
    if root is not None:
        candidates.append(root / ".env")
    candidates.append(Path.cwd() / ".env")
    return candidates


def _load_env_file() -> Path | None:
    """加载第一个存在的 ``.env``，返回它的路径（都不存在则返回 None）。

    ``override=False``：已存在的真实环境变量优先，部署脚本注入的值不会被
    仓库里的 ``.env`` 覆盖。
    """

    for path in _env_file_candidates():
        if path.is_file():
            load_dotenv(path, override=False)
            return path
    return None


ENV_FILE = _load_env_file()


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
    """默认数据目录：**仓库内**的 ``.soldeazy-data``。

    为什么不用 ``%LOCALAPPDATA%``：MCP 客户端常给服务进程套沙箱，沙箱只允许写
    工作区，写本地数据目录会 EPERM。放在仓库内既满足沙箱，又能靠 .gitignore
    排除（里面是登录 cookie，不能入库）。

    **基准用包的安装位置，不用 ``Path.cwd()``**（2026-11-19 修）：

    原来这里写的是 ``Path.cwd()``，注释还辩解"``uv run`` 是从项目根启动的"。
    但推荐给客户端的启动方式是 ``.venv\\Scripts\\python.exe -m soldeazy_mcp_server``
    —— 这时 cwd 由客户端决定，可能是 ``C:\\Windows\\System32`` 或用户主目录。
    后果：profile、导出文件、计划与审计全写到那个目录去，既找不到也留了一地垃圾。

    顺序：环境变量 > 仓库根（由包位置推出）> cwd（兜底，如 site-packages 安装）。
    """

    root = _package_root()
    if root is not None:
        return (root / ".soldeazy-data").resolve()
    return (Path.cwd() / ".soldeazy-data").resolve()


def resolve_dir(raw: str) -> Path:
    """把配置里的目录解析成绝对路径（相对路径按仓库根，支持 ``~``）。"""

    path = Path(str(raw or ".")).expanduser()
    if path.is_absolute():
        return path
    root = _package_root()
    return ((root or Path.cwd()) / path).resolve()


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
            # 凭据读不到时，第一件要确认的就是"到底加载了哪个 .env"
            "env_file": str(ENV_FILE) if ENV_FILE else "",
        }
