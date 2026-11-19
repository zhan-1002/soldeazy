"""预览返回超限时必须**降级**，不能把整个调用拒掉。

背景（2026-11-18 实测）：4 个 SKU 的 dry_run 预览返回 1734 token，
超过 4000 字符闸门，于是整个调用被拒：

    {"success": false, "error": {"code": "SOLDEAZY_OUTPUT_TOO_LARGE"}}

但预览是"决定要不要执行"的唯一依据 —— 拒掉它，用户看到的只是
"返回太大"，而不是"你将要改这 34 条"。防 token 浪费反而让人拿不到任何结果。

所以超限时要砍可再生的部分（逐条样本、提醒条数），保留判决本身。
"""

from __future__ import annotations

import json

from soldeazy_mcp_server import tools


def _payload(rows: int = 40, confirms: int = 6) -> dict:
    return {
        "ok": True,
        "plan_id": "p1",
        "items": rows,
        "datasheets": rows,
        "preview": {
            "概览": [{"字段": "价格（F 列）", "条数": rows, "涉及数据表": rows, "方向": "涨"}],
            "作用范围": {"账号": ["FUN"], "站点": ["US"]},
            "逐条样本": [
                {"数据表": f"3117{i:03d}", "SKU": f"SKU{i}", "字段": "价格",
                 "原值": 27.48, "新值": 28.48}
                for i in range(rows)
            ],
        },
        "must_confirm": [
            f"**提醒 {i}**：" + "这是一句比较长的中文提醒，用来模拟真实返回里的体积。" * 3
            for i in range(confirms)
        ],
    }


def _size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def test_shrink_brings_an_oversized_preview_under_the_limit() -> None:
    payload = _payload(rows=120, confirms=6)
    assert _size(payload) > tools.MAX_OUTPUT_CHARS, "构造的样本应当先超限"

    shrunk = tools._shrink_preview(payload)
    assert _size(shrunk) <= tools.MAX_OUTPUT_CHARS, "降级后必须落回闸门内"


def test_shrink_keeps_the_verdict_intact() -> None:
    """砍掉的只能是可再生的明细，判决字段一个不能少。"""

    shrunk = tools._shrink_preview(_payload())

    assert shrunk["ok"] is True
    assert shrunk["items"] == 40
    assert shrunk["datasheets"] == 40
    assert shrunk["plan_id"] == "p1"
    # 概览和作用范围回答"改哪些、改多少"，必须留着
    assert shrunk["preview"]["概览"]
    assert shrunk["preview"]["作用范围"]["账号"] == ["FUN"]
    # 至少要有一条样本让人看到改动形态
    assert shrunk["preview"]["逐条样本"]


def test_shrink_marks_what_it_trimmed() -> None:
    """截断必须留痕，否则人以为那就是全部。"""

    shrunk = tools._shrink_preview(_payload(rows=40, confirms=6))

    assert shrunk["preview_truncated"]["逐条样本"] is True
    assert shrunk["preview_truncated"]["逐条样本总数"] == 40
    assert "review_after.xlsx" in shrunk["preview_truncated"]["降级原因"]
    assert shrunk["must_confirm_truncated"] is True
    assert shrunk["must_confirm_total"] == 6


def test_shrink_leaves_a_small_payload_alone() -> None:
    small = _payload(rows=2, confirms=1)
    assert _size(small) <= tools.MAX_OUTPUT_CHARS

    shrunk = tools._shrink_preview(small)
    assert shrunk == small, "没超限时不该改动任何东西"
    assert "preview_truncated" not in shrunk


def test_shrink_does_not_mutate_the_original() -> None:
    payload = _payload()
    before = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    tools._shrink_preview(payload)

    assert json.dumps(payload, ensure_ascii=False, sort_keys=True) == before
