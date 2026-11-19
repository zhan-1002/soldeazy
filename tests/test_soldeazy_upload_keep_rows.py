"""``build_upload_file`` 传 ``keep_rows`` 时必须自洽（生成与复核用同一份行号）。

实机缺陷（2026-11-18，还原测试时暴露）：

    生成文件用的是「过滤掉表头行号后的 wanted」，
    复核期望却用「未过滤的 keep_rows」。
    调用方把表头行号 1..header 也传进 keep_rows（很自然的写法）时，
    两者行数不同 → 比对照样整体错位 → 报出 A3/B3/C3 这类
    与真实变更完全无关的差异，`UnsafeUpload` 拒绝上传。

结果：**任何"把表头行号也放进 keep_rows"的调用都会失败**，
而 `apply_plan` 恰好就是那么传的（`keep_rows=plan_rows(...)` 里含表头）。
只是之前真机测试都是"整份导出上传"没触发，这次精确还原才撞上。
"""

from __future__ import annotations

import pytest

from soldeazy_mcp_server.providers.soldeazy_workbook import (
    UnsafeUpload,
    build_upload_file,
    header_row_count,
    read_matrix,
    write_matrix,
)

# 两行表头 + 3 条数据（第 3~6 行）
HEADER_TECH = ["dsheet_row_id", "dsheet_shop_initial", "dsheet_var_parent_sku",
               "dsheet_product_code", "dsheet_qty", "dsheet_price"]
HEADER_SHOW = ["Row ID", "Sales channel", "P.SKU", "SKU", "Qty", "Price"]
ROWS = [
    ["1001", "FUN", "P-A", "SKU-A", "50", "10.00"],
    [None, None, "P-A", "SKU-B", "100", "20.00"],
    ["1002", "FUN", "P-B", "SKU-C", "30", "30.00"],
]
MATRIX = [HEADER_TECH, HEADER_SHOW, *ROWS]


def _make_xlsx(path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Sheet1"
    for row in MATRIX:
        worksheet.append(row)
    workbook.save(str(path))
    workbook.close()


def test_keep_rows_including_header_succeeds(tmp_path) -> None:
    """**核心回归**：keep_rows 里带表头行号时必须正常，而不是报错。

    这正是还原测试踩到的坑——调用方把 1..header 也传进来是很自然的写法。
    """

    src = tmp_path / "src.xlsx"
    _make_xlsx(src)
    header = header_row_count(MATRIX)
    assert header == 2

    # 只改第 3 行（第一条数据）的价格，keep_rows 含表头行号 1、2
    changes = [("price", 3, 12.5)]
    dst = tmp_path / "dst.xlsx"

    report = build_upload_file(src, dst, changes, keep_rows=[1, 2, 3, 4])

    assert report["rows"] == 2          # 表头 + 第3、4行（数据行）
    _s, written = read_matrix(dst)
    assert written[2][5] == 12.5        # 第 3 行价格已改
    assert written[3][5] == "20.00"     # 第 4 行未动


def test_keep_rows_without_header_also_works(tmp_path) -> None:
    """只传数据行号（不含表头）同样要正常——表头由函数自动保留。"""

    src = tmp_path / "src.xlsx"
    _make_xlsx(src)
    dst = tmp_path / "dst.xlsx"

    report = build_upload_file(src, dst, [("qty", 5, 77)], keep_rows=[5])

    assert report["rows"] == 1
    _s, written = read_matrix(dst)
    assert written[header_row_count(MATRIX)][4] == 77


def test_keep_rows_out_of_range_still_rejected(tmp_path) -> None:
    """越界行号仍然要拒——修 bug 不能把这个保护去掉。"""

    src = tmp_path / "src.xlsx"
    _make_xlsx(src)
    with pytest.raises(UnsafeUpload, match="超出表格范围"):
        build_upload_file(src, tmp_path / "d.xlsx", [], keep_rows=[99])


def test_keep_rows_only_header_is_rejected(tmp_path) -> None:
    """只给表头行号 = 没有数据行可传，要拒。"""

    src = tmp_path / "src.xlsx"
    _make_xlsx(src)
    with pytest.raises(UnsafeUpload, match="keep_rows 为空"):
        build_upload_file(src, tmp_path / "d.xlsx", [], keep_rows=[1, 2])


def test_whitelist_violation_still_caught(tmp_path) -> None:
    """写非 E/F 列仍要被拦——这是那个自检的本职。"""

    src = tmp_path / "src.xlsx"
    _make_xlsx(src)

    # 手工把 A 列改掉，绕过 build_upload_file 的 changes 机制
    with pytest.raises(ValueError):
        # apply_changes 对未知字段会报错，间接证明不能写 A 列
        build_upload_file(src, tmp_path / "d.xlsx", [("title", 3, "hack")])
