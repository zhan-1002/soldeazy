"""预览阶段的返回要能直接喂给会话渲染成表格，且必须提醒人的部分不能丢。

用户 2026-11-18 要求：

> 预览阶段 AI 读取工具回传的参数要喂给会话，会话中要显示类似于表格的组件反馈，
> 和我确认这点，即 AI 要提醒人的部分如套装、变体情况还有本次改动涉及账号、站点，
> 这些太多则截断。

所以这里守住四件事：

1. **结构固定**：``preview`` 下有三张表（概览 / 作用范围 / 逐条样本），
   键名稳定，会话可以直接按表渲染；
2. **必须提醒项**：``must_confirm`` 里必须出现套装/变体、作用范围（账号+站点）；
3. **多了要截断**：逐条样本有上限，且带 ``truncated`` + 总数，
   不能把几千行塞进会话；
4. **明细仍在文件里**：表格只是摘要，逐行核对靠 review 文件。
"""

from __future__ import annotations

from soldeazy_mcp_server.providers.soldeazy_change import (
    PREVIEW_SCOPE_ITEMS,
    PREVIEW_TABLE_ROWS,
    build_preview_summary,
)
from soldeazy_mcp_server.providers.soldeazy_plan import ChangePlan, PlanItem


def _plan(count: int = 3, *, qty: int = 0) -> ChangePlan:
    items = [
        PlanItem(f"45{i:05d}", f"SKU{i:03d}", "price", 21.9, 25.0)
        for i in range(count)
    ]
    items += [
        PlanItem(f"45{i:05d}", f"SKU{i:03d}", "qty", 100.0, 50.0)
        for i in range(qty)
    ]
    return ChangePlan(
        plan_id="price-x", kind="price", site="US",
        shop={"id": "159", "name": "FUN"}, created_at="x", items=items,
    )


def _summary(plan, **kwargs):
    base = {
        "shop": {"id": "159", "name": "FUN"},
        "site": "US",
        "only_listed": True,
        "needs_confirmation": {},
        "missing": [],
    }
    base.update(kwargs)
    return build_preview_summary(plan, **base)


def test_preview_has_three_renderable_tables() -> None:
    """会话要能直接按这三块渲染表格，所以键名必须稳定。"""

    data = _summary(_plan(3))

    assert set(data["preview"]) == {"概览", "作用范围", "逐条样本"}
    assert isinstance(data["preview"]["概览"], list)
    assert isinstance(data["preview"]["逐条样本"], list)


def test_overview_separates_price_and_quantity() -> None:
    data = _summary(_plan(2, qty=3))
    overview = data["preview"]["概览"]

    price_row = next(row for row in overview if "价格" in row["字段"])
    qty_row = next(row for row in overview if "数量" in row["字段"])

    assert price_row["条数"] == 2
    assert qty_row["条数"] == 3
    assert price_row["方向"] == "涨 2"
    assert qty_row["方向"] == "降 3"


def test_scope_names_the_account_and_site() -> None:
    """本次改动涉及哪个账号、哪个站点 —— 这是人工必须先看的。"""

    data = _summary(_plan(1))
    scope = data["preview"]["作用范围"]

    assert scope["账号"] == ["FUN"]
    assert scope["站点"] == ["US"]
    assert scope["状态筛选"] == ["已刊登(所有)"]


def test_scope_marks_unspecified_as_all() -> None:
    """不指定就是全站/全账号，必须写明，不能留空让人猜。"""

    data = _summary(_plan(1), shop={}, site="")
    scope = data["preview"]["作用范围"]

    assert "全部账号" in scope["账号"][0]
    assert "全部站点" in scope["站点"][0]


def test_scope_marks_unlisted_included() -> None:
    data = _summary(_plan(1), only_listed=False)
    assert "未刊登" in data["preview"]["作用范围"]["状态筛选"][0]


def test_sample_rows_are_readable() -> None:
    """逐条样本要带 数据表/SKU/字段/原值/新值 —— 这才能显示成表。"""

    data = _summary(_plan(2))
    sample = data["preview"]["逐条样本"]

    assert len(sample) == 2
    assert set(sample[0]) == {"数据表", "SKU", "字段", "原值", "新值"}
    assert sample[0]["字段"] == "价格"


def test_sample_is_truncated_with_counts() -> None:
    """几千行不能全塞进会话：截断并明确告知还有多少。"""

    plan = _plan(PREVIEW_TABLE_ROWS + 15)
    data = _summary(plan)

    assert len(data["preview"]["逐条样本"]) == PREVIEW_TABLE_ROWS
    assert data["preview_truncated"]["逐条样本"] is True
    assert data["preview_truncated"]["逐条样本总数"] == PREVIEW_TABLE_ROWS + 15


def test_sample_not_truncated_when_small() -> None:
    data = _summary(_plan(3))
    assert data["preview_truncated"]["逐条样本"] is False


def test_packs_must_be_confirmed_by_a_human() -> None:
    """套装/变体是必须显式提醒的一类——不能自动改，也不能默默略过。"""

    data = _summary(
        _plan(1),
        needs_confirmation={"ZMFL0012": ["ZMFL0012 x2", "ZMFL0012 x4"]},
    )
    text = "\n".join(data["must_confirm"])

    assert "套装/变体" in text
    assert "ZMFL0012 x2" in text
    assert "不会被点名" in text or "没有被点名" in text


def test_missing_targets_are_called_out() -> None:
    data = _summary(_plan(1), missing=["NOT-EXIST", "ALSO-MISSING"])
    text = "\n".join(data["must_confirm"])

    assert "线上找不到" in text
    assert "2 个" in text


def test_scope_reminder_always_present() -> None:
    """作用范围提醒是常驻的——每次预览都要让人看到账号/站点。"""

    data = _summary(_plan(1))
    text = "\n".join(data["must_confirm"])

    assert "作用范围" in text
    assert "FUN" in text
    assert "US" in text


def test_detail_file_reminder_always_present() -> None:
    """表格只是摘要，逐行核对必须在文件里——这句提醒不能少。"""

    data = _summary(_plan(1))
    text = "\n".join(data["must_confirm"])

    assert "review_after" in text or "磁盘文件" in text


def test_confirmations_are_capped_for_large_plans() -> None:
    """套装清单太长时要折叠，不能把提醒本身变成噪音。"""

    many = {f"SKU{i:03d}": [f"SKU{i:03d} x2"] for i in range(50)}
    data = _summary(_plan(1), needs_confirmation=many)
    packs_line = next(line for line in data["must_confirm"] if "套装/变体" in line)

    # 只列前 3 组 + 提示还有更多
    assert "还有更多" in packs_line
    assert packs_line.count("SKU") <= 6


def test_scope_items_are_capped() -> None:
    """账号/站点列表也受上限保护（虽然当前只有单个，规则要一致）。"""

    assert PREVIEW_SCOPE_ITEMS >= 1
    data = _summary(_plan(1))
    assert data["preview_truncated"]["作用范围"] is False


# ── 参数范围 vs 导出事实 ──────────────────────────────────────────────
#
# 这一组守的是"改错店铺"的早期发现：用户以为在改 FUN，实际导出里全是别的账号。
# 历史上真实发生过（不选店铺 = 全部店铺，第一页恰好全是 EBAY TAY）。


def test_export_shops_appear_in_scope_table() -> None:
    """导出里实际的账号要显示出来——这是"我读到了什么"的事实侧。"""

    data = _summary(_plan(1), shops_in_export=["FUN"])
    assert data["preview"]["作用范围"]["导出里的账号"] == ["FUN"]


def test_matching_export_shop_produces_no_warning() -> None:
    """参数与事实一致时不报警，避免把正常情况变成噪音。"""

    data = _summary(_plan(1), shops_in_export=["FUN"])
    assert data["scope_mismatch"] is False
    assert not any("范围不符" in line for line in data["must_confirm"])


def test_long_and_short_shop_names_count_as_the_same() -> None:
    """导出列写短名 FUN，参数可能是长名 EBAY FUN——必须认作同一家。"""

    data = _summary(_plan(1), shops_in_export=["FUN"])
    assert data["scope_mismatch"] is False

    data2 = _summary(
        _plan(1),
        shop={"id": "159", "name": "EBAY FUN"},
        shops_in_export=["FUN"],
    )
    assert data2["scope_mismatch"] is False


def test_mismatched_export_shop_raises_a_loud_warning() -> None:
    """参数说改 FUN、导出里全是别的账号：必须显式警告，这是改错店铺的信号。"""

    data = _summary(
        _plan(1),
        shop={"id": "159", "name": "FUN"},
        shops_in_export=["EBAY TAY"],
    )

    assert data["scope_mismatch"] is True
    text = "\n".join(data["must_confirm"])
    assert "范围不符" in text
    assert "EBAY TAY" in text
    assert "改动会落到导出里的这些账号" in text


def test_no_param_shop_means_no_mismatch_check() -> None:
    """没指定店铺（= 全部账号）时不存在"错配"，不该报警。"""

    data = _summary(
        _plan(1), shop={}, site="US", shops_in_export=["EBAY TAY", "EBAY LT"]
    )

    assert data["scope_mismatch"] is False
    assert data["preview"]["作用范围"]["导出里的账号"] == ["EBAY TAY", "EBAY LT"]
    assert "全部账号" in data["preview"]["作用范围"]["账号"][0]


def test_empty_export_shops_means_no_mismatch() -> None:
    """拿不到导出账号（比如导出失败）时不下结论。"""

    data = _summary(_plan(1), shops_in_export=[])
    assert data["scope_mismatch"] is False
    assert "导出里的账号" not in data["preview"]["作用范围"]


def test_export_shop_list_is_capped() -> None:
    """账号特别多时要截断，不能把作用范围表撑爆。"""

    many = [f"EBAY S{i:02d}" for i in range(30)]
    data = _summary(_plan(1), shop={}, shops_in_export=many)

    assert len(data["preview"]["作用范围"]["导出里的账号"]) == PREVIEW_SCOPE_ITEMS
    assert data["preview_truncated"]["作用范围"] is True


def test_empty_plan_produces_valid_structure() -> None:
    """0 条改动时结构仍完整，会话不会因为缺键而渲染失败。"""

    data = _summary(_plan(0))

    assert data["preview"]["逐条样本"] == []
    assert data["preview"]["概览"][0]["条数"] == 0
    assert data["must_confirm"]          # 至少还有作用范围与文件提醒
