"""让 parse_rows 正确解析「检视=所有」的响应，从而摆脱导出依赖。

真实结构（2026-11-18 抓取，FUN/US，`selsetprofile='ALL columns'`）：

    表头 20 格：... [7] 标题  [8] ''（空名＝变体数列）  [9] SKU  [10] 价格  [11] 数量
    数据 21 格：... [7] ''    [8] 标题                [9] '5'  [10] SKU   [11] 价格

也就是**数据行比表头多 1 格**，多出的位置在表头第 8 列（空名）。
后果：从第 8 列起数据整体右移一格，按表头下标直取会把 SKU 当价格。

旧实现 `_split_stacked_columns` 用负下标（``cells[-4]`` 取价格）在
21 格的行上取到的是「运费」列（值 '-'），**读到错值却不报错**。

这份夹具用真机数据（不是手写），断言修复后能读到正确值。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soldeazy_mcp_server.providers.soldeazy import (  # noqa: E402
    parse_rows,
    resolve_columns,
)

# 真机表头（检视=所有，20 格）
HEADER_ALL = [
    "", "序号", "数据表\n编号", "功能选项", "状态", "渠道\n帐号", "站点", "标题",
    "", "SKU", "价格", "数量", "实际\n库存", "运费\n(当地)",
    "数据摸板", "刊登模板", "日程", "促销\n刊登", "eBay促销", "数据表标籤",
]

# 真机数据行 1（单变体，rowid=3117213）
ROW_ONE = [
    "", "1", "3117213", "edit delete preview", "",
    "EBAY\nFUN", "US", "", "100W UFO LED High Bay Light",
    "", "FGS100W110V", "27.48", "50", "-",
    "0", "FUN_US_BIN_利郎达", "FUN-US-NEW", "-", "-", "-", "",
]

# 真机数据行 2（5 个变体，rowid=3117211）
ROW_FIVE = [
    "", "2", "3117211", "edit delete preview", "",
    "EBAY\nFUN", "US", "", "50W 100W 200W 300W 500W Factory Shop",
    "5",
    "FGS50W110V\nFGS100W110V\nFGS200W110V\nFGS300W110V\nFGS500W110V",
    "22.77\n27.48\n45.50\n59.16\n99.56",
    "100\n50\n100\n50\n100", "-\n-\n-\n-\n-",
    "0", "FUN_US_BIN_利郎达", "FUN-US-NEW", "-", "-", "-", "",
]


def test_header_mapping_is_correct_without_any_shift() -> None:
    """表头下标本身是对的：price->10, qty->11。

    问题不在表头，而在「数据比表头多一格」后的整体右移。
    """

    mapping = resolve_columns(HEADER_ALL)

    assert mapping["sku"] == 9
    assert mapping["price"] == 10
    assert mapping["qty"] == 11
    assert mapping["site"] == 6


def test_data_row_is_one_cell_longer_than_header() -> None:
    """把这个结构差异写成断言，避免以后又当成"没有数量列"。"""

    assert len(HEADER_ALL) == 20
    assert len(ROW_ONE) == 21
    assert len(ROW_FIVE) == 21
    # 多出来的位置在表头的空名列（变体数）
    assert ROW_ONE[9] == ""          # 表头 SKU 位置，数据里是空
    assert ROW_ONE[10] == "FGS100W110V"   # 真正的 SKU
    assert ROW_ONE[11] == "27.48"         # 真正的价格


def test_parse_rows_reads_the_real_price_from_a_single_variant_row() -> None:
    """单变体行：价格必须是 27.48，不是 '-'（负下标会取到运费列）。"""

    _header, records = parse_rows([HEADER_ALL, ROW_ONE])

    assert len(records) == 1
    record = records[0]
    assert record["rowid"] == "3117213"
    assert record["sku"] == "FGS100W110V", f"sku 读错: {record['sku']!r}"
    assert record["price"] == pytest.approx(27.48), f"price 读错: {record['price']!r}"
    assert record["site"] == "US"


def test_parse_rows_expands_five_variants_with_correct_values() -> None:
    """多变体行：5 个变体各成一条，SKU/价格/数量都要对得上。"""

    _header, records = parse_rows([HEADER_ALL, ROW_FIVE], expand_variants=True)

    assert len(records) == 5
    assert [r["sku"] for r in records] == [
        "FGS50W110V", "FGS100W110V", "FGS200W110V", "FGS300W110V", "FGS500W110V",
    ]
    assert [r["price"] for r in records] == [
        pytest.approx(22.77), pytest.approx(27.48), pytest.approx(45.50),
        pytest.approx(59.16), pytest.approx(99.56),
    ]
    assert [r["rowid"] for r in records] == ["3117211"] * 5
    assert [r["variants"] for r in records] == [5] * 5


def test_parse_rows_exposes_quantity_from_the_response() -> None:
    """数量必须能读到（50）—— 这是「对账摆脱导出」的关键前提。"""

    _header, records = parse_rows([HEADER_ALL, ROW_FIVE], expand_variants=True)

    assert [r.get("qty") for r in records] == [
        pytest.approx(100), pytest.approx(50), pytest.approx(100),
        pytest.approx(50), pytest.approx(100),
    ]
