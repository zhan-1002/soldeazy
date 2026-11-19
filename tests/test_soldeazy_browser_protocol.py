"""``SoldeazyBrowser`` 必须满足执行链路声明的协作接口。

``soldeazy_apply`` / ``soldeazy_change`` 把第一个参数标成 ``DatasheetBrowser``
（Protocol），意思是"只要满足这些方法和属性就能跑"。这组测试守住两件事：

1. **真实浏览器满足协议** —— 否则执行链路在运行时才炸；
2. **协议覆盖了实际用到的东西** —— 协议漏了某个方法，类型检查就形同虚设，
   所以这里用"静态提取实际调用"的方式反向校验。

第 2 条是关键：``mypy`` 在没装的环境里跑不了，但"代码里用了什么"可以
用正则静态扫出来，和协议声明比对。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from soldeazy_mcp_server.providers.datasheet_browser import DatasheetBrowser
from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser

SRC = Path(__file__).resolve().parents[1] / "src" / "soldeazy_mcp_server"


def _declared_members() -> set[str]:
    """从 Protocol 类里读出声明的方法名与属性名。"""

    import ast

    tree = ast.parse(
        (SRC / "providers" / "datasheet_browser.py").read_text(encoding="utf-8")
    )
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DatasheetBrowser"
    )
    names: set[str] = set()
    # 注解赋值 = 属性；FunctionDef = 方法
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _used_members() -> set[str]:
    """从调用方源码里静态提取 ``browser.X`` 与 ``getattr(browser, "X")``。"""

    used: set[str] = set()
    for name in ("soldeazy_apply.py", "soldeazy_change.py"):
        text = (SRC / "providers" / name).read_text(encoding="utf-8")
        used.update(re.findall(r"\bbrowser\.(\w+)", text))
        used.update(re.findall(r"getattr\(\s*browser,\s*[\"'](\w+)", text))
    return used


def test_real_browser_satisfies_the_protocol() -> None:
    """真实浏览器必须满足协议，否则执行链路会在运行时才炸。

    注意：5 个状态属性是在 ``__init__`` 里赋值的**实例属性**，
    所以必须用实例检查（``hasattr(类, ...)`` 查不到它们）。
    这里不构造完整浏览器（会碰 settings/profile），只造一个最小实例。
    """

    import tempfile
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings

    browser = SoldeazyBrowser(
        replace(
            Settings.load(),
            soldeazy_profile_dir=str(Path(tempfile.mkdtemp()) / "p"),
        )
    )

    missing = [
        member
        for member in _declared_members()
        if not member.startswith("_") and not hasattr(browser, member)
    ]
    assert missing == [], f"SoldeazyBrowser 缺少协议要求：{missing}"

    # Protocol 的运行时检查也应该通过
    assert isinstance(browser, DatasheetBrowser)


def test_protocol_covers_everything_the_callers_actually_use() -> None:
    """协议漏一个成员，类型检查就漏一片——这条守住"协议 ⊇ 实际使用"。"""

    declared = _declared_members()
    used = _used_members()

    # 调用方用到但协议没声明的（排除内置方法）
    uncovered = sorted(used - declared - {"append", "get", "copy", "items"})
    assert uncovered == [], (
        f"这些成员被调用但协议没声明：{uncovered}；"
        "请在 datasheet_browser.py 里补上"
    )


@pytest.mark.parametrize(
    "member",
    [
        "active_shop",
        "last_export_shops",
        "last_export_warnings",
        "last_queue_ownership_warning",
        "last_export_path",
    ],
)
def test_protocol_declares_the_state_attributes(member: str) -> None:
    """协议里要写明调用方读哪些状态属性，否则类型检查不知道它们存在。"""

    assert member in _declared_members()


@pytest.mark.parametrize(
    "member",
    [
        "search",
        "export_via_api",
        "read_datasheet_values",
        "upload_via_api",
        "sync_up_via_api",
        "queue_jobs",
        "queue_result",
    ],
)
def test_protocol_declares_the_async_methods(member: str) -> None:
    assert member in _declared_members()


def test_protocol_is_runtime_checkable() -> None:
    """能在运行时做 ``isinstance`` 检查，测试里才能拿假对象直接验。"""

    class _Duck:
        active_shop = None
        last_export_shops: list[str] = []
        last_export_warnings: list[str] = []
        last_queue_ownership_warning = ""
        last_export_path = None
        last_apply_export = None

        async def search(self, *a, **k): ...
        async def export_via_api(self, *a, **k): ...
        async def read_datasheet_values(self, *a, **k): ...
        async def upload_via_api(self, *a, **k): ...
        async def sync_up_via_api(self, *a, **k): ...
        async def queue_jobs(self, *a, **k): ...
        async def queue_result(self, *a, **k): ...

    assert isinstance(_Duck(), DatasheetBrowser)


def test_a_class_missing_a_method_is_rejected() -> None:
    """反例：少一个方法就不算满足协议——证明这个检查真的有效。"""

    class _Incomplete:
        active_shop = None
        last_export_shops: list[str] = []
        last_export_warnings: list[str] = []
        last_queue_ownership_warning = ""
        last_export_path = None

        async def search(self, *a, **k): ...
        # 故意不实现 export_via_api 及其余方法

    assert not isinstance(_Incomplete(), DatasheetBrowser)
