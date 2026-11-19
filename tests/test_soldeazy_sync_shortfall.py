"""同步条数不一致：把「差几个」变成「差的是哪几个、为什么」。

用户反馈：改动了 N 个数据表、同步的却是 M 个，不知道为什么。

只报 ``同步了 96 个，上传的是 100 个`` 是不可行动的——用户拿不到任何
可以着手的信息。这里把计划的数据表集合与同步工序明细逐条比对，分成四类：

| 类别 | 含义 | 该不该担心 |
|---|---|---|
| ``synced`` | 成功同步 | 否 |
| ``skipped_no_listing`` | 没有在线刊登，平台不处理 | **否**（未刊登表同步不了是预期） |
| ``failed`` | 工序明确报失败 | **是**，附平台原因 |
| ``not_in_job`` | 计划里有、明细里根本没有 | **是**，说明请求没覆盖到（缓存截断嫌疑） |

最后一类尤其要单独分出来：它看起来像"失败"，其实是"根本没被处理"，
两者的排查方向完全不同。
"""

from __future__ import annotations

import asyncio

import pytest

from soldeazy_mcp_server.providers.soldeazy_apply import (
    _await_latest_job,
    summarize_sync_shortfall,
)


def test_all_synced() -> None:
    planned = ["1001", "1002", "1003"]
    rows = [
        {"rowid": "1001", "result": "Revise Successful"},
        {"rowid": "1002", "result": "Revise Successful"},
        {"rowid": "1003", "result": "Revise Successful"},
    ]

    result = summarize_sync_shortfall(planned, rows)

    assert result["synced"] == planned
    assert result["skipped_no_listing"] == []
    assert result["failed"] == []
    assert result["not_in_job"] == []
    assert result["planned"] == 3
    assert result["job_rows"] == 3


def test_no_listing_is_counted_separately_from_failures() -> None:
    """没有在线刊登 ≠ 失败。混在一起会让人以为要去查错误原因。"""

    planned = ["1001", "1002"]
    rows = [
        {"rowid": "1001", "result": "Revise Successful"},
        {"rowid": "1002", "result": "没有在线刊登，跳过"},
    ]

    result = summarize_sync_shortfall(planned, rows)

    assert result["synced"] == ["1001"]
    assert result["skipped_no_listing"] == ["1002"]
    assert result["failed"] == []
    assert result["not_in_job"] == []


def test_english_listing_message_also_recognised() -> None:
    planned = ["1001"]
    rows = [{"rowid": "1001", "result": "No active listing found"}]

    result = summarize_sync_shortfall(planned, rows)

    assert result["skipped_no_listing"] == ["1001"]
    assert result["failed"] == []


def test_real_failure_keeps_the_platform_reason() -> None:
    """失败要附平台给的原因，否则用户没法排查。"""

    planned = ["1001"]
    rows = [{"rowid": "1001", "result": "Item specific Brand is required"}]

    result = summarize_sync_shortfall(planned, rows)

    assert result["failed"] == [
        {"rowid": "1001", "result": "Item specific Brand is required"}
    ]
    assert result["synced"] == []


def test_missing_from_job_is_its_own_category() -> None:
    """计划里有、明细里没有 —— 不是失败，是没被处理。

    这是最容易被误判成"失败"的一类。分开之后，排查方向完全不同：
    失败要去查平台拒绝原因，没进任务要去查"请求有没有覆盖到"。
    """

    planned = ["1001", "1002", "1003"]
    rows = [{"rowid": "1001", "result": "Revise Successful"}]

    result = summarize_sync_shortfall(planned, rows)

    assert result["synced"] == ["1001"]
    assert result["not_in_job"] == ["1002", "1003"]
    assert result["failed"] == []


def test_empty_job_rows_reports_everything_as_not_in_job() -> None:
    """明细一条都没有：全部算"没进任务"，而不是"全部失败"。"""

    result = summarize_sync_shortfall(["1", "2"], [])

    assert result["not_in_job"] == ["1", "2"]
    assert result["failed"] == []
    assert result["synced"] == []


def test_counts_add_up() -> None:
    """四类之和必须能解释全部计划项，不能有"消失的"数据表。"""

    planned = ["1", "2", "3", "4", "5"]
    rows = [
        {"rowid": "1", "result": "Revise Successful"},
        {"rowid": "2", "result": "没有在线刊登"},
        {"rowid": "3", "result": "Some error"},
    ]

    result = summarize_sync_shortfall(planned, rows)

    total = (
        len(result["synced"])
        + len(result["skipped_no_listing"])
        + len(result["failed"])
        + len(result["not_in_job"])
    )
    assert total == len(planned)


def test_job_rows_outside_the_plan_are_ignored() -> None:
    """工序里出现计划外的 rowid（抓错任务时的症状）不能污染统计。"""

    result = summarize_sync_shortfall(
        ["1"],
        [
            {"rowid": "1", "result": "Revise Successful"},
            {"rowid": "9999", "result": "Revise Successful"},
        ],
    )

    # 统计只算计划内的
    assert result["synced"] == ["1"]
    assert result["not_in_job"] == []
    assert result["planned"] == 1
    # 但计划外的行要单独暴露出来 —— 这是"抓错了任务"的直接证据
    assert result["unexpected_in_job"] == ["9999"]
    assert result["job_rows"] == 2


def test_unexpected_rows_become_a_note_not_a_failure() -> None:
    """抓到别人的任务时，条数统计不可信，但**不判失败**——
    因为我们的改动可能其实成功了，只是拿错了任务来比对。"""

    class WrongJob(_Browser):
        async def queue_result(self, job_id: str):
            return {"rows": [
                {"rowid": "9999", "result": "Revise Successful"},
                {"rowid": "8888", "result": "Revise Successful"},
            ]}

    browser = WrongJob([], total_items="2")
    result = asyncio.run(
        _await_latest_job(
            browser, 2, planned_ids=["1001", "1002"], attempts=1, wait_seconds=0
        )
    )

    assert result is not None
    assert result["unexpected_in_job"] == ["8888", "9999"]
    assert any("可能读到了别人的任务" in n for n in result["notes"])
    # 关键：not_in_job 也会全中，但那是"拿错任务"的后果，不是我们的失败
    assert result["not_in_job"] == ["1001", "1002"]


def test_blank_ids_are_ignored() -> None:
    result = summarize_sync_shortfall(["1", "  ", ""], [{"rowid": "1", "result": "ok successful"}])
    assert result["planned"] == 1


# --- _await_latest_job 的集成行为 ---


class _Browser:
    """第一次给基线（空），之后冒出一个同步任务 + 明细。"""

    def __init__(self, job_rows: list[dict], total_items: str = "1") -> None:
        self._calls = 0
        self._job_rows = job_rows
        self._total = total_items

    async def queue_jobs(self, limit: int = 20):
        self._calls += 1
        if self._calls == 1:
            return []
        res = {
            "total_items": self._total,
            "success_msgs_count": str(len(self._job_rows)),
            "failure_msgs_count": "0",
        }
        return [{"queue_job_idx": "9", "queue_job_act": "DAlignTask", "queue_job_res": res}]

    async def queue_result(self, job_id: str):
        return {"rows": self._job_rows}


def test_not_in_job_becomes_a_problem_with_sample() -> None:
    """"没进任务"必须升级为 problem，并给出可核对的样本。"""

    browser = _Browser(
        [{"rowid": "1001", "result": "Revise Successful"}], total_items="1"
    )
    result = asyncio.run(
        _await_latest_job(
            browser,
            2,
            planned_ids=["1001", "1002"],
            attempts=1,
            wait_seconds=0,
        )
    )

    assert result is not None
    assert result["ok"] is False
    assert any("没有进入同步任务" in p for p in result["problems"])
    assert any("1002" in p for p in result["problems"])
    assert result["not_in_job"] == ["1002"]


def test_all_skipped_no_listing_becomes_problem_when_nothing_synced() -> None:
    """一个都没同步上时，"没有在线刊登"不能只当警告——那等于整体没生效。"""

    browser = _Browser(
        [{"rowid": "1001", "result": "没有在线刊登"}], total_items="1"
    )
    result = asyncio.run(
        _await_latest_job(
            browser,
            1,
            planned_ids=["1001"],
            attempts=1,
            wait_seconds=0,
        )
    )

    assert result is not None
    assert result["ok"] is False
    assert any("没有在线刊登" in p for p in result["problems"])


def test_partial_skip_is_only_a_note() -> None:
    """有成功的、有跳过的：跳过属预期，只记警告不判失败。"""

    browser = _Browser(
        [
            {"rowid": "1001", "result": "Revise Successful"},
            {"rowid": "1002", "result": "没有在线刊登"},
        ],
        total_items="2",
    )
    result = asyncio.run(
        _await_latest_job(
            browser,
            2,
            planned_ids=["1001", "1002"],
            attempts=1,
            wait_seconds=0,
        )
    )

    assert result is not None
    assert result["ok"] is True
    assert any("没有在线刊登" in n for n in result["notes"])


@pytest.mark.parametrize("attempts", [1, 2])
def test_detail_unavailable_falls_back_to_count_only(attempts: int) -> None:
    """拿不到明细时退回只比条数，不能因为读不到明细就判失败。"""

    class NoDetail(_Browser):
        async def queue_result(self, job_id: str):
            raise RuntimeError("boom")

    browser = NoDetail([], total_items="1")
    result = asyncio.run(
        _await_latest_job(
            browser, 1, planned_ids=["1001"], attempts=attempts, wait_seconds=0
        )
    )

    assert result is not None
    # 条数一致 → 不判失败
    assert result["ok"] is True
