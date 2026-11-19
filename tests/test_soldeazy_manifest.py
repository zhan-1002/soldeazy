"""``soldeazy-writes.jsonl`` 的读取契约（旧链路历史来源）。

这个文件原来的其他用例（令牌签名、一次性消费、漂移检查）测的是已停用的
「预览→令牌→写入」链路，那两个模块（soldeazy_write / soldeazy_pricing）已在
2026-11-17 的清理中删除，用例随之删除。

**``read_manifest`` 仍然活着**：``soldeazy_change_history`` 要把旧链路的写入记录
和新链路的 ``<plan_id>.audit.jsonl`` 合并返回，否则升级后历史看起来"突然变空"。
所以这里只保留它的契约测试，并改成直接写 jsonl（不再依赖已删掉的
``append_manifest``）。
"""

from __future__ import annotations

import json
from pathlib import Path

from soldeazy_mcp_server.providers.soldeazy_apply import read_manifest


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_read_manifest_returns_newest_first(tmp_path) -> None:
    path = tmp_path / "m.jsonl"
    _write(
        path,
        [
            {"token": "a", "site": "US", "kind": "price"},
            {"token": "b", "site": "DE", "kind": "stock"},
            {"token": "c", "site": "US", "kind": "stock"},
        ],
    )

    assert [r["token"] for r in read_manifest(path)] == ["c", "b", "a"]
    assert read_manifest(path, limit=2)[0]["token"] == "c"


def test_read_manifest_filters_by_site_and_kind(tmp_path) -> None:
    path = tmp_path / "m.jsonl"
    _write(
        path,
        [
            {"token": "a", "site": "US", "kind": "price"},
            {"token": "b", "site": "DE", "kind": "stock"},
            {"token": "c", "site": "us", "kind": "stock"},
        ],
    )

    assert [r["token"] for r in read_manifest(path, site="US")] == ["c", "a"]
    assert [r["token"] for r in read_manifest(path, site="us", kind="stock")] == ["c"]
    assert read_manifest(path, kind="price")[0]["token"] == "a"


def test_read_manifest_skips_corrupt_lines(tmp_path) -> None:
    """坏行（手工改坏、写一半）不能毁掉整份历史。"""

    path = tmp_path / "m.jsonl"
    path.write_text(
        '{"token": "a"}\n'
        "not json at all\n"
        "\n"
        "[1, 2, 3]\n"
        '{"token": "b"}\n',
        encoding="utf-8",
    )

    assert [r["token"] for r in read_manifest(path)] == ["b", "a"]


def test_read_manifest_handles_a_missing_file(tmp_path) -> None:
    assert read_manifest(tmp_path / "nope.jsonl") == []


def test_read_manifest_limit_zero_returns_nothing(tmp_path) -> None:
    path = tmp_path / "m.jsonl"
    _write(path, [{"token": "a"}])
    assert read_manifest(path, limit=0) == []
