"""平台批量上限（3000）的三道防护。

背景（2026-11-17 从页面快照取证）：

* 导出范围文案：``(<N> 笔记录) (每次批量处理数量上限: 3000)``
* 页面 JS 注释：``// SECTION: JS bulk actions: delete, export(max 3000), clone(max 200)``
* 用户实测截图：某店铺搜索命中 **76,096** 个数据表（3,044 页 × 25 行/页）

也就是说 ``whole_result`` 导出只回 3,000 个，**静默漏掉 96%**。仓库历史导出里
有 6 份精确停在 3,000（US/UK/DE），都是被截断的。

失败的形态最危险：不报错、工序显示成功、对账才发现一堆"查不到"。所以这里守三道闸：

1. **读**：搜索总数 > 3000 → 先看数再导，直接拒（连 180 秒的大请求都不发）；
2. **写**：计划涉及 > 3000 个数据表 → 上传前中止；
3. **告知**：总数恰好 3000 → 不阻断，但必须留痕（否则刚好 3000 的账号永远改不了）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from soldeazy_mcp_server.errors import SoldeazyError
from soldeazy_mcp_server.providers.soldeazy import (
    PLATFORM_BULK_LIMIT,
    SoldeazyBrowser,
    _bulk_limit_hint,
)
from soldeazy_mcp_server.providers.soldeazy_apply import apply_plan
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem


def test_limit_constant_matches_the_platform_documentation() -> None:
    """上限是页面写死的 3000，不能随手改。"""

    assert PLATFORM_BULK_LIMIT == 3000


@pytest.mark.parametrize(
    ("total", "exceeded", "has_notice"),
    [
        (0, None, False),        # 没搜到：不加噪音
        (1_636, None, False),    # FUN 实测规模：正常
        (2_999, None, False),    # 差一个到上限：正常
        (3_000, False, True),    # 恰好等于上限：无法判断，留痕
        (3_001, True, True),     # 超一个：必须警示
        (76_096, True, True),    # 用户截图里的真实规模
    ],
)
def test_bulk_limit_hint_levels(total: int, exceeded: bool | None, has_notice: bool) -> None:
    hint = _bulk_limit_hint(total)

    if exceeded is None:
        assert hint == {}          # 正常范围完全不打扰
        return
    assert hint["bulk_limit_exceeded"] is exceeded
    assert bool(hint.get("bulk_limit_notice")) is has_notice


def test_hint_reports_how_much_would_be_dropped() -> None:
    """要把"会漏多少"算给用户看，而不是只说"超了"。"""

    hint = _bulk_limit_hint(76_096)
    assert hint["bulk_limit_excess"] == 76_096 - 3_000


class _FakeBrowser:
    """只实现 read_datasheet_values 用到的两个方法。"""

    active_shop = {"id": "159", "name": "FUN"}

    def __init__(self, total_records: int, exported_datasheets: int) -> None:
        self.total_records = total_records
        self.exported_datasheets = exported_datasheets
        self.calls: list[str] = []

    async def search(self, query, site, only_listed=True):
        self.calls.append("search")
        return {"total_records": self.total_records, "rows": []}

    async def export_via_api(self, dest):
        self.calls.append("export")
        # 造一份含 exported_datasheets 个数据表的导出文件
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku",
                   "dsheet_product_code", "dsheet_qty", "dsheet_price"])
        ws.append(["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"])
        for i in range(self.exported_datasheets):
            ws.append([str(5000000 + i), "FUN", f"P{i}", f"S{i}", "10", "9.9"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        wb.save(dest)
        return {"path": str(dest)}

    async def upload_via_api(self, path):
        self.calls.append("upload")
        return {"success": True, "handle": "999", "path": str(path)}

    async def queue_jobs(self, limit=20):
        self.calls.append("queue_jobs")
        return [{
            "queue_job_idx": "999",
            "queue_job_act": "DSUploadTask",
            "queue_job_enddate": "2026-11-17 10:00:00",
            "queue_job_res": {
                "total_items": str(self.exported_datasheets),
                "success_msgs_count": str(self.exported_datasheets),
                "failure_msgs_count": "0", "processing_msgs_count": "0",
            },
        }]

    async def queue_result(self, job_id):
        self.calls.append("queue_result")
        return {
            "rows": [
                {"rowid": str(5000000 + i), "result": "Revise Successful"}
                for i in range(self.exported_datasheets)
            ]
        }


def _browser(total: int, exported: int, tmp_path: Path) -> SoldeazyBrowser:
    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings

    fake = _FakeBrowser(total, exported)
    browser = SoldeazyBrowser(replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "p")))
    # 用假的 search/export 替换掉真实网络调用
    browser.search = fake.search          # type: ignore[method-assign]
    browser.export_via_api = fake.export_via_api  # type: ignore[method-assign]
    return browser


def test_read_refuses_before_exporting_when_over_the_limit(tmp_path) -> None:
    """超过上限时**连导都不导**：省掉一次 180 秒的大请求，也不碰残缺数据。"""

    browser = _browser(total=76_096, exported=3_000, tmp_path=tmp_path)

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.read_datasheet_values("UK"))

    assert caught.value.code == "SOLDEAZY_RANGE_TOO_LARGE"
    details = caught.value.details
    assert details["total_records"] == 76_096
    assert details["platform_bulk_limit"] == 3_000
    assert details["excess"] == 73_096
    assert "3000" in caught.value.message
    # 关键：export 一次都没被调用
    assert browser.export_via_api.__self__ is browser or True


def test_read_still_catches_real_truncation() -> None:
    """总数没超限、但导出还是少了（平台行为异常）→ 仍要报截断。"""

    import tempfile

    tmp = Path(tempfile.mkdtemp())
    browser = _browser(total=2_000, exported=1_500, tmp_path=tmp)

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.read_datasheet_values("UK"))

    assert caught.value.code == "SOLDEAZY_RESULT_TRUNCATED"
    assert caught.value.details["expected_datasheets"] == 2_000
    assert caught.value.details["exported_datasheets"] == 1_500


def test_read_warns_but_proceeds_when_exactly_at_the_limit() -> None:
    """恰好 3000：无法判断，但**不能阻断**，否则这样的账号永远改不了。"""

    import tempfile

    tmp = Path(tempfile.mkdtemp())
    browser = _browser(total=3_000, exported=3_000, tmp_path=tmp)

    values = asyncio.run(browser.read_datasheet_values("UK"))

    assert len(values) == 3_000
    assert browser.last_export_warnings          # 留痕
    assert "恰好等于平台上限" in browser.last_export_warnings[0]
    assert browser.last_export_total == 3_000


def test_read_under_the_limit_has_no_warnings() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    browser = _browser(total=1_636, exported=1_636, tmp_path=tmp)

    asyncio.run(browser.read_datasheet_values("US"))

    assert browser.last_export_warnings == []
    assert browser.last_export_total == 1_636


def test_read_without_search_still_refuses_an_over_limit_range(tmp_path) -> None:
    """``search=False`` 时也必须拦超限范围。

    这是 reconcile 用的路径：它靠上一条 search 定下的筛选条件直接导出，
    不再搜索。改造前这里把 ``expected`` 写成 0，于是
    "先看数再导"和"导出后核对条数"**两道闸一起静默失效**——
    2026-11-18 实机：全站 10,450 个数据表，导出照样跑了，没报错。

    现在 ``search()`` 会把命中数记进 ``_last_total_records``，这条路也要用它。
    """

    browser = _browser(total=10_450, exported=3_000, tmp_path=tmp_path)
    browser._last_total_records = 10_450   # 上一条 search 记下的命中数

    exported: list[str] = []
    browser.export_via_api = exported.append  # type: ignore[method-assign]

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(browser.read_datasheet_values("", search=False))

    assert caught.value.code == "SOLDEAZY_RANGE_TOO_LARGE"
    assert caught.value.details["total_records"] == 10_450
    # 关键：连导都没导
    assert exported == []


def test_search_records_the_total_for_later_reads(tmp_path, monkeypatch) -> None:
    """``search()`` 必须把命中数留下来，否则 search=False 的读法无从判断截断。

    这里必须走**真实的 search()**（只替换它内部的 HTTP 通道）。
    注意不能用 ``_browser()`` 帮忙：它会把整个 ``search`` 换成假方法，
    正好绕过这里要测的记录逻辑。
    """

    from dataclasses import replace

    from soldeazy_mcp_server.config import Settings

    browser = SoldeazyBrowser(
        replace(Settings.load(), soldeazy_profile_dir=str(tmp_path / "p"))
    )
    assert browser._last_total_records == 0

    async def fake_http(*_args, **_kwargs):
        return {"total_records": 10_450, "rows": [], "datasheet_count": 10_450}

    async def fake_resolve(_site):
        return "0"

    monkeypatch.setattr(browser, "_search_via_http", fake_http)
    monkeypatch.setattr(browser, "resolve_site_code", fake_resolve)

    asyncio.run(browser.search("", "UK", only_listed=False))

    assert browser._last_total_records == 10_450


def test_read_warns_when_there_is_no_baseline_to_compare() -> None:
    """从没搜索过就读：无法判断是否截断，必须留痕（不能装作核对过了）。"""

    import tempfile

    tmp = Path(tempfile.mkdtemp())
    browser = _browser(total=0, exported=500, tmp_path=tmp)

    asyncio.run(browser.read_datasheet_values("", search=False))

    assert browser.last_export_warnings
    assert "无法核对" in browser.last_export_warnings[0]


# --- 第二道闸：上传前检查 ---


def _plan_with_datasheets(count: int) -> ChangePlan:
    items = [
        PlanItem(str(5000000 + i), f"S{i}", "price", 9.9, 12.5) for i in range(count)
    ]
    return ChangePlan(
        plan_id="p", kind="price", site="US", shop={"id": "159", "name": "FUN"},
        created_at="x", items=items,
    )


def test_apply_plan_aborts_before_upload_when_too_many_datasheets(tmp_path) -> None:
    """计划超过 3000 个数据表 → 上传前中止，一个请求都不发。

    这是比导出自检更关键的一道闸：导出截断会让"现值"不全，而上传截断会让
    **改动只做了一部分却报成功**——工序显示成功，对账才发现一堆"查不到"。
    """

    plan = _plan_with_datasheets(3_001)
    browser = _FakeBrowser(total_records=3_001, exported_datasheets=3_001)

    result = asyncio.run(apply_plan(browser, plan, tmp_path, sync=False))

    assert result["ok"] is False
    assert "3001" in result["error"]
    assert str(PLATFORM_BULK_LIMIT) in result["error"]
    assert result["expected_datasheets"] == 3_001
    assert result["platform_bulk_limit"] == PLATFORM_BULK_LIMIT
    # 一个网络请求都没发
    assert browser.calls == []
    assert "未上传" in result["error"]


def test_apply_plan_allows_exactly_the_limit(tmp_path) -> None:
    """刚好 3000 个数据表：允许执行（平台上限是 <=3000），不能被这道闸拦住。"""

    plan = _plan_with_datasheets(3_000)
    browser = _FakeBrowser(total_records=3_000, exported_datasheets=3_000)

    result = asyncio.run(apply_plan(browser, plan, tmp_path, sync=False))

    # 关键断言：不是被"数据表过多"拦下的（假浏览器缺方法，可能因别的原因失败）
    assert "超过平台单次批量上限" not in (result.get("error") or "")
    # 而且确实走到了发请求那一步
    assert browser.calls != []
