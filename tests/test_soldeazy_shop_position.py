"""店铺名不许靠"位置假设"猜出来。

原来的取法：

    shop = cells[site_index - 1]

即"站点左邻那一格就是店铺"。这在页面列序不变时成立，但**页面加一列就会静默取错**，
而店铺名会喂给 ``shop_problems`` 的"有没有切账号"校验：取错就可能误判成
"店铺一致"而放行，把改动写到别的店铺去——这是最坏的一种错。

现在的前提条件：**表头里必须真的有「渠道帐号」这一列**，才允许用位置假设。
表头认不出来就不给店铺名（宁可空着，也不能给个错的）。

注意一个已实测的坑：表头 14 列，数据行 15~16 列（多出勾选框、重复标题格），
所以**表头下标不能直接拿来取数据行**，只能用来确认"这一列存在"。
"""

from __future__ import annotations

import pytest

from soldeazy_mcp_server.providers.soldeazy import parse_rows

# 真实表头（含「渠道 帐号」「站点」）
HEADER_WITH_SHOP = [
    "", "序号", "数据表\n编号", "功能选项", "状态", "渠道\n帐号", "站点", "标题",
    "", "SKU", "价格", "运费\n(当地)", "促销\n刊登", "eBay促销",
]

# 不含「渠道 帐号」的表头（模拟页面结构变化）
HEADER_WITHOUT_SHOP = [
    "", "序号", "数据表\n编号", "功能选项", "状态", "站点", "标题",
    "", "SKU", "价格", "运费\n(当地)", "促销\n刊登", "eBay促销",
]


def _row(prefix: list[str], shop: str = "EBAY TUD", site: str = "US") -> list[str]:
    """拼一条数据行：中间是 prefix，尾部 5 列固定为 SKU/价格/运费/…"""
    return [*prefix, shop, site, "Title", "", "PCPB0028US", "20.99", "0", "-", "-"]


# 不带勾选框前缀，但仍要有 rowid（前 6 格里第一个 6 位以上数字）
NO_SHOP_PREFIX = ["1", "4567313", "edit", ""]


def test_shop_read_from_left_of_site_when_header_has_the_column() -> None:
    """表头有「渠道帐号」→ 允许用"站点左邻"这个位置假设。"""

    row = _row(["", "1", "4567313", "edit", ""])
    _, records = parse_rows([HEADER_WITH_SHOP, row])

    assert records[0]["shop"] == "EBAY TUD"
    assert records[0]["site"] == "US"


def test_shop_left_empty_when_header_lacks_the_column() -> None:
    """表头没有「渠道帐号」→ **不给店铺名**，而不是猜一个。

    猜错的后果是把改动写到别的店铺，所以宁可空着。
    """

    row = _row(NO_SHOP_PREFIX, shop="EBAY TUD", site="US")
    _, records = parse_rows([HEADER_WITHOUT_SHOP, row])

    assert records[0]["site"] == "US"      # 站点仍按内容识别，正常
    assert records[0]["shop"] == ""        # 店铺留空，不猜


def test_site_still_detected_without_shop_column() -> None:
    """店铺列缺失不该影响站点识别。"""

    for site in ("US", "UK", "Germany"):
        row = _row(NO_SHOP_PREFIX, shop="ignored", site=site)
        _, records = parse_rows([HEADER_WITHOUT_SHOP, row])
        assert records[0]["site"] == site


def test_column_shift_row_still_resolves_shop_and_site() -> None:
    """数据行比表头少一列（勾选框不见了）时，两个字段都要正确。"""

    full = _row(["", "1", "4567313", "edit", ""])
    shifted = full[1:]                     # 去掉开头的勾选框
    _, records = parse_rows([HEADER_WITH_SHOP, shifted])

    assert records[0]["rowid"] == "4567313"
    assert records[0]["shop"] == "EBAY TUD"
    assert records[0]["site"] == "US"


def test_row_with_extra_column_still_resolves_shop_and_site() -> None:
    """数据行比表头多一列（重复标题格）时也要正确。"""

    row = _row(["", "1", "4567313", "edit", "", "dup-title"])
    _, records = parse_rows([HEADER_WITH_SHOP, row])

    assert records[0]["shop"] == "EBAY TUD"
    assert records[0]["site"] == "US"


@pytest.mark.parametrize("shop_name", ["EBAY FUN", "FUN", "EBAY LT"])
def test_shop_name_preserved_verbatim(shop_name: str) -> None:
    """店铺名原样保留，不做大小写或裁剪以外的加工。"""

    row = _row(["", "1", "4567313", "edit", ""], shop=shop_name)
    _, records = parse_rows([HEADER_WITH_SHOP, row])
    assert records[0]["shop"] == shop_name
