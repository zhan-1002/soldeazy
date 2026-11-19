"""一次改价 + 改库存：一份计划、一次上传、一次同步。

业务事实（2026-11-17 用户纠正）：

* **上传**改的是线下数据表，E（数量）和 F（价格）本来就同时写；
* **线上生效只发生在同步那一步**，靠勾选「价格及邮费」/「刊登数量」；
* 所以"改价和改库存同时生效"= 一次上传 + 同步勾两个键，
  **不是**跑两遍 change_price 和 change_stock。

这里守住三件事：
1. 合并后的计划在一次上传里同时写 E 列和 F 列；
2. 同步键从计划内容推导（改什么就推什么，不漏推也不多推）；
3. 混合计划不会退化成"两条链路"。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from soldeazy_mcp_server.providers import soldeazy_apply as apply_mod
from soldeazy_mcp_server.providers.soldeazy_apply import (
    build_items,
    make_plan,
    plan_rows,
    plan_to_cell_changes,
)
from soldeazy_mcp_server.providers.soldeazy_plan import (
    ChangePlan,
    freeze,
    load_verified,
)
from soldeazy_mcp_server.providers.soldeazy_workbook import (
    EDITABLE_COLUMNS,
    apply_changes,
    diff_matrices,
)

# 一个多变体数据表：母体 ZM-P 下两个子体，价格/数量都不同
LIVE = {
    ("4569384", "ZMDC0063"): {
        "rowid": "4569384",
        "parent": "ZM-P",
        "sku": "ZMDC0063",
        "qty": "100",
        "price": "21.90",
    },
    ("4569384", "ZMDC0064"): {
        "rowid": "4569384",
        "parent": "ZM-P",
        "sku": "ZMDC0064",
        "qty": "100",
        "price": "22.90",
    },
}

# 导出矩阵：两行表头 + 母体行 + 一个子体行
MATRIX = [
    ["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku",
     "dsheet_product_code", "dsheet_qty", "dsheet_price"],
    ["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"],
    ["4569384", "FUN", "ZM-P", "ZMDC0063", "100", "21.9"],
    [None, None, "ZM-P", "ZMDC0064", "100", "22.9"],
]


def _merged_plan() -> ChangePlan:
    price_items, _ = build_items(LIVE, "price", {"ZMDC0063": 25.0})
    qty_items, _ = build_items(LIVE, "qty", {"ZM-P": 50})
    return make_plan(
        "price_qty", "US", {"id": "159", "name": "FUN"}, [*price_items, *qty_items]
    )


def test_one_plan_carries_both_fields() -> None:
    """混合计划里两种 field 并存，条目数 = 价格条 + 数量条。"""

    plan = _merged_plan()
    fields = [item.field for item in plan.items]

    assert fields.count("price") == 1
    assert fields.count("qty") == 2          # 母体下两个子体各一条
    assert plan.kind == "price_qty"
    assert len({item.rowid for item in plan.items}) == 1   # 仍是同一个数据表


def test_one_upload_writes_both_columns() -> None:
    """一次上传同时改 E(数量) 与 F(价格)，不是两次上传。"""

    plan = _merged_plan()
    changes, missing = plan_to_cell_changes(plan, MATRIX)

    assert missing == []
    written = apply_changes(MATRIX, changes)

    # 第 3 行（ZMDC0063）：数量 100→50、价格 21.9→25.0
    assert written[2][4] == 50.0
    assert written[2][5] == 25.0
    # 第 4 行（ZMDC0064）：只改数量，价格保持原值
    assert written[3][4] == 50.0
    assert written[3][5] == "22.9"


def test_write_scope_stays_inside_e_and_f() -> None:
    """上传是整行覆盖：除 E/F 之外一格都不许变。"""

    plan = _merged_plan()
    changes, _ = plan_to_cell_changes(plan, MATRIX)
    written = apply_changes(MATRIX, changes)
    touched = {change.letter for change in diff_matrices(MATRIX, written)}

    assert touched <= EDITABLE_COLUMNS
    assert touched == {"E", "F"}          # 真的两列都动了


def test_upload_keeps_parent_and_all_child_rows() -> None:
    """只保留计划涉及的数据表，且母体行 + 全部子体行都要在。"""

    plan = _merged_plan()
    keep = plan_rows(plan, MATRIX)

    assert keep == [3, 4]                 # 母体行 + 子体行，一行都不能少


def _stub_deps(plan: ChangePlan, captured: dict) -> dict:
    """把 run_change 的依赖换成假的，只观察它怎么调 apply_plan。"""

    async def resolve_shop(_shop):
        return {"id": "159", "name": "FUN"}

    async def read_values(_site, only_listed=True):
        return LIVE

    def build(live, kind, targets):
        return build_items(live, kind, targets)

    def make(kind, site, shop, items, **_kwargs):
        return plan

    def write_draft(_dir, _plan):
        return "draft"

    def freeze_it(_dir, _plan):
        return {"sha256": "digest"}

    def load_verified_it(_dir, _plan_id):
        return plan, "digest"

    async def apply_plan(_browser, _plan, _dir, *, sync, sync_price, sync_quantity):
        captured.update(
            sync=sync, sync_price=sync_price, sync_quantity=sync_quantity
        )
        return {"ok": True, "sync": "ok"}

    async def reconcile(_browser, _plan, _dir):
        return {"ok": True, "matched": len(plan.items), "items": len(plan.items)}

    class FakeBrowser:
        pass

    return {
        "build_items": build,
        "make_plan": make,
        "write_draft": write_draft,
        "freeze": freeze_it,
        "load_verified": load_verified_it,
        "apply_plan": apply_plan,
        "reconcile": reconcile,
        "plan_dir": "/tmp/plans",
        "browser": FakeBrowser(),
        "resolve_shop": resolve_shop,
        "read_datasheet_values": read_values,
    }


def test_run_change_merges_qty_into_the_same_plan(monkeypatch) -> None:
    """给 quantities 时，价格与数量条目进同一份计划、同一次 apply_plan。"""

    from soldeazy_mcp_server.providers import soldeazy_change as change_mod

    plan = _merged_plan()
    captured: dict = {}
    deps = _stub_deps(plan, captured)

    async def fake_read(_site, query="", only_listed=True, **_kw):
        return LIVE

    # browser 是 FakeBrowser，read_datasheet_values 由 run_change 直接调
    deps["browser"].read_datasheet_values = fake_read

    result = asyncio.run(
        change_mod.run_change(
            site="US",
            prices={"ZMDC0063": 25.0},
            quantities={"ZM-P": 50},
            shop="FUN",
            dry_run=False,
            verbose=False,
            deps=deps,
        )
    )

    assert result["ok"] is True
    assert result["kind"] == "price_qty"
    assert result["items"] == 3                    # 1 价格 + 2 数量
    # 两个同步键都开：这是"同时生效"的全部秘密
    assert captured["sync_price"] is True
    assert captured["sync_quantity"] is True


def test_run_change_without_qty_targets_stays_single_kind() -> None:
    """不传 quantities 时行为与改造前完全一致。"""

    from soldeazy_mcp_server.providers import soldeazy_change as change_mod

    price_items, _ = build_items(LIVE, "price", {"ZMDC0063": 25.0})
    plan = make_plan("price", "US", {"id": "159", "name": "FUN"}, price_items)
    captured: dict = {}
    deps = _stub_deps(plan, captured)

    async def fake_read(_site, query="", only_listed=True, **_kw):
        return LIVE

    deps["browser"].read_datasheet_values = fake_read

    result = asyncio.run(
        change_mod.run_change(
            site="US",
            prices={"ZMDC0063": 25.0},
            shop="FUN",
            dry_run=False,
            verbose=False,
            deps=deps,
        )
    )

    assert result["kind"] == "price"
    assert result["items"] == 1
    assert captured["sync_quantity"] is False


def test_apply_plan_derives_sync_keys_from_the_plan(monkeypatch) -> None:
    """同步键由计划内容推导：改什么推什么，不能张冠李戴。"""

    from soldeazy_mcp_server import tools
    import soldeazy_mcp_server.providers.soldeazy_plan as plan_mod

    # 改量计划：必须推「刊登数量」，不能推价格
    qty_items, _ = build_items(LIVE, "qty", {"ZM-P": 50})
    qty_plan = make_plan("qty", "US", {"id": "159", "name": "FUN"}, qty_items)

    captured: dict = {}

    async def fake_apply(_browser, _plan, _dir, *, sync, sync_price, sync_quantity):
        captured.update(sync_price=sync_price, sync_quantity=sync_quantity)
        return {"ok": True, "sync": "ok"}

    monkeypatch.setattr(apply_mod, "apply_plan", fake_apply)
    monkeypatch.setattr(
        plan_mod, "load_verified", lambda _dir, _id: (qty_plan, "digest")
    )
    monkeypatch.setattr(tools, "_require_write_enabled", lambda: None)
    monkeypatch.setattr(tools, "_plan_dir", lambda: "/tmp/plans")

    asyncio.run(tools.soldeazy_apply_plan("qty-x", dry_run=False))

    assert captured == {"sync_price": False, "sync_quantity": True}


def test_apply_plan_preview_reports_sync_keys_without_writing(monkeypatch) -> None:
    """dry_run 预览也要说清楚"这次会推哪两个开关"。"""

    import soldeazy_mcp_server.providers.soldeazy_plan as plan_mod
    from soldeazy_mcp_server import tools

    plan = _merged_plan()
    monkeypatch.setattr(plan_mod, "load_verified", lambda _dir, _id: (plan, "d"))
    monkeypatch.setattr(tools, "_plan_dir", lambda: "/tmp/plans")

    result = asyncio.run(tools.soldeazy_apply_plan("price_qty-x", dry_run=True))

    data = result["data"]
    assert data["sync_price"] is True
    assert data["sync_quantity"] is True
    assert data["sync_keys"] == ["价格及邮费", "刊登数量"]
    assert data["price_items"] == 1
    assert data["qty_items"] == 2


def test_plan_changes_accepts_qty_targets_alongside_price(monkeypatch) -> None:
    """分步路径也支持一份计划里同时含价格与数量。"""

    import soldeazy_mcp_server.providers.soldeazy_plan as plan_mod
    from soldeazy_mcp_server import tools

    captured: dict = {}

    async def fake_resolve(_shop):
        return {"id": "159", "name": "FUN"}

    async def fake_read(_site, query="", only_listed=True, **_kw):
        return LIVE

    def fake_write(_dir, plan):
        captured["plan"] = plan
        return "/tmp/plans/plan.json"

    monkeypatch.setattr(tools, "_resolve_shop_id", fake_resolve)
    monkeypatch.setattr(tools.soldeazy, "read_datasheet_values", fake_read)
    monkeypatch.setattr(tools, "_plan_dir", lambda: "/tmp/plans")
    monkeypatch.setattr(plan_mod, "write_draft", fake_write)

    result = asyncio.run(
        tools.soldeazy_plan_changes(
            prices={"ZMDC0063": 25.0},
            quantities={"ZM-P": 50},
        )
    )

    data = result["data"]
    assert data["kind"] == "price_qty"
    assert data["price_items"] == 1
    assert data["qty_items"] == 2
    assert data["planned_items"] == 3
    assert captured["plan"].kind == "price_qty"


def test_frozen_mixed_plan_survives_hash_verification(tmp_path) -> None:
    """混合计划的哈希校验必须照样通过（不能因为 kind 变了就失效）。"""

    plan = _merged_plan()
    directory = tmp_path / "plans"
    directory.mkdir()

    from soldeazy_mcp_server.providers.soldeazy_plan import write_draft

    write_draft(directory, plan)
    freeze(directory, plan)
    loaded, digest = load_verified(directory, plan.plan_id)

    assert loaded.kind == "price_qty"
    assert len(loaded.items) == 3
    assert digest == plan.digest()
    # 盘上的 JSON 也如实记录混合 kind，历史可读
    raw = json.loads((directory / f"{plan.plan_id}.json").read_text(encoding="utf-8"))
    assert raw["kind"] == "price_qty"


def test_freeze_hash_mismatch_aborts_before_any_upload() -> None:
    """冻结时算的哈希与回读不一致 → 拒绝执行，绝不上传。

    这是 ``frozen`` 那个返回值原来被丢弃时漏掉的一道闸：写盘被截断、
    磁盘缓存、并发改文件都会让"要执行的"和"冻住的"不是同一份。
    """

    from soldeazy_mcp_server.errors import SoldeazyError
    from soldeazy_mcp_server.providers import soldeazy_change as change_mod

    plan = _merged_plan()
    captured: dict = {}
    deps = _stub_deps(plan, captured)
    # freeze 说 A，回读说 B
    deps["freeze"] = lambda _dir, _plan: {"sha256": "hash-at-freeze"}
    deps["load_verified"] = lambda _dir, _pid: (plan, "hash-after-reload")

    async def fake_read(_site, query="", only_listed=True, **_kw):
        return LIVE

    deps["browser"].read_datasheet_values = fake_read

    applied_called: list[bool] = []

    async def no_apply(*_args, **_kwargs):
        applied_called.append(True)
        return {"ok": True}

    deps["apply_plan"] = no_apply

    with pytest.raises(SoldeazyError) as caught:
        asyncio.run(
            change_mod.run_change(
                site="US",
                prices={"ZMDC0063": 25.0},
                quantities={"ZM-P": 50},
                shop="FUN",
                dry_run=False,
                verbose=False,
                deps=deps,
            )
        )

    assert caught.value.code == "SOLDEAZY_PLAN_UNSTABLE"
    # 关键：一个上传请求都没发出去
    assert applied_called == []
