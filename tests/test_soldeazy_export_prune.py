"""中间导出文件不能无限增长，但也不能删掉"正在用的那份"。

背景：``read_datasheet_values`` 每次读取都会落一份全量导出（生产账号约
7 千行 / 250KB）。不清理就会一直堆（实测已积 16 份 / 1.7MB）。

但**不能简单用完即删**：``last_export_path`` 指向的那份会被
"生成人工核对文件"复用——如果读取时就删掉，人工核对会在无声无息中失效，
用户以为自己在核一份真实文件，实际什么都没生成。

所以策略是「保留最近 N 份，清掉更早的」：

- 磁盘不会无限增长；
- 正在用的那份一定还在（它是最新的）；
- 历史几份也留着，便于事后追溯。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from soldeazy_mcp_server.providers.soldeazy import (
    KEEP_EXPORT_FILES,
    _prune_old_exports,
)


def _make(directory: Path, name: str, age_seconds: float) -> Path:
    """造一个文件并把 mtime 设成"多少秒前"。"""

    path = directory / name
    path.write_bytes(b"x")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_keeps_only_the_most_recent_files(tmp_path) -> None:
    for index in range(10):
        _make(tmp_path, f"state_{index}.xlsx", age_seconds=1000 - index)

    removed = _prune_old_exports(tmp_path, keep=3)

    left = sorted(path.name for path in tmp_path.glob("state_*.xlsx"))
    assert removed == 7
    # 最新的 3 个（age 最小）留下
    assert left == ["state_7.xlsx", "state_8.xlsx", "state_9.xlsx"]


def test_newest_file_is_never_deleted(tmp_path) -> None:
    """正在用的那份一定是最新的——必须留着，否则核对文件生成不了。"""

    newest = _make(tmp_path, "state_newest.xlsx", age_seconds=0)
    for index in range(5):
        _make(tmp_path, f"state_old{index}.xlsx", age_seconds=500 + index)

    _prune_old_exports(tmp_path, keep=1)

    assert newest.exists(), "最新的导出文件被删了，人工核对会失效"


def test_default_keep_is_documented_constant() -> None:
    assert KEEP_EXPORT_FILES >= 2, "至少要留 2 份，否则正在用+上一份都保不住"


def test_non_state_files_are_untouched(tmp_path) -> None:
    """只清 ``state_*.xlsx``，别的文件（计划、核对文件）不能碰。"""

    _make(tmp_path, "state_1.xlsx", age_seconds=100)
    _make(tmp_path, "state_2.xlsx", age_seconds=50)
    plan = tmp_path / "price-x.json"
    plan.write_text("{}", encoding="utf-8")
    review = tmp_path / "price-x.review-after.xlsx"
    review.write_bytes(b"x")

    _prune_old_exports(tmp_path, keep=1)

    assert plan.exists()
    assert review.exists()


def test_missing_directory_is_harmless(tmp_path) -> None:
    """目录还不存在时不该抛异常（首次运行就会遇到）。"""

    assert _prune_old_exports(tmp_path / "nope", keep=3) == 0


def test_keep_zero_or_negative_is_a_noop(tmp_path) -> None:
    """``keep <= 0`` 视为"不清理"，避免配置写错把文件全删了。"""

    _make(tmp_path, "state_a.xlsx", age_seconds=10)
    _make(tmp_path, "state_b.xlsx", age_seconds=5)

    assert _prune_old_exports(tmp_path, keep=0) == 0
    assert len(list(tmp_path.glob("state_*.xlsx"))) == 2


def test_empty_directory_returns_zero(tmp_path) -> None:
    assert _prune_old_exports(tmp_path, keep=3) == 0


def test_delete_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog) -> None:
    """删不掉（比如文件被占用）只记 warning，绝不能影响主流程。"""

    import logging

    for index in range(4):
        _make(tmp_path, f"state_{index}.xlsx", age_seconds=100 - index)

    original_unlink = Path.unlink

    def flaky(self, *args, **kwargs):
        if self.name == "state_0.xlsx":
            raise OSError(13, "in use")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)

    with caplog.at_level(logging.WARNING, logger="soldeazy_mcp.soldeazy"):
        removed = _prune_old_exports(tmp_path, keep=2)

    # 其余三份里有两份被删（keep=2 保留最新的两份）
    assert removed == 1 or removed == 2
    assert any("删不掉旧导出" in record.message for record in caplog.records)


def test_ordering_uses_mtime_not_filename(tmp_path) -> None:
    """文件名里是事件循环时间，重启后会变小——必须按 mtime 排。

    如果按文件名排，重启后新文件的"数字"可能比旧文件小，
    就会把**正在用的那份**当成"旧的"删掉。
    """

    # 文件名：新文件数字更小（模拟重启后事件循环时间归零）
    old = _make(tmp_path, "state_999999.xlsx", age_seconds=500)
    new = _make(tmp_path, "state_100.xlsx", age_seconds=0)

    _prune_old_exports(tmp_path, keep=1)

    assert new.exists(), "按文件名排序会把最新文件误删"
    assert not old.exists()


@pytest.mark.parametrize("count", [1, 2, 5])
def test_never_exceeds_keep(tmp_path, count: int) -> None:
    for index in range(count + 8):
        _make(tmp_path, f"state_{index}.xlsx", age_seconds=1000 - index)

    _prune_old_exports(tmp_path, keep=count)

    assert len(list(tmp_path.glob("state_*.xlsx"))) == count
