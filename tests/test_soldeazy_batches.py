"""多批执行：切段与循环的正确性。

设计原则（用户 2026-11-18 明确）
================================

多批次**不是新子系统**，就是「同一套动作连续执行多次」。
所以这里要守住的不变量只有三条：

1. **切段完整**：同一个数据表的所有行落在同一批（母体 + 子体不拆开），
   且所有数据表恰好出现一次 —— 不重不漏；
2. **单批不变**：不需要分批时走原来的单批路径，行为一个字节都不变；
3. **逐批自洽**：每批都是完整的导出→改→上传→同步→对账，失败就停，
   已成功的批次不回滚（回滚本身又是一次写入）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from soldeazy_mcp_server.providers.soldeazy import PLATFORM_BULK_LIMIT
from soldeazy_mcp_server.providers.soldeazy_change import (
    _carry_export_warnings,
    _ranges,
    _run_batches,
    _split_plan,
)
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem


def _make_plan(kind, site, shop, items, *, only_listed=True):
    return ChangePlan(
        plan_id=f"{kind}-{len(items)}",
        kind=kind,
        site=site,
        shop=shop,
        created_at="x",
        items=items,
        only_listed=only_listed,
    )


def _plan(datasheets: int, *, per_datasheet: int = 1) -> ChangePlan:
    items: list[PlanItem] = []
    for index in range(datasheets):
        for child in range(per_datasheet):
            items.append(
                PlanItem(
                    f"{5000000 + index:07d}",
                    f"S{index}-{child}",
                    "price",
                    9.9,
                    12.5,
                )
            )
    return _make_plan("price", "US", {"id": "159", "name": "FUN"}, items)


def test_ranges_cover_everything_once() -> None:
    for total in (0, 1, 2999, 3000, 3001, 7000, 20421):
        ranges = _ranges(total, PLATFORM_BULK_LIMIT)
        covered: list[int] = []
        for start, end in ranges:
            covered.extend(range(start, end + 1))
        assert covered == list(range(1, total + 1)), total


def test_split_keeps_a_datasheet_whole() -> None:
    """多变体数据表的母体行和子体行**必须在同一批**。

    拆开会让上传文件缺结构：只传子体行平台认不出它属于哪个数据表，
    工序只处理一部分、其余报失败（还原时实机踩过一次）。
    """

    plan = _plan(PLATFORM_BULK_LIMIT + 10, per_datasheet=3)
    batches = _split_plan(plan, _make_plan)

    assert len(batches) == 2
    seen: dict[str, int] = {}
    for batch in batches:
        per = {}
        for item in batch.items:
            per[item.rowid] = per.get(item.rowid, 0) + 1
        for rowid, count in per.items():
            # 每个数据表在某一批里必须是完整的 3 行，不能跨批
            assert count == 3, f"{rowid} 只带上了 {count} 行"
            seen[rowid] = seen.get(rowid, 0) + 1

    # 每个数据表恰好出现一次
    assert set(seen) == {f"{5000000 + i:07d}" for i in range(PLATFORM_BULK_LIMIT + 10)}
    assert all(count == 1 for count in seen.values())


def test_split_respects_the_limit_per_batch() -> None:
    plan = _plan(PLATFORM_BULK_LIMIT * 2 + 5)
    batches = _split_plan(plan, _make_plan)

    assert len(batches) == 3
    for batch in batches:
        assert len({item.rowid for item in batch.items}) <= PLATFORM_BULK_LIMIT
    assert sum(len(batch.items) for batch in batches) == len(plan.items)


def test_no_split_returns_the_original_plan() -> None:
    """不需要分批时**必须**返回原计划本体（不是副本），单批路径完全不变。"""

    plan = _plan(3000)
    assert _split_plan(plan, _make_plan) == [plan]

    single = _plan(1)
    assert _split_plan(single, _make_plan) == [single]


def test_split_preserves_plan_attributes() -> None:
    plan = _plan(PLATFORM_BULK_LIMIT + 1)
    plan.site = "UK"
    plan.shop = {"id": "4432", "name": "LT"}
    plan.only_listed = False

    for batch in _split_plan(plan, _make_plan):
        assert batch.site == "UK"
        assert batch.shop == {"id": "4432", "name": "LT"}
        assert batch.only_listed is False


# --- 分批执行 ---


class _FakeApply:
    """记录每批被调用一次；按需让某一批失败。"""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    async def __call__(self, browser, plan, workdir, *, sync, sync_price, sync_quantity):
        self.calls.append(plan.plan_id)
        ok = len(self.calls) != self.fail_at
        return {
            "plan_id": plan.plan_id,
            "ok": ok,
            "items": len(plan.items),
            "expected_datasheets": len({item.rowid for item in plan.items}),
            "sync": "ok" if ok else "partial",
            "problems": [] if ok else ["工序任务有 1 条失败"],
        }


class _FakeReconcile:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    async def __call__(self, browser, plan, workdir):
        self.calls.append(plan.plan_id)
        ok = len(self.calls) != self.fail_at
        return {
            "ok": ok,
            "matched": len(plan.items) if ok else 0,
            "items": len(plan.items),
            "mismatched_total": 0 if ok else len(plan.items),
            "absent_total": 0,
        }


class _FakeBrowser:
    last_export_warnings: list[str] = []


def _run(plan, batches, *, apply=None, reconcile=None, verbose=False):
    return asyncio.run(
        _run_batches(
            _FakeBrowser(),
            plan,
            batches,
            plan_dir=Path("."),
            price=True,
            qty=False,
            apply_plan=apply or _FakeApply(),
            reconcile=reconcile or _FakeReconcile(),
            extra={},
            verbose=verbose,
        )
    )


def test_every_batch_runs_the_full_cycle() -> None:
    """每批都要完整跑一遍 apply + reconcile —— 不是只上传不发同步。"""

    plan = _plan(PLATFORM_BULK_LIMIT + 1)
    batches = _split_plan(plan, _make_plan)
    apply, reconcile = _FakeApply(), _FakeReconcile()

    result = _run(plan, batches, apply=apply, reconcile=reconcile)

    assert len(apply.calls) == 2
    assert len(reconcile.calls) == 2
    assert result["ok"] is True
    assert result["batches"] == 2
    assert result["batches_done"] == 2
    assert result["stage"] == "done"
    assert result["problems"] == []


def test_a_failed_batch_stops_the_rest() -> None:
    """某批失败就停：继续跑只会扩大失败面，判决也更难解释。"""

    plan = _plan(PLATFORM_BULK_LIMIT * 2 + 1)   # 3 批
    batches = _split_plan(plan, _make_plan)
    assert len(batches) == 3
    apply = _FakeApply(fail_at=2)

    result = _run(plan, batches, apply=apply)

    assert len(apply.calls) == 2          # 第 3 批没跑
    assert result["ok"] is False
    assert result["batches_done"] == 1
    assert result["batches_failed"] == 1
    assert result["stage"] == "batch"
    assert any("第 2/3 批未通过" in p for p in result["problems"])
    assert any("还有 1 批未执行" in p for p in result["problems"])


def test_a_failure_on_the_last_batch_still_reports_cleanly() -> None:
    """最后一批失败时不该出现"还有 0 批未执行"这种废话。"""

    plan = _plan(PLATFORM_BULK_LIMIT * 2)
    batches = _split_plan(plan, _make_plan)
    result = _run(plan, batches, apply=_FakeApply(fail_at=2))

    assert result["ok"] is False
    assert result["batches_failed"] == 1
    assert not any("未执行" in p for p in result["problems"])


def test_reconcile_failure_also_stops() -> None:
    plan = _plan(PLATFORM_BULK_LIMIT * 2)
    batches = _split_plan(plan, _make_plan)
    reconcile = _FakeReconcile(fail_at=1)
    apply = _FakeApply()

    result = _run(plan, batches, apply=apply, reconcile=reconcile)

    assert len(apply.calls) == 1
    assert result["ok"] is False
    assert any("未通过" in p for p in result["problems"])


def test_batch_report_reports_progress_and_keeps_detail() -> None:
    plan = _plan(PLATFORM_BULK_LIMIT + 1)
    batches = _split_plan(plan, _make_plan)
    result = _run(plan, batches)

    assert result["batches"] == 2
    assert len(result["batch_detail"]) == 2
    assert result["reconciled"].endswith(f"/{len(plan.items)}")
    # 逐批结果里要能看出每批改了几条
    assert all("items" in row for row in result["batch_detail"])


def test_export_warnings_are_carried_into_the_verdict() -> None:
    """导出阶段的"无法判断是否截断"留痕必须透传，不能被批次汇总吞掉。"""

    class _Browser:
        last_export_warnings = ["恰好等于平台上限，无法判断是否被截断"]

    applied = _carry_export_warnings(_Browser(), {"sync_notes": ["原有提醒"]})
    assert applied["sync_notes"] == [
        "原有提醒",
        "恰好等于平台上限，无法判断是否被截断",
    ]

    # 没有提醒时不凭空加字段
    class _Quiet:
        last_export_warnings: list[str] = []

    assert _carry_export_warnings(_Quiet(), {"ok": True}) == {"ok": True}
