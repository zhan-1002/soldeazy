"""pytest 公共配置。

pytest 8.4 的 tmpdir 插件用 `Path.mkdir(mode=0o700)` 建临时目录（`_pytest/tmpdir.py`）。
在部分 Windows 受限令牌环境（例如某些沙箱）里，这样建出来的目录只带 OWNER RIGHTS
权限项，创建它的进程随后连列目录、读取都被拒绝，于是所有用 `tmp_path` 的用例整片
`PermissionError`。

普通 Windows 终端不受影响（owner rights 正常生效），所以这里不无条件改行为：先在
临时目录实测一次 0o700 目录能不能读，只有确实读不了时才把 0o700 放宽成 0o777。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
from typing import Any

#: 确保导入的是**本仓库**的 ``src/soldeazy_mcp_server``。
#:
#: 如果不显式插入，当这台机器上还装着别的同名包（例如从同一个工作区分出去的
#: 旧仓库），``import soldeazy_mcp_server`` 可能解析到那一份 —— 测试会因为
#: "代码不是这里的"而莫名其妙地失败，或者更糟：拿旧代码跑出"通过"。
_REPO_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))


def _owner_only_dirs_are_unreadable() -> bool:
    """实测 mode=0o700 建出来的目录当前进程能否读取。"""
    if sys.platform != "win32":
        return False

    probe = pathlib.Path(tempfile.gettempdir()) / f"pytest-mode-probe-{os.getpid()}"
    try:
        probe.mkdir(mode=0o700)
    except OSError:
        return True

    try:
        os.listdir(probe)
        return False
    except OSError:
        return True
    finally:
        try:
            probe.rmdir()
        except OSError:
            # 正好是复现的环境：这个空目录自己都删不掉，只能留着。
            pass


def pytest_configure(config: Any) -> None:
    if not _owner_only_dirs_are_unreadable():
        return

    original_mkdir = pathlib.Path.mkdir

    def mkdir(
        self: pathlib.Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if mode == 0o700:
            mode = 0o777
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    pathlib.Path.mkdir = mkdir  # type: ignore[method-assign]
