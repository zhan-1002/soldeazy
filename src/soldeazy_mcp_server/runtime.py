"""运行时单例：配置、浏览器客户端、FastMCP 实例。

单独一个模块是为了让 ``tools.py`` 能 ``from ... import runtime`` 拿到同一份
配置与浏览器对象，而 ``server.py`` 只负责命令行入口——这样既没有循环导入，
也保持"工具定义"与"启动方式"分离。
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from soldeazy_mcp_server.config import Settings, resolve_dir
from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("soldeazy_mcp")

settings = Settings.load()


def _prepare_temp_dir(settings: Settings) -> Path:
    """把 TEMP/TMP 指到工作区内的数据目录，并确保它存在。

    两个真实故障都由这一件事引起：

    1. **沙箱拒绝写系统临时目录** —— MCP 客户端常把服务进程关在只允许写工作区的
       沙箱里，Playwright 解压内核时写 ``%TEMP%`` 会被拒，报成
       ``SOLDEAZY_BROWSER_LAUNCH_DENIED``，看起来像"没装浏览器"。
    2. **新机器上 TEMP 指向一个不存在的目录** —— Playwright 解压时 EPERM。

    所以这里不等客户端注入：默认就在 ``<数据目录>/temp`` 下，并把它建出来。
    显式设了 ``SOLDEAZY_TEMP_DIR`` 时以它为准。
    """

    override = (os.getenv("SOLDEAZY_TEMP_DIR") or "").strip()
    target = Path(override).expanduser() if override else settings.data_dir / "temp"
    if not target.is_absolute():
        # 相对路径按**仓库根**解析，不用 cwd —— 与 profile 目录同一口径。
        # 客户端从 C:\Windows\System32 拉起时，按 cwd 会把内核解压到那里。
        target = resolve_dir(str(target))

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 建不出来就别改环境变量：让 Playwright 用它自己的默认值，
        # 真出错时 launch_error 会报出确切路径。
        logger.warning("临时目录不可用，沿用系统默认：%s", target)
        return target

    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(target)
    return target


TEMP_DIR = _prepare_temp_dir(settings)
soldeazy = SoldeazyBrowser(settings)


@asynccontextmanager
async def lifespan(_server):
    try:
        yield {}
    finally:
        await soldeazy.close()


mcp = FastMCP("soldeazy", lifespan=lifespan)
