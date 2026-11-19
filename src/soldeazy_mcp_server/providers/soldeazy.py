"""Soldeazy datasheet browser provider (Playwright, persistent profile).

The production 影刀 flow is *not* an API client: it drives the Soldeazy web UI
(login → search → export xlsx → offline pricing → upload → BULK SYNC). This
module reproduces that path in Playwright so the MCP server can do the same
without 影刀.

Selector policy:

* every locator is declared once in :data:`SELECTORS` with the 影刀 XPath,
  plus a stable ``css``/``name`` alternative when the site offers one;
* locators are tried in order (css → xpath) so a cosmetic page change only
  breaks one entry;
* text-anchored locators (buttons, banners) are matched on their visible label.

Status: the login/search/read path is written against the live page structure
recorded in ``archive/02_soldeazy详情补数``; export/upload/sync are written from
the 影刀 selector list and still need one live run before they are trusted.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import importlib.util
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlencode

from soldeazy_mcp_server.config import Settings
from soldeazy_mcp_server.errors import SoldeazyError

logger = logging.getLogger("soldeazy_mcp.soldeazy")

BASE_URL = "https://stiger.soldeazy.com"
LIST_PATH = "/app/soldeazy/datasheet/dsheet_list?default_search_profile"
# 无头模式默认的 Chrome UA 会带 HeadlessChrome 字样，
# 站点可能把会话绑在 UA 上，导致「有头登录完、无头用不了」。
# 这里统一用常规桌面 UA，切换模式时登录态才能复用。
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

LOGIN_PATH = "/app/soldeazy/login"
UPLOAD_PATH = "/app/soldeazy/upload-datasheet"

#: 平台单次批量操作（导出 / 删除 / 同步）的数据表条数硬上限。
#:
#: 依据（2026-11-17 从页面快照取证）：
#: - 导出范围文案：``(<N> 笔记录) (每次批量处理数量上限: 3000)``
#: - 页面 JS 注释：``// SECTION: JS bulk actions: delete, export(max 3000), clone(max 200)``
#:
#: 实测后果：某店铺搜索命中 76,096 个数据表，``whole_result`` 导出只回 3,000 个，
#: 静默漏掉 96%。仓库历史导出里有 6 份精确停在 3,000，都是被截断的。
#:
#: 所以任何按"整个搜索结果"操作的动作，都要先拿总数和这个值比：
#: 超过就必须缩小范围（加店铺/站点/SKU 筛选）或分批，绝不能只做一部分还报成功。
PLATFORM_BULK_LIMIT = 3000

#: 「检视」（Inspection View）下拉里「所有」对应的 value。
#:
#: 用户口径（2026-11-18）：**以后统一使用检视所有的页面响应，包括对账**。
#: 页面上的选项是：
#:   ``Default``                    -> [Shop,Site,Title,Gallery,SKU,Price,Post.D,...] 无数量
#:   ``Listing Qty & Stock level``  -> [..., SKU, Qty, Stk]
#:   ``ALL columns``                -> [..., SKU, Price, Qty, Stk, ...]  全列
#: 固定传它，列布局就不会随账号上次的选择而变。
INSPECTION_ALL_COLUMNS = "ALL columns"

#: 刊登方式（Listing Format）的默认值：``BIN`` = 长期在线（Buy It Now）。
#:
#: 用户口径（2026-11-19）：「默认为 bin 除非被要求」。拍卖（``AUC``）
#: 的价格/库存没有改的意义，混进来只会让计划和同步数对不上。
#:
#: 页面上的真实选项（从 HTML 读到的原文，未硬编码猜测）：
#:   ``''`` → 全部、``'AUC'`` → 拍卖、``'BIN'`` → 长期在线
#: 注意空串代表"全部"，所以默认值不能是空串。
DEFAULT_LISTING_FORMAT = "BIN"


def bulk_range(n: int, total: int, limit: int = PLATFORM_BULK_LIMIT) -> tuple[int, int]:
    """第 ``n`` 批（从 0 起）在 ``partial_result`` 里的序号区间，闭区间。

    **序号是倒序的**，这是 2026-11-18 实测出来的，与直觉相反：
    序号 1 对应全量**最后**一条，序号 ``total`` 对应**第一条**。
    实测（FUN/US，1636 个数据表）：``partial 1..10`` 拿到全量末尾 10 个，
    ``1632..1636`` 拿到开头 5 个，``500..600`` 与映射预测 101 条逐条相同。

    好在"按批切段"本身不受方向影响——区间是无重叠、无遗漏的双射，
    所以照样把序号切成 ``1..limit``、``limit+1..2*limit``……即可。

    真正会出事的是**拿上一批的 rowid 去推下一批的起点**：方向反了就会跳段，
    而且不报错。所以切段只按序号算，不按内容猜。
    """

    start = n * limit + 1
    if start > total:
        return 0, 0
    return start, min(start + limit - 1, total)


def bulk_batches(total: int, limit: int = PLATFORM_BULK_LIMIT) -> list[tuple[int, int]]:
    """把 ``total`` 个数据表切成若干序号区间；不需要分批时返回单个区间。"""

    if total <= 0:
        return []
    if total <= limit:
        return [(1, total)]
    ranges: list[tuple[int, int]] = []
    index = 0
    while True:
        start, end = bulk_range(index, total, limit)
        if not start:
            break
        ranges.append((start, end))
        index += 1
    return ranges

#: ``reconcile/`` 目录里保留多少份中间导出文件。
#:
#: 每次读取都落一份全量导出（生产账号 7 千行 / 250KB），不清会无限增长。
#: 但**不能删光**：``last_export_path`` 指向的那份会被"生成人工核对文件"复用，
#: 删掉正在用的会让核对在无声无息中失效。所以保留最近几份，更早的清掉。
KEEP_EXPORT_FILES = 5


def _prune_old_exports(directory: Path, *, keep: int = KEEP_EXPORT_FILES) -> int:
    """清掉 ``directory`` 里过期的 ``state_*.xlsx``，保留最近 ``keep`` 份。

    按修改时间排序（不是文件名——文件名里是事件循环时间，重启后会变小）。
    返回删掉的份数；删除失败只记 warning，绝不影响主流程。
    """

    if keep < 1:
        return 0
    try:
        files = sorted(
            directory.glob("state_*.xlsx"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        logger.warning("清理旧导出时列目录失败：%s", exc)
        return 0

    removed = 0
    for path in files[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("删不掉旧导出 %s：%s", path.name, exc)
    if removed:
        logger.info("清理了 %d 份过期导出，保留最近 %d 份", removed, keep)
    return removed

#: 站点下拉里显示的是英文站点名，不是 US/DE 这种代码。
#:
#: 2026-11-18 实测全量 28 个站点（页面 ``select[name=listsite]`` 的真实选项）。
#: 这张表只作为**别名**用（``DE`` → ``Germany``），真正的解析以页面选项为准；
#: 表里没有的站点照样能跑，只是调用方得写页面上的原名（如 ``Austria``）。
SITE_LABELS: dict[str, str] = {
    "US": "US",
    "UK": "UK",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "AU": "Australia",
    "AT": "Austria",
    "BE": "Belgium_Dutch",
    "BR": "Brazil",
    "CA": "Canada",
    "CN": "China",
    "IN": "India",
    "IE": "Ireland",
    "JP": "Japan",
    "MY": "Malaysia",
    "MX": "Mexico",
    "NL": "Netherlands",
    "PH": "Philippines",
    "PL": "Poland",
    "RU": "Russia",
    "SG": "Singapore",
    "CH": "Switzerland",
    "HK": "HongKong",
}

#: 站点下拉的 value —— **2026-11-18 从线上页面全量实测**（28 项，含空值"全部"）。
#:
#: 这张表只是缓存/别名参考：真正的解析走 `list_site_options()` 读页面，
#: 所以平台新增站点时不需要改代码。保留它是因为：
#: - 离线测试需要一份可信样本；
#: - 报错时能给出"已知站点"提示，而不是让调用方自己去页面上找。
#:
#: 历史备注：2026-11-17 之前的版本只有 12 个，缺 SG/HK/MY 等，
#: 而查不到时会退回空串（= 全部站点），导致"改 SG 的价格"静默改成全站点。
#: 现在查不到直接报错，绝不退回空串。
SITE_CODES: dict[str, str] = {
    "US": "0",
    "CA": "2",
    "UK": "3",
    "AU": "15",
    "AT": "16",
    "BE": "23",
    "FR": "71",
    "DE": "77",
    "IT": "101",
    "BE_NL": "123",
    "NL": "146",
    "ES": "186",
    "CH": "193",
    "HK": "201",
    "IN": "203",
    "IE": "205",
    "MY": "207",
    "CA_FR": "210",
    "PH": "211",
    "PL": "212",
    "RU": "215",
    "SG": "216",
    "MX": "900",
    "BR": "901",
    "CN": "902",
    "JP": "903",
    "MOTORS": "100",
}


@dataclass(frozen=True)
class Locator:
    """One UI element: a stable alternative first, the 影刀 XPath as fallback."""

    name: str
    xpath: str
    css: str = ""

    def candidates(self) -> list[str]:
        if self.css:
            return [self.css, f"xpath={self.xpath}"]
        return [f"xpath={self.xpath}"]


SELECTORS: dict[str, Locator] = {
    # --- 登录页 ---
    "login_user": Locator("输入框_login_user", '//form//input[@type="text"]', "input[name=login_user]"),
    "login_pass": Locator("密码框", '//form//input[@type="password"]', "input[name=login_pass]"),
    "login_submit": Locator("Login", '//form//button[@type="submit"]', "form button[type=submit]"),
    # 「记住用户名」是 **Element UI** 的 checkbox：
    #   <label id="login_remember_username" class="el-checkbox">
    #     <span class="el-checkbox__input"><span class="el-checkbox__inner"></span>
    #       <input type="checkbox" name="login_remember_username"
    #              class="el-checkbox__original">   ← 这个 input 是隐藏的
    #
    # 原生 input 被 el-checkbox__original 隐藏（opacity/尺寸为零），
    # Playwright 的 check() 对它会失败或被静默吞掉。要点**可见的 label**，
    # 点 label 会触发 Vue 的状态切换。
    "login_remember": Locator(
        "记住用户名",
        '//label[contains(@class,"el-checkbox")][.//input[@name="login_remember_username"]]',
        "label.el-checkbox:has(input[name=login_remember_username])",
    ),
    # 判断是否已勾选：Element UI 会把状态挂在 label 的 class 上
    "login_remember_input": Locator(
        "记住用户名-原生input",
        '//input[@name="login_remember_username"]',
        "input[name=login_remember_username]",
    ),
    "ad_popup": Locator("soldeazy-广告弹框", '//div[@class="fancybox-skin"]', ".fancybox-skin"),
    "ad_dismiss": Locator(
        "弹框-(不再显示此消息)",
        '//ul/li//input',
        "#fancybox_dismiss_option",
    ),
    "ad_close": Locator(
        "弹框-关闭",
        '//ul/li//a[normalize-space(.)="关闭"][contains(@class,"fancybox_dismiss")]',
    ),
    # --- datasheet 搜索页 ---
    "search_sku": Locator("SKU输入框", "//form//input", "form input[name=txtproductcode]"),
    "search_site": Locator("Channel Site", "//form//select", "form select[name=listsite]"),
    "search_format": Locator("Listing Format", "//form//select", "form select[name=listformat]"),
    "search_status": Locator("Status", "//form//select", "form select[name=listactive]"),
    "search_date": Locator("Date Modified", "//form//select", "form select[name=listdate]"),
    "search_scope": Locator("Scope", "//form//table//tr/td//select", "form select[name=listlimit]"),
    "search_button": Locator("Search", '//form//input[@type="button"][@name="btn_search"][@id="btn_search"]'),
    "result_table": Locator("搜索结果table", '//form//table[@id="tbl_dSheet"]', "#tbl_dSheet"),
    "export_button": Locator(
        "Export",
        '//form//table//tr/td/input[@type="button"][contains(@class,"btn_01")]'
        '[contains(@class,"bulk-action-btn")][contains(@class,"export")]',
        "input.bulk-action-btn.export",
    ),
    "export_options": Locator("Bulk Export Options", "//select", "#divBulkExportDialog select"),
    "export_profile": Locator("Add / edit an Export Profile", "//select", "#divBulkExportDialog select"),
    "export_confirm": Locator("Bulk Export Options_export", "//button"),
    "export_dialog": Locator("批量导出弹层", '//div[@id="jqizoo"]', "#jqizoo"),
    "export_profile_select": Locator(
        "导出档案下拉",
        "//div[@id='jqizoo']//select[@name='listExppf']",
        "#jqizoo select[name=listExppf]",
    ),
    "export_format_select": Locator(
        "导出格式下拉",
        "//div[@id='jqizoo']//select[@name='listExpfmt']",
        "#jqizoo select[name=listExpfmt]",
    ),
    "bulk_sync_button": Locator("Bulk SYNC", '//form//table//tr/td/input[@type="button"][@id="btn_bulkdalign"]', "#btn_bulkdalign"),
    "sync_dialog": Locator("同步弹框", '//div[@id="jqizoo"]', "#jqizoo"),
    "sync_price_postage": Locator("Price Postage", "//input"),
    "sync_up_button": Locator("SYNC UP", '//button[normalize-space(.)="SYNC UP"]'),
    "sync_banner": Locator(
        "New D-Align bul...",
        '//form//div[starts-with(normalize-space(.),"New D-Align bulk job added")]',
    ),
    # --- 上传页 ---
    "upload_file": Locator("选择文件_filePackage", '//form//input[@type="file"]', "form input[type=file]"),
    "upload_done": Locator("Upload Completed", '//form/div[normalize-space(.)="Upload Completed"]'),
}

#: Column labels of the result table, as seen in the production list page.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "rowid": ("数据表编号", "Row ID", "dsheet_row_id"),
    "shop": ("渠道帐号", "Sales channel"),
    "site": ("站点", "Site"),
    "sku": ("SKU", "dsheet_product_code"),
    "price": ("价格", "Price"),
    "postage": ("运费(当地)", "运费", "Post.D."),
    "qty": ("数量", "Qty"),
    "total": ("合计", "Total"),
}


def _selector_available() -> bool:
    return importlib.util.find_spec("playwright.async_api") is not None


def _bulk_limit_hint(total_records: int) -> dict[str, Any]:
    """把"这次结果有没有超过平台批量上限"如实告诉调用方。

    为什么必须回传：导出 / 批量删除 / 同步共用同一个上限（3000）。超了不会报错，
    只会静默少做一部分——这是最危险的失败方式（工序显示成功，对账才发现少了）。

    只在**真的需要提醒**时才带字段，正常的几千条以内不加噪音。
    """

    if not total_records:
        return {}
    if total_records > PLATFORM_BULK_LIMIT:
        return {
            "bulk_limit_exceeded": True,
            "bulk_limit_notice": (
                f"命中 {total_records} 个数据表，超过平台批量上限 "
                f"{PLATFORM_BULK_LIMIT} 个：导出/删除/同步都会静默截断。"
                "写入前必须加店铺 / 站点 / SKU 筛选缩小范围。"
            ),
            "bulk_limit_excess": total_records - PLATFORM_BULK_LIMIT,
        }
    if total_records == PLATFORM_BULK_LIMIT:
        return {
            "bulk_limit_exceeded": False,
            "bulk_limit_notice": (
                f"命中数恰好等于平台上限 {PLATFORM_BULK_LIMIT} 个，"
                "无法区分「本来就有这么多」和「被截断」；建议缩小范围复核一次。"
            ),
        }
    return {}


def _shop_name_matches(expected: str, name: str) -> bool:
    """店铺名比对：导出列写短名（FUN），页面写长名（EBAY FUN）。

    与 ``soldeazy_change._shop_matches`` / ``soldeazy_apply._shop_name_matches``
    同一口径——三处必须在"什么算同一个店铺"上一致，
    否则会出现"这里说匹配、那里说中止"。
    """

    if not expected or not name:
        return False
    return name == expected or expected in name or name in expected


def _shop_scope_hint(shop: str, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """如实说明这次搜索到底按哪个店铺过滤了。

    平台语义（2026-11-13 用户纠正）：**店铺不选 = 全选**，返回的是所有渠道账号
    的数据混在一起（第一页可能恰好全是某个店的行，容易误读成"默认店铺"）。
    所以没传 shop 时不是"过滤错了"，而是"根本没过滤"——调用方要自己按
    ``shops_in_result`` / 行内店铺列区分归属。
    """

    scoped = bool((shop or "").strip())
    hint: dict[str, Any] = {
        "shop_filter": shop if scoped else "(未选店铺 = 全部店铺)",
        "shop_filter_applied": scoped,
    }
    if records:
        seen = sorted({str(row.get("shop") or "").strip() for row in records} - {""})
        hint["shops_in_result"] = seen
        hint["shop_scope_mismatch"] = bool(
            scoped and seen and any(name != shop and shop not in name for name in seen)
        )
    return hint


def launch_error(
    directory: Path,
    errors: list[str],
    tried: list[str] | None = None,
    idle_seconds: float = 300.0,
) -> SoldeazyError:
    """把浏览器启动失败分类成可操作的错误。

    三类：profile 被别的进程占用 / 权限被拒 / 真的没浏览器。
    第一类最常见也最容易被误解成"装坏了"：Chromium 一个 user-data-dir
    同时只允许一个进程打开，所以 stdio 模式下第二个会话必然抢不到。
    """

    joined = " ".join(errors)
    lowered = joined.lower()
    locked = any(
        word in lowered
        for word in (
            "processsingleton",
            "singletonlock",
            "singleton",
            "already in use",
            "is already running",
        )
    ) or ("lock" in lowered and "profile" in lowered)
    if locked:
        return SoldeazyError(
            "SOLDEAZY_PROFILE_LOCKED",
            f"浏览器 profile 已被另一个进程占用：{directory}。"
            "Chromium 一个 user-data-dir 同时只允许一个进程打开，"
            "所以同一时间只有一个会话能用 Soldeazy 工具——"
            "这与 MCP 是否全局安装无关：stdio 模式下每个会话各自起一个服务进程，"
            "第二个进程抢不到 profile。"
            f"占用方空闲 {idle_seconds:.0f} 秒后会自动释放这把锁"
            "（SOLDEAZY_BROWSER_IDLE_SECONDS，0=不释放），稍等重试通常即可；"
            "要多会话实时共用，请改用一个常驻服务（ebay-mcp-server --transport sse）"
            "让所有会话连同一个进程；或给每个会话配不同的 SOLDEAZY_PROFILE_DIR。",
            details={"attempts": errors},
        )
    if any(
        word in joined
        for word in ("Permission", "EPERM", "Access is denied", "拒绝访问", "EACCES")
    ):
        return SoldeazyError(
            "SOLDEAZY_BROWSER_LAUNCH_DENIED",
            "浏览器已找到，但启动被拒绝（权限/沙箱）。"
            f"进程需要对 profile 目录 {directory} 和临时目录有写权限；"
            "如果 MCP 跑在沙箱里，请把这两个目录加入白名单，"
            "或把它们指到工作区内的可写路径。",
            details={"attempts": errors},
        )
    return SoldeazyError(
        "SOLDEAZY_BROWSER_LAUNCH_FAILED",
        "浏览器启动失败：本机没有可用的 Chrome / Edge / Chromium。"
        "最省事的修法是执行 uv run playwright install chromium，"
        "装好后重试即可；也可以安装 Chrome 或 Edge。",
        details={
            "tried": tried or [],
            "attempts": errors,
            "hint": "LOCALAPPDATA=" + (os.environ.get("LOCALAPPDATA") or "(未设置)"),
        },
    )


def looks_logged_in(html: str) -> bool:
    """用页面 HTML 判断 cookie 是否还有效（不开浏览器的轻量校验）。

    登录页有密码输入框；已登录的数据表页有 ``#tbl_dSheet``。
    两个标记都不在时保守返回 False。
    """

    lowered = html.lower()
    if "tbl_dsheet" in lowered:
        return True
    if 'type="password"' in lowered or 'name="password"' in lowered:
        return False
    return False


_FORM_CONTROL = re.compile(
    r"<select\b.*?</select>|<input\b[^>]*>|<textarea\b.*?</textarea>",
    re.S | re.I,
)
_OPTION = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.S | re.I)
_ATTR = re.compile(r"(\w[\w-]*)\s*=\s*[\"']([^\"']*)[\"']", re.I)
_CELL = re.compile(r"<t[dh]\b.*?</t[dh]>", re.S | re.I)
_ROW = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _tag_attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in _ATTR.finditer(tag)}


def parse_form_state(html: str) -> dict[str, list[str]]:
    """把搜索表单的当前状态读成 ``{name: [values]}``（多选/重名保留多值）。

    和浏览器提交时的序列化口径一致：checkbox/radio 只取 checked，
    select 取 selected（一个都没有就取第一项），hidden/text 取 value。
    定位方式是找含 ``btn_search`` 的那个 form——页面有嵌套 form，
    按 ``<form>...</form>`` 直接切会切错。
    """

    anchor = html.find("btn_search")
    if anchor < 0:
        return {}
    start = html.rfind("<form", 0, anchor)
    end = html.find("</form>", anchor)
    if start < 0 or end < 0:
        return {}
    block = html[start:end]

    state: dict[str, list[str]] = {}

    def add(name: str, value: str) -> None:
        if not name:
            return
        state.setdefault(name, []).append(value)

    for match in _FORM_CONTROL.finditer(block):
        tag = match.group(0)
        head = tag[: tag.find(">") + 1]
        attrs = _tag_attrs(head)
        name = attrs.get("name", "")
        kind = tag.split()[0][1:].lower()
        if kind == "select":
            options = _OPTION.findall(tag)
            picked = [
                (_tag_attrs(raw).get("value", htmllib.unescape(_TAG.sub("", body)).strip()))
                for raw, body in options
                if "selected" in raw.lower()
            ]
            if not picked and options:
                first_raw, first_body = options[0]
                picked = [
                    _tag_attrs(first_raw).get(
                        "value", htmllib.unescape(_TAG.sub("", first_body)).strip()
                    )
                ]
            for value in picked:
                add(name, value)
        elif kind == "textarea":
            add(name, htmllib.unescape(_TAG.sub("", tag[tag.find(">") + 1 :])).strip())
        else:
            input_type = (attrs.get("type") or "text").lower()
            if input_type in {"submit", "button", "image", "reset"} and name != "btn_search":
                continue
            if input_type in {"checkbox", "radio"} and "checked" not in head.lower():
                continue
            add(name, attrs.get("value", ""))
    return state


def cell_text_from_html(cell: str) -> str:
    """<td> 片段转单元格文本：<br> 是变体分隔符，要变成换行。"""

    text = re.sub(r"<br\s*/?>", "\n", cell, flags=re.I)
    text = _TAG.sub("", text)
    return htmllib.unescape(text).strip()


def parse_result_table(html: str) -> list[list[str]]:
    """从搜索结果 HTML 里抠出 ``#tbl_dSheet`` 的单元格矩阵（含表头行）。

    两个真机坑（2026-11-11）：
    - 单元格里嵌着子表格，非贪心的 ``.*?</table>`` 会在子表格处截断；
    - 数据行里也嵌着 ``<tr>``（变体/标题子表），非贪心的 ``.*?</tr>`` 会把
      外层行在嵌套 ``</tr>`` 处切断，丢掉尾部 SKU/价格列。
    表格和行都按嵌套深度切；单元格则保留全部后代 td/th 的文档序——
    与 DOM 通道 ``locator("th,td")`` 的口径一致。
    """

    match = re.search(r"<table\b[^>]*id=[\"']tbl_dSheet[\"']", html, re.I)
    if not match:
        return []
    block = _slice_table(html, match.start())
    return [
        [cell_text_from_html(cell) for cell in _CELL.findall(row)]
        for row in _top_level_rows(block)
    ]


def _top_level_rows(block: str) -> list[str]:
    """按 <tr> 嵌套深度切出顶层行（嵌套子表的行并入父行文本）。"""

    rows: list[str] = []
    depth = 0
    start = -1
    for match in re.finditer(r"<tr\b|</tr>", block, re.I):
        if match.group(0).lower().startswith("<tr"):
            depth += 1
            if depth == 1:
                start = match.start()
        else:
            depth -= 1
            if depth == 0 and start >= 0:
                rows.append(block[start : match.end()])
                start = -1
    return rows


def _slice_table(html: str, start: int) -> str:
    """从 start 处的 <table> 起，按嵌套深度切到配对的 </table>。"""

    depth = 0
    for match in re.finditer(r"<table\b|</table>", html[start:], re.I):
        depth += 1 if match.group(0).lower().startswith("<table") else -1
        if depth == 0:
            return html[start : start + match.end()]
    return html[start:]


def extract_total_records(html: str) -> int:
    """分页条上的总记录数（形如「1,636 笔记录」），读不到返回 0。"""

    match = re.search(r"([\d,]+)\s*笔记录", html)
    if not match:
        return 0
    return int(match.group(1).replace(",", ""))


def extract_sql_token(html: str) -> str:
    """搜索页里的 ``sql_cache_token``（单双引号都见过），同步接口要用。"""

    match = re.search(
        r"name=[\"']sql_cache_token[\"'][^>]*value=[\"']([^\"']*)[\"']", html, re.I
    )
    if match:
        return htmllib.unescape(match.group(1))
    match = re.search(
        r"value=[\"']([^\"']*)[\"'][^>]*name=[\"']sql_cache_token[\"']", html, re.I
    )
    return htmllib.unescape(match.group(1)) if match else ""


def parse_shop_options(html: str) -> list[dict[str, str]]:
    """从列表页 HTML 里读渠道账号下拉（``listshop[]``）的选项。"""

    match = re.search(r"<select\b[^>]*name=[\"']listshop\[\][\"'].*?</select>", html, re.S | re.I)
    if not match:
        return []
    options = []
    for raw, body in _OPTION.findall(match.group(0)):
        attrs = _tag_attrs(raw)
        value = attrs.get("value", "")
        name = htmllib.unescape(_TAG.sub("", body)).strip()
        if value and name:
            options.append({"id": value, "name": name})
    return options


def parse_site_options(html: str) -> list[dict[str, str]]:
    """从列表页 HTML 里读**站点**下拉（``listsite``）的真实选项。

    为什么要从页面读而不是查常量表：

    站点是平台配置，会随账号授权、平台增开市场而变化。硬编码一张
    ``SITE_CODES`` 表，一旦表里缺了某个站点（历史上就缺 SG/HK/MY），
    查不到会退回空串 —— 而空串在平台语义里是"**不筛选站点**"，
    于是"改 SG 的价格"会静默改成所有站点的同 SKU。

    店铺从一开始就是从页面读的（``parse_shop_options``）。站点和店铺是
    同一类东西（都是搜索表单的可选边界），必须用同一种方式获取。
    """

    match = re.search(r"<select\b[^>]*name=[\"']listsite[\"'].*?</select>", html, re.S | re.I)
    if not match:
        return []
    options: list[dict[str, str]] = []
    for raw, body in _OPTION.findall(match.group(0)):
        attrs = _tag_attrs(raw)
        value = attrs.get("value", "")
        label = htmllib.unescape(_TAG.sub("", body)).strip()
        # 空 value 是「全部站点」那一项，单独保留（name 可能是空或"全部"）
        options.append({"id": value, "name": label})
    return options


class SoldeazyBrowser:
    """Drive the Soldeazy datasheet UI with one persistent browser profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = (settings.soldeazy_base_url or BASE_URL).rstrip("/")
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self.active_shop: dict[str, str] | None = None
        #: 最近一次 read_datasheet_values 导出的数据里出现的渠道账号名。
        #: 对账时要靠它确认"读的是不是计划所属的店铺"，否则没设 active_shop 时
        #: 会读到全部店铺混在一起的数据；按计划店铺筛一遍再比。
        self.last_export_shops: list[str] = []
        #: 最近一次搜索报的总数据表数（用于判断导出是否被 3000 上限截断）
        self.last_export_total: int = 0
        #: 最近一次 search 报的命中总数。
        #:
        #: 单独存一份是必须的：``read_datasheet_values(search=False)`` 不会重新
        #: 搜索，只靠 `_last_*` 沿用筛选条件。改造前那里把 ``expected`` 写成 0，
        #: 于是"先看数再导"和"导出后核对条数"两道闸**同时静默失效**——
        #: 拿 3000 条残缺导出当现值去对账，不报错。
        #: （2026-11-18 实机：全站 10,450 个数据表，search=False 时防护完全没触发。）
        self._last_total_records: int = 0
        #: 无法判断是否截断时的留痕（例如总数恰好等于平台上限），不阻断执行
        self.last_export_warnings: list[str] = []
        #: 工序任务归属判断不成立时的留痕（回退到全量列表，见 queue_jobs）
        self.last_queue_ownership_warning: str = ""
        #: 最近一次读取用的导出文件路径。人工核对文件必须基于它生成，
        #: 这样"人看到的"与"会上传的"是同一份数据。
        self.last_export_path: Path | None = None
        #: ``apply_plan`` 本次实际使用的导出文件（可能是复用的那份）。
        #: 上传文件必须基于它生成，不能自己拼 ``<plan_id>.export.xlsx`` ——
        #: 复用时那个路径并不存在（2026-11-19 实测就在这崩过）。
        self.last_apply_export: Path | None = None
        #: 分批读取时各批的导出文件（批模式下核对文件要按批生成，不能只看最后一份）
        self.last_export_batch_paths: list[Path] = []
        #: 平台下拉（站点 / 渠道账号）缓存：``{kind: [(id, name), ...]}``。
        #:
        #: 这些下拉是**账号的静态属性**（88 个渠道账号、28 个站点），不会因为
        #: 搜索而变。而拿到它们必须读一次列表页 HTML —— 实测 17 秒 / 1.2MB。
        #: 一次改价要搜索好几轮（读现值 / 导出前 / 同步前 / 对账），
        #: 不缓存就要把这份 1.2MB 反复拉十几遍。
        #:
        #: 只缓存解析后的**选项列表**（几十个短字符串），不缓存 HTML 本体——
        #: 后者是几百 KB 的字符串，留在内存里才是"给系统添累赘"。
        self._options_cache: dict[str, list[dict[str, str]]] = {}
        self._last_query: str = ""
        self._last_site: str = ""
        #: 最近一次搜索解析出的站点 value（空 = 全站）。导出必须沿用同一个值，
        #: 否则"搜的是 SG、导的是全站"这种错位会让计划的行集对不上。
        self._last_site_code: str = ""
        self._last_status: str = ""
        self._last_status_value: str = ""
        self._last_date: str = ""
        self._last_date_value: str = ""
        #: 最近一次搜索用的刊登方式（``BIN`` / ``AUC`` / ``""``=全部）。
        #: 导出必须沿用同一个值，否则"搜的是 BIN、导的是全部"。
        self._last_listing_format: str = DEFAULT_LISTING_FORMAT
        #: 最近一次搜索页下发的同步令牌；同步接口必须用它，不能沿用旧值
        self._last_sql_token: str = ""
        self.browser_channel_used: str = ""
        self.session_save_error: str = ""
        self.session_cookie_count: int = 0
        #: 空闲自动释放：最后一次碰浏览器的时间 + 挂着的释放任务
        self._last_used: float = time.monotonic()
        self._idle_task: asyncio.Task | None = None

    # ---------- 浏览器空闲释放 ----------
    def _touch(self) -> None:
        """记录"浏览器刚被用过"，并把空闲释放的计时器往后推。"""

        self._last_used = time.monotonic()
        self._schedule_idle_release()

    def _schedule_idle_release(self) -> None:
        idle = float(self.settings.soldeazy_browser_idle_seconds or 0)
        if idle <= 0:
            return
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            self._idle_task = asyncio.ensure_future(self._idle_release(idle))
        except RuntimeError:  # 没有事件循环（纯单测里直接构造对象）
            self._idle_task = None

    async def _idle_release(self, idle: float) -> None:
        """空闲满 idle 秒就关浏览器、交还 profile 锁。

        关闭前先落盘 cookie（close() 里做），所以下一个抢到锁的进程
        不用重新登录。正在跑 search（持有 _lock）时顺延一轮，不掐业务。
        """

        await asyncio.sleep(idle)
        if self._context is None:
            return
        if self._lock.locked():
            self._schedule_idle_release()
            return
        logger.info("Soldeazy 浏览器空闲 %.0f 秒，释放 profile 锁", idle)
        # 先摘引用再关：close() 会取消 _idle_task，不能取消正在运行的自己
        self._idle_task = None
        await self.close()

    # ---------- 生命周期 ----------
    @property
    def profile_dir(self) -> Path | None:
        # 解析走 settings.profile_path：相对路径按 cwd 展开成绝对路径，
        # 否则「相对路径的 .parent」会算到别的目录上去（计划就会写错地方）。
        return self.settings.profile_path

    def profile_ready(self) -> bool:
        directory = self.profile_dir
        return bool(directory and directory.is_dir() and any(directory.iterdir()))

    async def status(self) -> dict[str, Any]:
        session_file = self._session_file() if self.profile_dir else None
        session_cookies = 0
        if session_file and session_file.is_file():
            try:
                session_cookies = len(json.loads(session_file.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                session_cookies = -1  # 文件坏了
        return {
            "name": "soldeazy",
            "base_url": self.base_url,
            "playwright_installed": _selector_available(),
            "profile_dir": str(self.profile_dir) if self.profile_dir else "",
            "profile_ready": self.profile_ready(),
            "credentials_configured": bool(
                self.settings.soldeazy_username and self.settings.soldeazy_password
            ),
            "session_file": str(session_file) if session_file else "",
            "session_file_exists": bool(session_file and session_file.is_file()),
            "session_cookie_count": session_cookies,
            # 不开浏览器问一次站点：cookie 还活着吗。文件不存在直接 False，不发请求。
            "session_valid": (
                await self.session_alive_http()
                if session_file and session_file.is_file()
                else False
            ),
            "session_save_error": self.session_save_error,
            "write_enabled": self.settings.soldeazy_write_enabled,
            "browser_open": self._page is not None,
            "browser_channel_configured": self.settings.soldeazy_browser_channel or "(内置 Chromium)",
            "browser_channel_used": self.browser_channel_used or "(未启动)",
            "browser_idle_release_seconds": self.settings.soldeazy_browser_idle_seconds,
            "headless": self.settings.soldeazy_headless,
            "dirs": self.dir_health(),
        }

    async def _launch_context(self, directory: Path) -> Any:
        """按候选计划逐个尝试启动浏览器，成功后记住选择。

        站点本身不挑浏览器；换机器、没装 Chrome、Playwright 版本不匹配，
        都不该让整条链路失败。
        """

        from soldeazy_mcp_server.providers import soldeazy_browser as browser_probe

        configured = (self.settings.soldeazy_browser_channel or "").strip()
        plan = browser_probe.build_plan(configured)
        plan = browser_probe.reorder(plan, browser_probe.load_cache(directory))

        errors: list[str] = []
        for step in plan:
            label = step["label"]
            kwargs: dict[str, Any] = {
                "headless": self.settings.soldeazy_headless,
                "args": ["--disable-blink-features=AutomationControlled"],
                "locale": "zh-CN",
                # **无论有头无头都固定 UA**。
                #
                # 免浏览器链路（`_http_client`）永远用 `DESKTOP_UA` 发请求，
                # 而服务端把会话绑在客户端身份上。所以浏览器侧的 UA 必须与之一致，
                # 否则"有头登录完、切到 httpx 就用不了"。
                #
                # 2026-11-18 实测对照（同账号、同 cookie、只改 headless）：
                #   无头登录 → httpx 200 有数据表 ✅
                #   有头登录 → httpx 302 回登录页 ❌（浏览器真实 UA 是 Chrome/157，
                #              与 DESKTOP_UA 的 131 不一致）
                "user_agent": DESKTOP_UA,
            }
            if step["executable"]:
                kwargs["executable_path"] = step["executable"]
            elif step["channel"]:
                kwargs["channel"] = step["channel"]
            try:
                context = await self._playwright.chromium.launch_persistent_context(
                    str(directory), **kwargs
                )
                self.browser_channel_used = label
                browser_probe.save_cache(directory, step)
                return context
            except Exception as exc:  # noqa: BLE001 - 换下一个候选
                errors.append(f"{label}: {str(exc).splitlines()[0][:150]}")

        # 分清「没装浏览器」和「装了但起不来（权限/沙箱/被占用）」，别用一句话糊弄过去
        raise launch_error(
            directory,
            errors,
            tried=[step["label"] for step in plan],
            idle_seconds=self.settings.soldeazy_browser_idle_seconds,
        )

    async def _ensure_page(self) -> Any:
        self._touch()
        if self._page is not None:
            return self._page
        if not _selector_available():
            raise SoldeazyError(
                "SOLDEAZY_BROWSER_MISSING",
                "playwright is not installed; run `uv run playwright install chromium`.",
            )
        directory = self.profile_dir
        if directory is None:
            raise SoldeazyError(
                "SOLDEAZY_PROFILE_MISSING",
                "Configure SOLDEAZY_PROFILE_DIR before opening the Soldeazy browser.",
            )
        directory.mkdir(parents=True, exist_ok=True)
        self._ensure_temp_dirs()

        from playwright.async_api import async_playwright

        # 启动失败后必须把 self._playwright 清掉再重试。
        #
        # 原来的写法只在这里赋值，失败时不清理：于是调用方"重试"时
        # ``self._page`` 仍为 None，却又跳过不了这一步，而复用的是一个
        # **已经半坏**的 playwright 实例 —— 重试必然继续失败。
        # 实测症状：连续重试 5 次全失败，但单独起一个进程就能成功。
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._launch_context(directory)
            await self._restore_session()
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            self._page.set_default_timeout(
                self.settings.soldeazy_navigation_timeout * 1000
            )
            return self._page
        except Exception:
            # 把失败的实例彻底拆掉，给下一次重试一个干净起点
            await self._teardown_browser()
            raise

    async def _teardown_browser(self) -> None:
        """拆掉浏览器与 playwright 实例（不碰落盘会话）。

        与 ``close()`` 的区别：``close()`` 会先把 cookie 落盘（那是正常退出），
        而这里是**启动失败后的清理**——此时没有有效会话可存，只是别让半坏的
        实例挡住下一次重试。
        """

        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None
        context, self._context = self._context, None
        self._page = None
        playwright, self._playwright = self._playwright, None
        if context is not None:
            try:
                await context.close()
            except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖原始错误
                logger.debug("清理浏览器上下文失败：%s", exc)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("停止 playwright 失败：%s", exc)

    @staticmethod
    def _ensure_temp_dirs() -> None:
        """TEMP/TMP 指向的目录必须先存在，否则 Playwright 解压内核时会 EPERM。

        新机器常见坑：配置里写了 TEMP=C:\\...\\temp，但这个目录没人建。

        **失败必须记下来**：这里吞掉错误的话，Playwright 会在很远的地方报一个
        看起来无关的 EPERM，排查时根本想不到是"临时目录没建出来"。
        """

        for name in ("TEMP", "TMP", "TMPDIR"):
            raw = (os.environ.get(name) or "").strip()
            if not raw:
                continue
            try:
                Path(raw).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "临时目录建不出来（%s=%s）：%s: %s —— "
                    "Playwright 启动时很可能报 EPERM",
                    name, raw, type(exc).__name__, exc,
                )

    def dir_health(self) -> dict[str, Any]:
        """报告 profile 与临时目录是否可写——部署到新机器时最常用的一次自检。"""

        def probe(directory: Path | None) -> dict[str, Any]:
            if directory is None:
                return {"path": "", "exists": False, "writable": False}
            try:
                directory.mkdir(parents=True, exist_ok=True)
                probe_file = directory / ".write_probe"
                probe_file.write_text("ok", encoding="utf-8")
                probe_file.unlink()
                writable = True
            except OSError as exc:
                return {
                    "path": str(directory),
                    "exists": directory.is_dir(),
                    "writable": False,
                    "error": f"{type(exc).__name__}: {exc}"[:150],
                }
            return {"path": str(directory), "exists": True, "writable": writable}

        temp_raw = (os.environ.get("TEMP") or "").strip()
        return {
            "profile": probe(self.profile_dir),
            "temp": probe(Path(temp_raw) if temp_raw else None),
        }

    def _session_file(self) -> Path:
        return self.profile_dir / "session_cookies.json"

    def _cookie_dict(self) -> dict[str, str]:
        """读落盘 cookie，变成 {name: value}。没有/坏了就返回空。"""

        path = self._session_file() if self.profile_dir else None
        if path is None or not path.is_file():
            return {}
        try:
            cookies = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {
            str(item.get("name")): str(item.get("value"))
            for item in cookies
            if isinstance(item, dict) and item.get("name")
        }

    def _http_client(self, *, follow_redirects: bool = False) -> Any:
        """不依赖浏览器的 HTTP 客户端：cookie 直接来自落盘文件。

        导出/上传/工序查询这些步骤本来就只有"cookie + 表单"两样东西，
        之前借 page.context.request 只是借 cookie。浏览器被空闲释放后，
        这些步骤仍能继续跑，不必重新抢 profile 锁。

        **默认不跟重定向**（2026-11-12 实机）：cookie 过期时登录页会自己
        302 循环，follow_redirects=True 会白跑 20 跳还把日志刷满；
        现在由各调用点显式判 3xx。

        请求头要与浏览器保持一致（UA + Accept 系列）：服务端把会话绑在客户端
        身份上，缺头或不一致会被当成"另一个人"而要求重新登录。UA 与浏览器侧
        共用 ``DESKTOP_UA``（浏览器启动时也强制设成它），这是免浏览器链路能
        跨进程复用的前提。
        """

        import httpx

        return httpx.AsyncClient(
            cookies=self._cookie_dict(),
            headers={
                "User-Agent": DESKTOP_UA,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
            follow_redirects=follow_redirects,
            timeout=180.0,
        )

    @staticmethod
    def _redirected_to_login(response: Any) -> bool:
        """3xx 且 Location 指向登录页 = 服务端不认这份 cookie。"""

        if response.status_code not in {301, 302, 303, 307, 308}:
            return False
        return "login" in (response.headers.get("location") or "").lower()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        multipart: dict[str, Any] | None = None,
        timeout: int = 120_000,
        ensure_page: bool = True,
        page_path: str = "",
        login_hint: str = "",
    ) -> tuple[int, bytes]:
        """发一次请求，优先借用浏览器上下文，没有浏览器就走落盘 cookie。

        返回 ``(状态码, 响应体字节)``。

        为什么要有这一层：导出 / 上传 / 同步 / 工序查询各自都写着
        「有 context 就 page.context.request，否则 httpx + 判重定向」，
        四份几乎一样的代码，其中一份漏了 ``_redirected_to_login`` 就等于
        cookie 过期时静默返回登录页 HTML，被当成业务数据往下走。
        收口到一处以后，这类漏判只有修一次的机会，也只会在这一处出错。

        注意两条通道的登录失效判定不对称：**HTTP 通道**会显式判 3xx 并抛
        ``SOLDEAZY_LOGIN_REQUIRED``；**浏览器通道**仍沿用原来的行为
        （Playwright 自己跟重定向），不额外判——浏览器里 cookie 失效会
        直接在页面上显示登录页，调用方靠返回内容或后续步骤发现。
        """

        async def post():
            if self._context is not None:
                if ensure_page:
                    page = await self._ensure_page()
                    # 导航判断必须和改造前完全一致，否则会多导一次航。
                    # 实测（2026-11-18）：导出前把"只要在站点内就行"收紧成
                    # "必须已经在列表页"，会让导出丢掉当前搜索上下文，
                    # 7077 行的全量变成 5777 行（少 1300 行）却不报错。
                    # 所以只有显式给了 page_path（工序页那种必须落在特定页面的
                    # 场景）才用严格判断，其余仍按"在站点内即视为就位"。
                    if page_path:
                        ready = (page.url or "").startswith(f"{self.base_url}{page_path}")
                    else:
                        ready = (page.url or "").startswith(self.base_url)
                    if not ready:
                        await self._goto(f"{self.base_url}{page_path or LIST_PATH}")
                        await asyncio.sleep(1.5 if page_path else 1.0)
                    request = page.context.request
                else:
                    request = self._context.request
                kwargs: dict[str, Any] = {"timeout": timeout}
                if form is not None:
                    kwargs["form"] = form
                if params is not None:
                    kwargs["params"] = params
                if multipart is not None:
                    kwargs["multipart"] = multipart
                response = await getattr(request, method)(url, **kwargs)
                return response.status, await response.body()

            client_kwargs: dict[str, Any] = {}
            if form is not None:
                client_kwargs["data"] = form
            if params is not None:
                client_kwargs["params"] = params
            if multipart is not None:
                # Playwright 的 multipart 是 {字段: {name, mimeType, buffer}}，
                # httpx 要的是 {字段: (文件名, 内容, 类型)}。在这里换一次，
                # 调用方就只需要写一种形状。
                client_kwargs["files"] = {
                    field: (spec.get("name"), spec.get("buffer"), spec.get("mimeType"))
                    for field, spec in multipart.items()
                }
            async with self._http_client() as client:
                posted = await getattr(client, method)(url, **client_kwargs)
            if self._redirected_to_login(posted):
                raise SoldeazyError(
                    "SOLDEAZY_LOGIN_REQUIRED",
                    login_hint
                    or f"cookie 已失效（{method.upper()} 被重定向到登录页）。"
                    "先调 soldeazy_open_login 重新登录。",
                    details={"status": posted.status_code},
                )
            return posted.status_code, posted.content

        return await post()

    async def _request_text(
        self, method: str, url: str, **kwargs: Any
    ) -> tuple[int, str]:
        """与 :meth:`_request` 相同，但把响应体按服务端编码解成文本。"""

        status, body = await self._request(method, url, **kwargs)
        return status, body.decode("utf-8", "replace").lstrip("\ufeff").strip()

    async def session_alive_http(self) -> bool:
        """不开浏览器，用落盘 cookie 问一次站点：登录态还活着吗。

        最多跟 3 跳（防登录页自环）；任何一跳指向 login 都直接判失效，
        一次请求就够，不再靠 TooManyRedirects 兜底。
        """

        if not self._cookie_dict():
            return False
        url = f"{self.base_url}{LIST_PATH}"
        async with self._http_client() as client:
            for _ in range(3):
                try:
                    response = await client.get(url)
                except Exception:  # noqa: BLE001 - 网络不通当作失效
                    return False
                if response.status_code == 200:
                    return looks_logged_in(response.text)
                if self._redirected_to_login(response):
                    return False
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location") or ""
                    if not location:
                        return False
                    url = location if location.startswith("http") else self.base_url + location
                    continue
                return False
        return False

    async def _save_session(self) -> None:
        """把 cookie 落盘（含会话级 _ht_ses_），重启后仍能复用登录态。

        写盘失败不能静默——那正是"cookie 存不下来、每次都要重新登录"的根因，
        失败原因记在 ``session_save_error`` 里，由 status 暴露出来。
        """

        self.session_save_error = ""
        if self._context is None:
            self.session_save_error = "浏览器上下文未启动，跳过了 cookie 落盘"
            return
        try:
            cookies = await self._context.cookies()
            self.session_cookie_count = len(cookies)
            self._session_file().write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - 记录但不再吞掉
            self.session_save_error = f"{type(exc).__name__}: {exc}"[:200]

    async def _restore_session(self) -> None:
        path = self._session_file()
        if self._context is None or not path.exists():
            return
        try:
            cookies = json.loads(path.read_text(encoding="utf-8"))
            if cookies:
                await self._context.add_cookies(cookies)
        except Exception:  # noqa: BLE001 - 文件坏了就当没有
            pass

    async def close(self) -> None:
        """正常退出：先落盘会话，再拆浏览器。

        与 ``_teardown_browser`` 的唯一区别就是这一步 ``_save_session``——
        正常退出要把 cookie 存下来，启动失败时则没有有效会话可存。
        """

        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None
        if self._context is not None:
            await self._save_session()
        await self._teardown_browser()

    # ---------- 基础动作 ----------
    async def _locate(self, key: str, timeout: int = 8000) -> Any:
        page = await self._ensure_page()
        entry = SELECTORS[key]
        last: Exception | None = None
        for selector in entry.candidates():
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=timeout)
                return locator
            except Exception as exc:  # noqa: BLE001 - 逐个候选试探
                last = exc
        raise SoldeazyError(
            "SOLDEAZY_ELEMENT_MISSING",
            f"找不到页面元素 {entry.name}",
            details={"candidates": entry.candidates(), "error": str(last)[:200]},
        )

    async def _dismiss_popup(self) -> None:
        page = await self._ensure_page()
        for key in ("ad_dismiss", "ad_close"):
            try:
                locator = page.locator(SELECTORS[key].candidates()[-1]).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=3000)
                    await asyncio.sleep(0.5)
            except Exception:  # noqa: BLE001 - 弹框是尽力而为
                continue

    async def _goto(self, url: str, *, attempts: int = 3) -> None:
        """打开页面并重试：站点偶发超时会让整条流程误报成业务错误。"""

        page = await self._ensure_page()
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                return
            except Exception as exc:  # noqa: BLE001 - 网络抖动重试
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(2 * attempt)
        raise SoldeazyError(
            "SOLDEAZY_NAV_FAILED",
            f"打开页面失败（已重试 {attempts} 次）：{url}",
            details={"error": str(last)[:160]},
        )

    # ---------- 登录 ----------
    async def _session_alive(self) -> bool:
        """用现有 cookie 探一下列表页；不碰登录框，也不触发登录。"""

        page = await self._ensure_page()
        try:
            await self._goto(f"{self.base_url}{LIST_PATH}")
            await asyncio.sleep(1.0)
        except Exception:  # noqa: BLE001 - 网络问题交给上层重试
            return False
        if "/login" in (page.url or "").lower():
            return False
        return await page.evaluate(
            "() => !!document.querySelector('select') || !!document.querySelector('#tbl_dSheet')"
        )

    async def interactive_login(self) -> dict[str, Any]:
        page = await self._ensure_page()

        # 先探活：cookie 还有效就直接复用，连登录页都不打开
        if await self._session_alive():
            await self._dismiss_popup()
            return {
                "success": True,
                "reused_session": True,
                "url": page.url,
                "profile_dir": str(self.profile_dir),
                "remember_username": None,
            }

        await self._goto(f"{self.base_url}{LOGIN_PATH}")
        await asyncio.sleep(2)
        await self._dismiss_popup()

        # profile 里已有会话时，站点直接跳过登录页——绝不再登一次
        if "/login" not in (page.url or "").lower():
            await self._goto(f"{self.base_url}{LIST_PATH}")
            await self._dismiss_popup()
            await self._save_session()
            return {
                "success": True,
                "reused_session": True,
                "url": page.url,
                "profile_dir": str(self.profile_dir),
                # 走这条分支时没打开登录页，无从确认「记住用户名」状态；
                # 明确写 None 而不是假装知道
                "remember_username": None,
            }

        username = self.settings.soldeazy_username
        password = self.settings.soldeazy_password
        if username and password:
            await (await self._locate("login_user")).fill(username)
            await (await self._locate("login_pass")).fill(password)
            # 「记住用户名」必须真的勾上——不勾的话会话撑不过一次重启，
            # 这是"反复要登录"的直接原因。勾选结果会回显在 login 返回值里。
            remembered = await self._ensure_remember_username(page)
            await (await self._locate("login_submit")).click()
        else:
            # 无凭据：无头模式下没有窗口可点，别干等——直接给出可执行的下一步
            if self.settings.soldeazy_headless:
                raise SoldeazyError(
                    "SOLDEAZY_CREDENTIALS_MISSING",
                    "当前是无头模式且没有配置凭据，无法自动登录，也没有窗口可人工登录。"
                    "请在 .env 设置 SOLDEAZY_USERNAME / SOLDEAZY_PASSWORD；"
                    "或设 SOLDEAZY_HEADLESS=0 后调用 soldeazy_open_login 手动登录一次，"
                    "登录态会写入 profile，之后改回无头仍然可用。",
                    details={
                        "headless": True,
                        "profile_dir": str(self.profile_dir),
                        "profile_ready": self.profile_ready(),
                    },
                )
            # 有窗口：留给人工，最多等 120 秒
            deadline = min(120.0, self.settings.soldeazy_navigation_timeout * 4)
            await self._wait_logged_in(page, deadline)

        await self._wait_logged_in(page, self.settings.soldeazy_navigation_timeout * 2)
        await self._goto(f"{self.base_url}{LIST_PATH}")
        await self._dismiss_popup()
        await self._save_session()
        # 换了登录态就等于换了账号的可见范围，下拉要重新读
        self._forget_options()
        # 记住用户名没勾上时，重启后一定又要重新登录。把结果如实回传，
        # 让人一眼看到"下次会不会还要登"。
        result: dict[str, Any] = {
            "success": True,
            "url": page.url,
            "profile_dir": str(self.profile_dir),
        }
        if username and password:
            result["remember_username"] = remembered
        return result

    async def _ensure_remember_username(self, page: Any) -> bool:
        """确保「记住用户名」被勾上，返回最终是否勾上。

        为什么这件事值得单独一个方法：

        这是 Element UI 的 checkbox，原生 input 被 ``el-checkbox__original``
        隐藏。对隐藏 input 调 ``check()`` 会失败（或因元素不可见被跳过），
        而旧代码用 ``except: pass`` 把它吞掉了 —— 于是"以为勾了、其实没勾"，
        表现就是**每次重开都要重新登录**。

        所以这里三层保证：

        1. 点**可见的 label**（Element UI 靠它切换 Vue 状态）；
        2. 直接读原生 input 的 ``checked``（这是最终事实，而不是 label 的 class）；
        3. 还没勾上就用 JS 强制设值并派发 change —— 作为最后的兜底。

        失败不抛异常（登录本身还能继续），但**返回 False 让人看见**。
        """

        def log_state(tag: str, state: dict[str, Any]) -> None:
            logger.info(
                "记住用户名[%s]: checked=%s label_class=%r",
                tag, state.get("checked"), state.get("label_class"),
            )

        async def read_state() -> dict[str, Any]:
            try:
                return await page.evaluate(
                    """
                    () => {
                      const box = document.querySelector(
                        'input[name="login_remember_username"]');
                      if (!box) return {missing: true};
                      const label = box.closest('label');
                      return {
                        checked: box.checked,
                        label_class: label ? label.className : '',
                      };
                    }
                    """
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("读记住用户名状态失败：%s", exc)
                return {"missing": True}

        state = await read_state()
        log_state("初始", state)
        if state.get("missing"):
            logger.warning("登录页找不到「记住用户名」勾选框，跳过")
            return False
        if state.get("checked"):
            return True

        # 第 1 层：点可见的 label（Element UI 的正确交互方式）
        try:
            await page.locator(
                "label.el-checkbox:has(input[name=login_remember_username])"
            ).first.click(timeout=5000)
            await asyncio.sleep(0.3)
        except Exception as exc:  # noqa: BLE001 - 换下一层兜底
            logger.warning("点击记住用户名 label 失败：%s", str(exc).splitlines()[0][:120])

        state = await read_state()
        log_state("点击后", state)
        if state.get("checked"):
            return True

        # 第 2 层：JS 直接设值并派发事件（绕过可见性判断）
        try:
            await page.evaluate(
                """
                () => {
                  const box = document.querySelector(
                    'input[name="login_remember_username"]');
                  if (!box) return false;
                  if (!box.checked) {
                    box.checked = true;
                    box.dispatchEvent(new Event('input', {bubbles: true}));
                    box.dispatchEvent(new Event('change', {bubbles: true}));
                  }
                  return true;
                }
                """
            )
            await asyncio.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            logger.warning("JS 勾选记住用户名失败：%s", str(exc).splitlines()[0][:120])

        state = await read_state()
        log_state("JS 兜底后", state)
        if state.get("checked"):
            return True

        logger.warning(
            "「记住用户名」最终没能勾上（label_class=%r）——"
            "这会导致下次启动需要重新登录",
            state.get("label_class"),
        )
        return False

    async def _wait_logged_in(self, page: Any, timeout_seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if "/login" not in (page.url or ""):
                return
            await asyncio.sleep(1.0)
        raise SoldeazyError(
            "SOLDEAZY_LOGIN_TIMEOUT",
            "登录未完成：请在弹出的浏览器窗口里手动登录，或检查 SOLDEAZY_USERNAME/PASSWORD。",
        )

    # ---------- 搜索 ----------
    async def search(
        self,
        sku_terms: str,
        site: str,
        shop: str = "",
        *,
        only_listed: bool = True,
        expand_variants: bool = True,
        listing_format: str = DEFAULT_LISTING_FORMAT,
    ) -> dict[str, Any]:
        """搜索并读回结果。**HTTP 通道优先**（不碰浏览器、不抢 profile 锁），
        HTTP 不可用时回退 DOM 通道。

        `only_listed` 默认开启：只搜「已刊登」的数据表。
        改价/改库存只对已刊登的有意义——未刊登的同步不到 eBay，
        混进来只会让上传数和同步数对不上。

        ``listing_format`` 默认 ``BIN``（长期在线），拍卖（``AUC``）不碰。
        拍卖的价格/库存没有改的意义，混进来只会让计划和同步数对不上。
        要包含全部形式时显式传 ``""``。

        ``expand_variants`` 默认**开启**：一个数据表里堆叠的多个变体各成一条
        记录。之前默认只留第一个变体，其余 SKU 在搜索结果里**凭空消失**
        ——2026-11-13 控价就因此把 ``ZMDC0063``（第 5 个变体）误判成
        "线上找不到"。搜索结果的价值就在于"这个 SKU 在不在"，
        吞掉变体等于给出错误的"不在"。导出/对账走的是 xlsx，不受这里影响。
        """

        self._last_query = sku_terms or ""
        self._last_site = site or ""
        self._last_listing_format = listing_format
        shop = shop or ((self.active_shop or {}).get("name") or "")
        async with self._lock:
            # 站点码在这里**先解析一次**：解析失败要立刻报错，不能等到 HTTP 失败
            # 再回退 DOM 通道——那会让"站点写错"变成"悄悄搜了别的站点"。
            self._last_site_code = await self.resolve_site_code(site)
            try:
                result = await self._search_via_http(
                    sku_terms, site, shop, only_listed,
                    expand_variants=expand_variants,
                    listing_format=listing_format,
                )
            except SoldeazyError as exc:
                if exc.code in {
                    "SOLDEAZY_LOGIN_REQUIRED",
                    "SOLDEAZY_SITE_NOT_FOUND",
                    "SOLDEAZY_SITE_LIST_EMPTY",
                }:
                    raise
                logger.warning("HTTP 搜索失败（%s），回退 DOM 通道", exc.code)
                result = await self._search_via_page(
                    sku_terms, site, shop, only_listed,
                    expand_variants=expand_variants,
                    listing_format=listing_format,
                )
            except Exception as exc:  # noqa: BLE001 - 回退总比整条链路死掉好
                logger.warning("HTTP 搜索异常（%s），回退 DOM 通道", type(exc).__name__)
                result = await self._search_via_page(
                    sku_terms, site, shop, only_listed,
                    expand_variants=expand_variants,
                    listing_format=listing_format,
                )

        # 记下这次命中数：`read_datasheet_values(search=False)` 不会重新搜索，
        # 只能靠它判断"导出会不会被 3000 上限截断"。不记就等于两道闸一起失效。
        self._last_total_records = int(result.get("total_records") or 0)
        return result

    async def _search_via_http(
        self,
        sku_terms: str,
        site: str,
        shop: str,
        only_listed: bool,
        *,
        limit: int = 200,
        expand_variants: bool = True,
        listing_format: str = DEFAULT_LISTING_FORMAT,
    ) -> dict[str, Any]:
        """和浏览器做的一模一样：GET 列表页拿表单当前状态 → 改筛选 → POST。

        比 DOM 通道还多两个好处：结果表是服务端渲染的完整 200 行/页
        （DOM 通道受虚拟化影响只能读到约 65 行），以及顺带拿到
        ``sql_cache_token`` 供同步接口使用。
        """

        shop_id = ""

        async def _resolve_shop_id() -> str:
            if not shop:
                return ""
            # 先用已记住的当前账号：``switch_shop`` 已经拿到了 id，
            # 不必为了把名字换成 id 再去读一次下拉（实测那一步 7~28 秒）。
            active = self.active_shop or {}
            if active.get("id") and _shop_name_matches(shop, str(active.get("name") or "")):
                return str(active["id"])
            known = {o["name"]: o["id"] for o in await self.list_shop_options()}
            if shop in known:
                return known[shop]
            upper = shop.upper()
            for name, oid in known.items():
                if upper in name.upper():
                    return oid
            return ""

        shop_id = await _resolve_shop_id()

        status_value = "All" if only_listed else ""
        self._last_status = "已刊登(所有)" if only_listed else "(空=全部)"
        self._last_status_value = status_value
        self._last_date = "(空)"
        self._last_date_value = ""

        # **不 GET 列表页**：实测（2026-11-18）直接 POST 只需要 1.5 秒，
        # 而先 GET 一次表单要 17 秒 / 1.2MB —— 那一步纯属浪费。
        # 列表页里的其它字段用固定默认值即可（实测能正常搜出结果）；
        # 需要下拉选项的地方（站点/店铺校验）另有缓存，不在这条路径上。
        page_limit_value = max(1, min(int(limit), 200))
        pairs: list[tuple[str, str]] = [
            ("second_mode", "dsheet_list"),
            ("txtproductcode", sku_terms or ""),
            ("listshop[]", shop_id),
            # 站点码已由 resolve_site_code 从页面选项解析好：
            # 空 = 全站；非空 = 精确命中的真实 value。
            ("listsite", self._last_site_code),
            ("listactive", status_value),
            ("listdate", ""),
            # **刊登方式默认 BIN**（用户 2026-11-19 定）。
            #
            # BIN = 长期在线（Buy It Now），是日常改价/改库存的对象；
            # AUC = 拍卖，改它的价格/库存没有意义，不碰。
            #
            # 页面上的真实选项（从 HTML 读到，不硬编码猜）：
            #   '' → 全部、'AUC' → 拍卖、'BIN' → 长期在线
            # 传空串才是"全部"，所以这里默认值必须是 'BIN' 而不是 ''。
            # 注意**不能**写成 ``listing_format or "BIN"``：空串是合法值
            # （用户显式要"全部形式"），`or` 会把它悄悄改成 BIN。
            # 只有 None 才用默认值。
            ("listformat", DEFAULT_LISTING_FORMAT if listing_format is None else listing_format),
            ("view_profile_id", "Default"),
            # **统一用「检视 = 所有」**（用户 2026-11-18 定的口径）。
            #
            # 两种检视的响应结构实测一致（表头 20 格、数据 21 格），
            # 但「所有」包含数量 / 实际库存 / 模板 / 日程 等全部列：
            #   - 数量：对账要看它，默认检视没有这列；
            #   - 不传这个参数时平台按账号上次的选择渲染，
            #     那会让"同一次改动"在不同时刻读到不同的列布局。
            # 固定成 "ALL columns" 就把列布局钉死了。
            ("selsetprofile", INSPECTION_ALL_COLUMNS),
            ("page", "1"),
            ("limit", str(page_limit_value)),
            ("table_action", "show"),
            ("mode", "dsheet_list"),
        ]

        html = ""
        async with self._http_client() as client:
            for url in (
                f"{self.base_url}/app/soldeazy/datasheet",
                f"{self.base_url}/app/soldeazy/datasheet/dsheet_list",
            ):
                # 重名字段（listshop[] / view_profile[] / search_filters[]）必须保留多值，
                # 而 httpx 的 AsyncClient 对 data=list 会生成同步流直接报错，
                # 所以自己 urlencode 成 content 发。
                posted = await client.post(
                    url,
                    content=urlencode(pairs).encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                # 只有真的 <table id=tbl_dSheet> 才算搜到了；CSS 里也出现过
                # 这个 id（选择器文本），不能用子串判断。
                if posted.status_code == 200 and re.search(
                    r"<table\b[^>]*id=[\"']tbl_dSheet[\"']", posted.text, re.I
                ):
                    html = posted.text
                    break
        if not html:
            raise SoldeazyError(
                "SOLDEAZY_HTTP_SEARCH_FAILED",
                "HTTP 搜索两个候选地址都没拿回结果表，回退 DOM 通道。",
            )

        self._last_sql_token = extract_sql_token(html)
        matrix = parse_result_table(html)
        if not matrix:
            return {
                "site": site,
                "shop": shop,
                "search": sku_terms,
                "status_filter": self._last_status,
                "columns": [],
                "row_count": 0,
                "rows": [],
                "page_limit": page_limit_value,
                "total_records": extract_total_records(html),
                **_shop_scope_hint(shop),
                "empty_reason": (
                    "没有匹配的数据表"
                    + ("（当前只查已刊登，可能被状态过滤掉了）" if only_listed else "")
                ),
            }
        header, records = parse_rows(matrix, expand_variants=expand_variants)
        total_records = extract_total_records(html)
        return {
            "site": site,
            "shop": shop,
            "search": sku_terms,
            "status_filter": self._last_status,
            "date_filter": self._last_date,
            "columns": header,
            "row_count": len(records),
            "datasheet_count": _count_datasheets(records),
            "rows": records,
            "page_limit": page_limit_value,
            "total_records": total_records,
            # 平台天花板：超过这个数的批量导出/删除/同步都会静默截断
            "platform_bulk_limit": PLATFORM_BULK_LIMIT,
            **_bulk_limit_hint(total_records),
            **_shop_scope_hint(shop, records),
        }

    async def _search_via_page(
        self,
        sku_terms: str,
        site: str,
        shop: str,
        only_listed: bool,
        *,
        expand_variants: bool = True,
        listing_format: str = DEFAULT_LISTING_FORMAT,
    ) -> dict[str, Any]:
        """DOM 通道（回退用）：调用方已持有 ``self._lock``。"""

        page = await self._ensure_page()
        await self._goto(f"{self.base_url}{LIST_PATH}")
        await self._dismiss_popup()

        # 刊登方式：默认 BIN（长期在线），拍卖不碰。
        # 两条通道必须用同一口径，否则 HTTP 失败回退到 DOM 时范围会变。
        try:
            scope = page.locator("select[name='listformat']").first
            if await scope.count():
                await scope.select_option(listing_format or "", timeout=5000)
        except Exception as exc:  # noqa: BLE001 - 设不上不影响主流程，但留痕
            logger.warning("DOM 通道设置刊登方式失败（%s）：%s", listing_format, exc)

        await (await self._locate("search_sku")).fill(sku_terms)
        if site:
            await self._select_site(site)
        if shop:
            await self._select_shop(shop)
        # 修改日期默认留空，避免沿用上次筛选导致漏数据
        await self._clear_date_filter()
        # 改价/改库存只对「已刊登」有意义：未刊登的数据表同步不到 eBay，
        # 传上去只会让上传数和同步数对不上。
        if only_listed:
            await self._select_status_listed()
        else:
            # 不限状态时必须把档位清回空（=全部）：页面 select 会沿用上次的值，
            # 不清的话 include_unlisted=true 实际仍按「已刊登」过滤。
            await self._select_status_all()
        await self._select_scope_whole_result()
        await (await self._locate("search_button")).click()
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(self.settings.soldeazy_min_delay)

        try:
            table = await self._locate("result_table")
        except SoldeazyError:
            # 没有结果表 = 这次搜索 0 条，不是错误。
            # 典型场景：只查「已刊登」但该账号全是未刊登。
            return {
                "site": site,
                "shop": shop,
                "search": sku_terms,
                "status_filter": self._last_status
                or ("已刊登" if only_listed else "不限"),
                "columns": [],
                "row_count": 0,
                "rows": [],
                **_shop_scope_hint(shop),
                "empty_reason": (
                    "没有匹配的数据表"
                    + ("（当前只查已刊登，可能被状态过滤掉了）" if only_listed else "")
                ),
            }

        # 搜索提交会把每页条数重置回 25，所以要在搜索**之后**调大页；
        # 调档是 AJAX 重拉，会保留本次搜索条件（实测 308 笔不变）。
        # 注意（2026-11-11 实机）：调档的 AJAX 重拉会换掉导出按钮的点击绑定，
        # 之后再点「导出」弹层不再出现。所以 search 里**只读不改**，
        # 真正需要大页的勾选动作在 select_datasheets 里自己调档。
        page_limit = await self._read_page_limit()
        table = await self._locate("result_table")
        trs = table.locator("tr")
        total = await trs.count()
        cell_rows: list[list[str]] = []
        for index in range(total):
            cells = await trs.nth(index).locator("th,td").all_inner_texts()
            cell_rows.append(cells)  # 原样交给 parse_rows：换行是变体分隔符
        header, records = parse_rows(cell_rows, expand_variants=expand_variants)
        self._last_sql_token = await self._eval_with_retry(
            page,
            "() => ((document.querySelector('input[name=sql_cache_token]')||{}).value || '')",
        )
        total_records = await self._read_total_records()
        return {
            "site": site,
            "shop": shop,
            "search": sku_terms,
            "status_filter": self._last_status or ("已刊登" if only_listed else "不限"),
            "date_filter": self._last_date or "(空)",
            "columns": header,
            "row_count": len(records),
            "datasheet_count": _count_datasheets(records),
            "rows": records,
            "page_limit": page_limit,
            "total_records": total_records,
            # 平台天花板：超过这个数的批量导出/删除/同步都会静默截断
            "platform_bulk_limit": PLATFORM_BULK_LIMIT,
            **_bulk_limit_hint(total_records),
            **_shop_scope_hint(shop, records),
        }

    async def list_shop_options(self) -> list[dict[str, str]]:
        """列出可用渠道账号（搜索表单里 `select[name='listshop[]']` 的选项）。

        这是站点给当前登录账号授权的门店清单，比 /ebayac 页面更可靠。
        """

        options = await self._options("shop", _parse_shop_options_nonempty)
        if not options and self._context is not None:
            # 可能停在登录页：确保登录后再读一次
            await self.interactive_login()
            self._forget_options()
            options = await self._options("shop", _parse_shop_options_nonempty)
        return options

    async def switch_shop(self, shop: str) -> dict[str, str]:
        """切换当前渠道账号（后续搜索/预览/写入默认用它）。"""

        wanted = (shop or "").strip()
        if not wanted:
            raise SoldeazyError("SOLDEAZY_SHOP_REQUIRED", "请给出渠道账号简称或 id")
        options = await self.list_shop_options()
        if not options:
            raise SoldeazyError("SOLDEAZY_SHOP_LIST_EMPTY", "没能读到渠道账号列表")

        hit = next((o for o in options if o["id"] == wanted), None)
        if hit is None:
            upper = wanted.upper()
            hit = next((o for o in options if o["name"].upper() == upper), None)
        if hit is None:
            hit = next((o for o in options if upper in o["name"].upper()), None)
        if hit is None:
            raise SoldeazyError(
                "SOLDEAZY_SHOP_NOT_FOUND",
                f"渠道账号里没有 {shop}",
                details={"available": [o["name"] for o in options][:20]},
            )

        self.active_shop = hit
        return hit

    async def _list_page_html(self, *, force: bool = False) -> str:
        """拿列表页 HTML 并校验登录态。

        **只在确实需要下拉选项时才调它**（见 ``_options``）。搜索路径不调：
        搜索直接 POST，不需要这份 1.2MB。
        """

        _status, text = await self._request_text(
            "get",
            f"{self.base_url}{LIST_PATH}",
            ensure_page=False,
            page_path=LIST_PATH,
            login_hint="cookie 缺失或已失效：先调 soldeazy_open_login 登录一次。",
        )
        if not looks_logged_in(text):
            raise SoldeazyError(
                "SOLDEAZY_LOGIN_REQUIRED",
                "cookie 缺失或已失效：先调 soldeazy_open_login 登录一次。",
                details={"session_file": str(self._session_file())},
            )
        return text

    async def _options(self, kind: str, parser: Any) -> list[dict[str, str]]:
        """取平台下拉选项（``site`` / ``shop``），按 ``kind`` 缓存解析结果。

        缓存的是**几十个短字符串**，不是那份 1.2MB HTML —— 后者留在内存里
        才是真正的负担。下拉是账号的静态属性，不会随搜索变化。
        """

        if kind in self._options_cache:
            return self._options_cache[kind]
        if self._context is None:
            options = parser(await self._list_page_html())
        else:
            options = await self._options_via_page(kind)
        if options:
            self._options_cache[kind] = options
        return options

    async def _options_via_page(self, kind: str) -> list[dict[str, str]]:
        """浏览器通道：从已打开的页面直接读下拉。"""

        page = await self._ensure_page()
        if not (page.url or "").startswith(self.base_url):
            await self._goto(f"{self.base_url}{LIST_PATH}")
            await asyncio.sleep(1.5)
        name = "listsite" if kind == "site" else "listshop[]"
        return await page.evaluate(
            """
            (name) => {
              const sel = Array.from(document.querySelectorAll('select'))
                .find(x => x.name === name);
              if (!sel) return [];
              return Array.from(sel.options)
                .map(o => ({id: o.value, name: o.textContent.trim()}));
            }
            """,
            name,
        )

    def _forget_options(self) -> None:
        """丢掉下拉缓存（换账号、重新登录后必须清）。"""

        self._options_cache.clear()

    async def list_site_options(self) -> list[dict[str, str]]:
        """列出平台站点下拉（``select[name='listsite']``）的真实选项。

        和 ``list_shop_options`` 同一种做法：**从页面读**，不查硬编码表。
        站点和店铺是同一类东西（搜索表单的可选边界），必须用同一种方式获取。

        返回 ``[{id, name}]``，其中 ``id`` 是提交给平台的 value，
        ``name`` 是下拉里显示的文字（如 ``Germany`` / ``Singapore``）。
        空 ``id`` 表示「全部站点」那一项。
        """

        options = await self._options("site", parse_site_options)
        if not options and self._context is not None:
            # 页面里读不到下拉：可能掉到登录页了，走一次交互登录再读
            await self.interactive_login()
            self._forget_options()
            options = await self._options("site", parse_site_options)
        return options

    async def resolve_site_code(self, site: str) -> str:
        """把用户写的站点（``US`` / ``Germany`` / ``sg``）解析成平台 value。

        三条硬规则：

        1. **站点为空 = 全站**（平台语义：不勾选即全部），直接返回 ``""``；
        2. 给了站点就必须**精确命中**，命中不了直接报错并列出可用站点 ——
           绝不退回空串，因为空串会变成"搜全部站点"，把改动写到别的站点去；
        3. 解析依据是**页面上的真实选项**，不是硬编码表。
        """

        wanted = (site or "").strip()
        if not wanted:
            return ""

        options = await self.list_site_options()
        if not options:
            raise SoldeazyError(
                "SOLDEAZY_SITE_LIST_EMPTY",
                "没能读到站点下拉，无法确定该站点是否存在；拒绝按「全部站点」继续。",
                details={"requested": wanted},
            )

        key = wanted.upper()
        selectable = [o for o in options if o["id"]]

        # 1) 完全等于 value
        hit = next((o for o in selectable if o["id"] == wanted), None)
        # 2) 完全等于显示名（忽略大小写）：US / Germany / Singapore
        if hit is None:
            hit = next((o for o in selectable if o["name"].upper() == key), None)
        # 3) 已知别名：DE -> Germany、HK -> HongKong 之类
        if hit is None:
            alias = SITE_LABELS.get(key)
            if alias:
                hit = next((o for o in selectable if o["name"].upper() == alias.upper()), None)

        if hit is None:
            raise SoldeazyError(
                "SOLDEAZY_SITE_NOT_FOUND",
                f"站点下拉里没有 {site!r}。未按「全部站点」继续——那样会把改动写到别的站点。",
                details={
                    "requested": wanted,
                    "available": [
                        {"id": o["id"], "name": o["name"]} for o in selectable
                    ][:30],
                },
            )
        return hit["id"]

    async def _select_shop(self, shop: str) -> None:
        """按渠道账号过滤。控件是 Chosen 多选，原生 select 被隐藏，
        只能直接设 selected 再触发 change/chosen:updated。"""

        page = await self._ensure_page()
        picked = await page.evaluate("""
        (shop) => {
          const s = document.querySelector("select[name='listshop[]']");
          if (!s) return null;
          Array.from(s.options).forEach(o => { o.selected = false; });
          const opt = Array.from(s.options).find(o => o.textContent.trim() === shop)
                   || Array.from(s.options).find(o => o.textContent.includes(shop));
          if (!opt) return null;
          opt.selected = true;
          s.dispatchEvent(new Event("change", {bubbles: true}));
          if (window.jQuery) { try { jQuery(s).trigger("chosen:updated"); } catch (e) {} }
          return {value: opt.value, text: opt.textContent.trim()};
        }
        """, shop)
        if not picked:
            raise SoldeazyError(
                "SOLDEAZY_SHOP_NOT_FOUND",
                f"渠道账号下拉里找不到 {shop}",
            )

    async def _select_scope_whole_result(self) -> None:
        """导出范围要覆盖整个搜索结果，不能只导勾选行。

        真机上这是表单里的 `select[name=bulk_option]`：`whole_result` /
        `partial_result` / `selected_items`。找不到就当页面不提供该控件。
        """

        page = await self._ensure_page()
        scope = page.locator("select[name=bulk_option]").first
        try:
            if await scope.count():
                await scope.select_option("whole_result", timeout=5000)
        except Exception:  # noqa: BLE001 - 只是尽力设置
            pass

    async def _read_page_limit(self) -> int:
        """只读当前每页条数档位，不做任何修改。"""

        page = await self._ensure_page()
        value = await self._eval_with_retry(
            page,
            "() => (document.querySelector('.pagination select.limit') || {}).value || ''",
        )
        try:
            return int(value)
        except ValueError:
            return 0

    async def _read_total_records(self) -> int:
        """读分页条上的总记录数（形如「308 笔记录」），读不到返回 0。"""

        page = await self._ensure_page()
        text = await self._eval_with_retry(
            page,
            """
            () => {
              const hits = Array.from(document.querySelectorAll('.pagination a, .pagination li'))
                .map((e) => (e.textContent || '').trim())
                .filter((t) => /\\d/.test(t) && t.length < 30);
              return hits[0] || '';
            }
            """,
        )
        digits = "".join(ch for ch in str(text or "") if ch.isdigit())
        return int(digits) if digits else 0

    async def _eval_with_retry(self, page: Any, script: str, arg: Any = None, *, tries: int = 4) -> Any:
        """页面 AJAX 重拉时 evaluate 会撞导航（context destroyed），重试即可。"""

        last: Exception | None = None
        for _ in range(tries):
            try:
                return await page.evaluate(script, arg) if arg is not None else await page.evaluate(script)
            except Exception as exc:  # noqa: BLE001 - 只吞导航竞态
                last = exc
                text = str(exc)
                if "destroyed" not in text and "navigation" not in text.lower():
                    raise
                await asyncio.sleep(2)
        raise last or SoldeazyError("SOLDEAZY_EVAL_FAILED", "页面脚本执行失败")

    async def _clear_date_filter(self) -> None:
        """修改日期默认留空，不沿用页面上次的值。找不到就跳过。"""

        page = await self._ensure_page()
        try:
            cleared = await page.evaluate(
                """
                () => {
                  const sel = Array.from(document.querySelectorAll('select'))
                    .find((x) => x.name === 'listdate');
                  if (!sel) return null;
                  const blank = Array.from(sel.options).find((o) => !(o.value || '').trim());
                  if (!blank) return null;
                  sel.value = blank.value;
                  sel.dispatchEvent(new Event('change', { bubbles: true }));
                  return { value: blank.value, text: (blank.textContent || '').trim() };
                }
                """
            )
            if cleared is not None:
                self._last_date = str(cleared.get("text") or "")
                self._last_date_value = str(cleared.get("value") or "")
        except Exception:  # noqa: BLE001 - 尽力而为，不阻断搜索
            pass

    async def _select_status_listed(self) -> None:
        """把状态筛成「已刊登(所有)」。找不到就跳过，不阻断搜索。"""

        page = await self._ensure_page()
        picked = await page.evaluate(
            """
            () => {
              const sel = Array.from(document.querySelectorAll('select'))
                .find((x) => x.name === 'listactive');
              if (!sel) return null;
              const options = Array.from(sel.options);
              // 英文界面 Listed(All)，中文界面 已刊登(所有)；排除 Unlisted / 未刊登
              const hit = options.find((o) => {
                const text = (o.textContent || '').trim();
                const lower = text.toLowerCase();
                if (lower.includes('unlisted') || text.includes('未刊登')) return false;
                return lower.includes('listed') || text.includes('已刊登');
              });
              if (!hit) return null;
              if (sel.value !== hit.value) {
                sel.value = hit.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
              }
              return { value: hit.value, text: (hit.textContent || '').trim() };
            }
            """
        )
        if picked:
            self._last_status = str(picked.get("text") or "")
            self._last_status_value = str(picked.get("value") or "")

    async def _select_status_all(self) -> None:
        """把状态档位清回空（= 全部）。页面 select 会沿用上次的值。"""

        page = await self._ensure_page()
        picked = await page.evaluate(
            """
            () => {
              const sel = Array.from(document.querySelectorAll('select'))
                .find((x) => x.name === 'listactive');
              if (!sel) return null;
              const blank = Array.from(sel.options).find((o) => !(o.value || '').trim());
              if (!blank) return null;
              if (sel.value !== blank.value) {
                sel.value = blank.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
              }
              return { value: blank.value, text: (blank.textContent || '').trim() };
            }
            """
        )
        if picked:
            self._last_status = str(picked.get("text") or "") or "(空=全部)"
            self._last_status_value = str(picked.get("value") or "")

    async def _select_site(self, site: str) -> None:
        """在 DOM 里选中站点下拉。

        解析统一走 ``resolve_site_code``（从页面选项精确匹配），
        这里只负责"把解析好的 value 点上去"。**不再按 label 猜** ——
        猜测会让"SG 选不中"悄悄退化成"没选 = 全站"，正是要避免的那种错。
        """

        if not (site or "").strip():
            return  # 空 = 全站，不动下拉

        code = self._last_site_code or await self.resolve_site_code(site)
        locator = await self._locate("search_site")
        try:
            await locator.select_option(code, timeout=5000)
        except Exception as exc:  # noqa: BLE001
            raise SoldeazyError(
                "SOLDEAZY_SITE_OPTION_MISSING",
                f"站点下拉里选不中 {site!r}（解析出的 value={code!r}）",
                details={"requested": site, "resolved_code": code},
            ) from exc

    # ---------- 导出 / 上传 / 同步（需真机验证） ----------
    async def export_via_api(
        self,
        dest: Path,
        *,
        profile: str = "55",
        ensure_page: bool = True,
        bulk_from: int | None = None,
        bulk_to: int | None = None,
    ) -> dict[str, Any]:
        """不导航页面，直接用会话 cookie 提交导出表单。

        2026-11-11 起作为**默认导出通道**：页面通道的弹层（`#jqizoo`）在部分
        会话下点「导出」后不出现（FUN 实机复现），而本通道的筛选参数直接取自
        最近一次 search（`_last_query` / `_last_site` / `_last_status_value` /
        `_last_date_value`），行集与计划一致，且实测一次拿到全量 7075 行。

        **浏览器被空闲释放后本通道仍可工作**：没有 context 时改用落盘 cookie
        的独立 HTTP 客户端，不再抢 profile 锁。cookie 失效会明确报
        ``SOLDEAZY_LOGIN_REQUIRED``，而不是让用户猜。

        ``bulk_from`` / ``bulk_to`` 给出时走 **partial_result（按序号区间）**：
        这是绕开 3000 上限的唯一官方途径。序号是 1-based 闭区间，对应导出
        结果的行序（2026-11-18 实测：partial 1..10 与全量前 10 个逐个相同，
        末尾区间同样对得上）。不给区间时仍是 whole_result。
        """

        partial = bulk_from is not None or bulk_to is not None
        if partial and (bulk_from is None or bulk_to is None):
            raise SoldeazyError(
                "SOLDEAZY_BAD_RANGE",
                "按区间导出必须同时给 from 和 to（1-based、闭区间）",
                details={"bulk_from": bulk_from, "bulk_to": bulk_to},
            )
        if partial and int(bulk_from) > int(bulk_to):  # type: ignore[arg-type]
            raise SoldeazyError(
                "SOLDEAZY_BAD_RANGE",
                f"导出区间起点大于终点：{bulk_from} > {bulk_to}",
                details={"bulk_from": bulk_from, "bulk_to": bulk_to},
            )

        shop_id = (self.active_shop or {}).get("id", "")
        # 沿用最近一次 search 解析出的站点 value。这里绝不能再查一遍硬编码表：
        # 两处取法不一致时会出现"搜的是 SG、导的是全站"，计划行集直接错位。
        site_code = self._last_site_code

        base_form = {
            "second_mode": "dsheet_list",
            "txtproductcode": self._last_query,
            "listshop[]": shop_id,
            "listsite": site_code,
            # 筛选条件必须和最近一次 search 完全一致，否则导出的行集对不上计划：
            # 日期默认留空（search 里清过），状态沿用 search 选中的档位
            # （only_listed=True 时是「已刊登(所有)」，False 时留空=全部）。
            "listdate": self._last_date_value or "",
            "listactive": self._last_status_value or "",
            # **导出必须沿用搜索时的刊登方式**，否则会"搜的是 BIN、
            # 导的是全部"——计划行集直接错位，AUC 的行混进来后被当成要改的对象。
            "listformat": self._last_listing_format or "",
            "view_profile_id": "Default",
            "radioEditTypeValue": "schedule",
            # bulk_option_from/to 只对 partial_result 有效——页面上它们只在
            # partial_result 那个 span 里出现。whole_result 下填了会被忽略，
            # 而且会让人误以为已按区间取值，所以默认不填。
            "listExppf": profile,
            "listExpfmt": "xlsx",
        }
        if partial:
            base_form["bulk_option_from"] = str(int(bulk_from))   # type: ignore[arg-type]
            base_form["bulk_option_to"] = str(int(bulk_to))       # type: ignore[arg-type]
        # 实测：导出用的是 bulk-export + dsheet_datatable，不是搜索用的 show/dsheet_list
        extra = {
            "table_action": "bulk-export",
            "mode": "dsheet_datatable",
            "bulk_option": "partial_result" if partial else "whole_result",
        }
        url = f"{self.base_url}/app/soldeazy/datasheet"
        if self._context is None and not await self.session_alive_http():
            # 浏览器已被空闲释放（或本进程根本没开过），且落盘 cookie 也不可用。
            # 先探活再发大请求：直接发会拿到登录页 HTML，再被当成"没返回 xlsx"，
            # 报成导出失败，掩盖真正的"没登录"。
            raise SoldeazyError(
                "SOLDEAZY_LOGIN_REQUIRED",
                "没有可用登录态：cookie 缺失或已失效，且浏览器未启动。"
                "先调 soldeazy_open_login 登录一次（或等占用 profile 的会话释放）。",
                details={"session_file": str(self._session_file())},
            )
        status, body = await self._request(
            "post",
            url,
            form={**base_form, **extra},
            timeout=180_000,
            ensure_page=ensure_page,
            login_hint=(
                "cookie 已失效（导出被重定向到登录页）。先调 soldeazy_open_login 重新登录。"
            ),
        )
        if not body or body[:2] != b"PK":
            raise SoldeazyError(
                "SOLDEAZY_EXPORT_API_FAILED",
                "cookie 导出没有返回 xlsx",
                details={
                    "status": status,
                    "head": body[:160].decode("utf-8", "ignore") if body else "",
                    "form": extra,
                },
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return {"path": str(dest), "size": len(body), "channel": "cookie", "form": extra}

    async def sync_up_via_api(
        self, *, price_postage: bool = True, quantity: bool = False,
        row_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """用接口提交「同步上平台」，替代点弹框。

        前提：当前页面已在搜索结果上、且目标行已勾选（`table_row_ids`）。
        `sql_cache_token` 由本次搜索下发，必须从页面上取，不能沿用旧值。

        ``row_ids`` 显式传入时直接用它——勾选框只在当前页存在，跨页勾选后
        页面上读到的勾选集合不完整，接口参数必须按计划的完整清单给。
        """

        if row_ids:
            row_ids = [str(item).strip() for item in row_ids if str(item).strip()]

        form_head = {
            "mode": "bulk_dalign",
            "second_mode": "dsheet_list",
            "table_action": "show",
            "bulk_option": "selected_items",
            "dalign_price": "1" if price_postage else "0",
            "dalign_imghttps": "1",
            "dalign_option": "2",
        }
        if quantity:
            form_head["dalign_qty"] = "1"
        url = f"{self.base_url}/app/soldeazy/datasheet"

        # 令牌与勾选集合只在页面上有；没有浏览器时用最近一次搜索存下的令牌。
        # 两种情况都要求 row_ids 最终非空——空清单发出去平台会当成"全选"，
        # 那是把整店数据推上平台，比报错危险得多。
        if self._context is not None:
            page = await self._ensure_page()
            token = await self._eval_with_retry(
                page,
                "() => ((document.querySelector('input[name=sql_cache_token]')||{}).value || '')",
            )
            if not row_ids:
                row_ids = await self._eval_with_retry(page, """
                () => {
                  const out = [];
                  Array.from(document.querySelectorAll('#tbl_dSheet tr')).forEach(r => {
                    const box = r.querySelector('input[type=checkbox]');
                    if (!box || !box.checked) return;
                    const cells = Array.from(r.cells).map(c => (c.innerText || '').trim());
                    const hit = cells.find(t => /^[0-9]{6,}$/.test(t));
                    if (hit) out.push(hit);
                  });
                  return out;
                }
                """)
        else:
            token = self._last_sql_token
            if not token:
                raise SoldeazyError(
                    "SOLDEAZY_TOKEN_MISSING",
                    "没有可用的同步令牌：先跑一次 search（HTTP 通道会顺带取令牌）。",
                )

        if not row_ids:
            raise SoldeazyError("SOLDEAZY_NO_SELECTION", "没有勾选任何数据表，无法同步。")

        status, text = await self._request_text(
            "post",
            url,
            form={
                **form_head,
                "table_row_ids": ",".join(row_ids),
                "sql_cache_token": token,
            },
            login_hint=(
                "cookie 已失效（同步被重定向到登录页）。先调 soldeazy_open_login 重新登录。"
            ),
        )

        hints = []
        for marker in ("没有在线刊登", "New D-Align", "同步成功", "已加入", "失败"):
            if marker in text:
                hints.append(marker)
        return {
            "success": status == 200,
            "status": status,
            "rows": len(row_ids),
            "table_row_ids": row_ids,
            "hints": hints,
        }

    async def create_blank_datasheet(
        self, *, shop_idx: str, site_code: str = "US", template_idx: str = ""
    ) -> dict[str, Any]:
        """从0创建一条空数据表，返回新 rowid（模板只预设刊登参数，不含产品数据）。"""

        page = await self._ensure_page()
        await self._goto(f"{self.base_url}{LIST_PATH}")
        await asyncio.sleep(1.5)
        before = await self._row_ids(page)

        response = await page.context.request.post(
            f"{self.base_url}/app/soldeazy/datasheet-create/index",
            form={
                "mode": "dsheet_datatable",
                "table_action": "create",
                "new_dsheet_shop_initial_idx": str(shop_idx),
                "new_dsheet_site_code": site_code,
                "new_dsheet_biz_profile_idx": "",
                "new_dsheet_listing_template_idx": str(template_idx),
                "new_dsheet_price_margin": "NaN",
                "new_dsheet_channel_service_fee": "NaN",
                "new_dsheet_price": "NaN",
            },
            timeout=120_000,
        )
        html = (await response.text()).lstrip("\ufeff")
        match = re.search(r"dsheet_row_id[^>]*value=['\"](\d+)", html) or re.search(
            r"table_row_id=(\d+)", html
        )
        if match:
            return {"success": True, "rowid": match.group(1), "source": "response"}

        await self._goto(f"{self.base_url}{LIST_PATH}")
        await asyncio.sleep(2)
        after = await self._row_ids(page)
        new = [rowid for rowid in after if rowid not in before]
        if not new:
            raise SoldeazyError(
                "SOLDEAZY_CREATE_NO_ROWID", "创建后没能确定新数据表编号"
            )
        return {"success": True, "rowid": new[0], "source": "list_diff"}

    async def _row_ids(self, page: Any) -> list[str]:
        return await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('#tbl_dSheet tr'))
              .map(r => Array.from(r.cells).map(c => (c.innerText || '').trim()))
              .map(c => c.find(t => /^\\d{6,}$/.test(t))).filter(Boolean)
            """
        )

    async def save_datasheet_fields(
        self, rowid: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """打开数据表编辑页，按字段名填值并点「储存」。

        价格/数量/SKU 在 ``dsku_price[]`` / ``dsku_qty[]`` / ``dsku_product_code[]``，
        用 ``dsheet_price`` 之类会静默丢字段。
        """

        page = await self._ensure_page()
        await self._goto(f"{self.base_url}{LIST_PATH}")
        await asyncio.sleep(1.5)
        await page.evaluate(
            """
            (rowid) => {
              const f = document.createElement('form');
              f.method = 'POST';
              f.action = '/app/soldeazy/datasheet';
              const add = (n, v) => {
                const i = document.createElement('input');
                i.type = 'hidden'; i.name = n; i.value = v;
                f.appendChild(i);
              };
              add('mode', 'dsheet_datatable');
              add('table_action', 'edit');
              add('table_row_id', rowid);
              document.body.appendChild(f);
              f.submit();
            }
            """,
            str(rowid),
        )
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2.5)

        applied = await page.evaluate(
            """
            (values) => {
              const done = {};
              Object.entries(values).forEach(([name, value]) => {
                const el = document.querySelector('[name="' + name + '"]');
                if (!el) { done[name] = 'missing'; return; }
                if (el.tagName === 'SELECT') {
                  const opt = Array.from(el.options)
                    .find(o => o.value === String(value) || o.textContent.trim() === String(value));
                  if (!opt) { done[name] = 'no option'; return; }
                  opt.selected = true;
                } else {
                  el.value = value;
                }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                done[name] = 'ok';
              });
              return done;
            }
            """,
            values,
        )

        await asyncio.sleep(0.5)
        clicked = await page.evaluate(
            """
            () => {
              const btns = Array.from(document.querySelectorAll('button,input[type=button],a'))
                .filter(el => el.offsetParent);
              const hit = btns.find(b => (b.value || b.innerText || '').trim() === '储存');
              if (!hit) return null;
              hit.click();
              return '储存';
            }
            """
        )
        await asyncio.sleep(6)
        notice = await page.evaluate(
            """
            () => {
              const el = document.querySelector('[class*=ajax-error], [class*=error-msg], [class*=notice]');
              return el && el.offsetParent ? (el.innerText || '').trim().slice(0, 200) : '';
            }
            """
        )
        # 字段名不存在或没有对应选项时，页面照样会提交成功——必须自己判失败
        rejected = {
            name: state
            for name, state in applied.items()
            if state in {"missing", "no option"}
        }
        return {
            "success": clicked is not None and not notice and not rejected,
            "rowid": str(rowid),
            "filled": applied,
            "rejected": rejected,
            "notice": notice,
        }

    async def head_url(self, url: str) -> int:
        """探测图片地址是否可访问，返回 HTTP 状态（不可达返回 0）。"""

        page = await self._ensure_page()
        try:
            response = await page.context.request.head(url, timeout=30_000)
            return response.status
        except Exception:  # noqa: BLE001 - 任何网络异常都按不可达处理
            return 0

    async def upload_photo(self, path: Path) -> dict[str, Any]:
        """上传产品图片，返回数据表 gallery 字段应填的文件名与 CDN 地址。"""

        page = await self._ensure_page()
        response = await page.context.request.post(
            f"{self.base_url}/app/soldeazy/datasheet_photo/upload_photos",
            multipart={
                "photofile": {
                    "name": path.name,
                    "mimeType": "image/jpeg",
                    "buffer": path.read_bytes(),
                }
            },
            timeout=180_000,
        )
        text = await response.text()
        match = re.search(r"upload_res\s*=\s*(\{.*?\})\s*;", text, re.S)
        if not match:
            raise SoldeazyError(
                "SOLDEAZY_PHOTO_UPLOAD_FAILED",
                "图片上传没有返回 upload_res",
                details={"head": text[:200]},
            )
        body = json.loads(match.group(1))
        return {
            "success": True,
            "file_name": body.get("file_name"),
            "real_name": body.get("real_name"),
            "gallery_type": body.get("gallery_type"),
        }

    async def read_datasheet_values(
        self,
        site: str = "US",
        query: str = "",
        *,
        search: bool = True,
        only_listed: bool = True,
        allow_batches: bool = False,
        respond_only: bool = False,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """搜索并导出一次，返回 ``{(母体rowid, 子体SKU): {qty, price, parent, ...}}``。

        子体行没有 Row ID，按"最近一个带 rowid 的行"归属。

        **搜索只负责给导出设筛选**（导出的 whole_result 带当前搜索条件），
        现值一律以导出为准——搜索页单页上限 200 行、导出单次上限
        ``PLATFORM_BULK_LIMIT`` 个数据表，是两个不同的截断点。

        所以导出后做完整性自检，三种情况都不许当成"现值"往下走：

        1. 搜索报的总数 > ``PLATFORM_BULK_LIMIT`` → 导出必然被截断，
           先看数再导。``allow_batches=False`` 时直接拒；
        2. 导出里的数据表数 < 搜索报的总数 → 已经截断了，拒；
        3. 总数恰好等于上限 → 无法区分"正好这么多"和"被截断"，只在
           ``last_export_warnings`` 里留痕（不阻断，否则刚好这个规模的账号永远改不了）。

        ``allow_batches=True`` 时，情况 1 不再直接拒，而是**按序号区间分几批导出**
        再合并（见 :func:`bulk_batches`）。每一批仍是原来那一套动作，只是重复了
        几次——不做任何跨批协调，也不改合并后的语义。
        默认 ``False``：``reconcile`` 这种"读现值做对账"的调用不该悄悄变慢几倍。

        2026-11-17 取证：某店铺搜索命中 76,096 个数据表，whole_result 只回
        上限那么多，静默漏 96%。仓库历史导出里有 6 份精确停在上限值。
        """

        from soldeazy_mcp_server.providers.soldeazy_workbook import read_matrix

        expected = 0
        if search:
            found = await self.search(query, site, only_listed=only_listed)
            expected = int(found.get("total_records") or 0)
        else:
            # 不重新搜索，但要沿用上一条 search 记下的命中数：
            # 否则 expected 恒为 0，"先看数再导"和"导出后核对条数"一起静默失效。
            # 2026-11-18 实机：全站 10,450 个数据表、search=False 时防护完全没触发，
            # 导出照样跑了（本该被拦下）。reconcile 正是用 search=False 调的。
            expected = int(self._last_total_records or 0)

        if expected > PLATFORM_BULK_LIMIT and allow_batches:
            return await self._read_in_batches(
                expected, site, query, only_listed=only_listed
            )

        # 只要值时**不导出 xlsx**：搜索响应（检视=所有）里已有 price / qty，
        # 按表头取值即可。对账走这条路 —— 它每轮都要读现值，导 xlsx 纯属浪费
        # （实测：导出 12~36 秒 / 242KB，而读响应只需几秒且不落盘）。
        if respond_only:
            return await self._read_response_values(
                site, query, only_listed=only_listed
            )

        # 先看数再导：超过平台上限时导出注定残缺，省掉一次 180 秒的大请求
        _claim_too_large(
            expected, site, query, (self.active_shop or {}).get("name", "")
        )

        workdir = self.settings.data_dir / "reconcile"
        workdir.mkdir(parents=True, exist_ok=True)
        # 每次读取都会落一份导出文件，长期不清理会无限增长（实测已堆积 16 份、
        # 最大单份 248KB）。这里在写入新文件前先清掉旧的——但要**保留最近几份**：
        # ``last_export_path`` 会被"生成核对文件"复用，删掉正在用的那份会让
        # 人工核对在无声无息中失效。
        _prune_old_exports(workdir, keep=KEEP_EXPORT_FILES)
        target = workdir / f"state_{int(asyncio.get_running_loop().time())}.xlsx"
        await self.export_via_api(target)
        # 记下来：生成人工核对文件时要用**同一份**导出，
        # 否则"人看到的"和"上传的"可能是两次不同时刻的数据。
        self.last_export_path = target

        _sheet, matrix = read_matrix(target)
        values, datasheet_ids, shops = _read_export_rows(matrix)
        self.last_export_shops = shops
        self.last_export_warnings = _assert_export_complete(expected, len(datasheet_ids))
        if not expected:
            # 没有上一条搜索的命中数 → 这一份导出到底全不全，无法判断。
            # 不阻断（可能本来就只是小范围读取），但必须让人看见"这次没核对过"。
            self.last_export_warnings.append(
                "没有可用的搜索命中数，无法核对这份导出是否被"
                f"{PLATFORM_BULK_LIMIT} 上限截断；如需确认请先 search 一次再读"
            )
        self.last_export_total = expected
        return values

    async def _read_response_values(
        self,
        site: str,
        query: str,
        *,
        only_listed: bool,
        max_pages: int = 50,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """只读搜索响应取现值，**不导出 xlsx**。

        给对账这类"只想知道值是多少"的场景用：
        响应（检视=所有）里已有 price / qty，按表头取值就行，
        不必落 242KB 文件、也不必 openpyxl 解析。

        单页 200 行，逐页取到拿全为止；某页没带来新数据表就停。
        """

        values: dict[tuple[str, str], dict[str, Any]] = {}
        datasheets: set[str] = set()
        total_records = 0

        for page_no in range(1, max_pages + 1):
            found = await self._search_page(
                query, site, only_listed=only_listed, page=page_no
            )
            if page_no == 1:
                total_records = int(found.get("total_records") or 0)
                self._last_total_records = total_records
            records = found.get("rows") or []
            if not records:
                break
            before = len(datasheets)
            for record in records:
                rowid = str(record.get("rowid") or "")
                sku = str(record.get("sku") or "")
                if not rowid or not sku:
                    continue
                datasheets.add(rowid)
                values[(rowid, sku)] = {
                    "rowid": rowid,
                    "parent": str(record.get("parent") or ""),
                    "sku": sku,
                    "qty": record.get("qty"),
                    "price": record.get("price"),
                    "postage": record.get("postage"),
                }
            if len(datasheets) == before:
                break
            if total_records and len(datasheets) >= total_records:
                break

        self.last_export_total = total_records
        self.last_export_warnings = []
        return values

    async def _search_page(
        self,
        query: str,
        site: str,
        *,
        only_listed: bool,
        page: int,
    ) -> dict[str, Any]:
        """取搜索结果第 ``page`` 页（第 1 页复用 ``search`` 的完整逻辑）。"""

        shop = (self.active_shop or {}).get("name") or ""
        if page == 1:
            return await self.search(query, site, shop=shop, only_listed=only_listed)

        shop_id = str((self.active_shop or {}).get("id") or "")
        pairs = [
            ("second_mode", "dsheet_list"),
            ("txtproductcode", query or ""),
            ("listshop[]", shop_id),
            ("listsite", self._last_site_code or ""),
            ("listactive", "All" if only_listed else ""),
            ("listdate", ""),
            ("listformat", self._last_listing_format or ""),
            ("view_profile_id", "Default"),
            ("selsetprofile", INSPECTION_ALL_COLUMNS),
            ("page", str(page)),
            ("limit", "200"),
            ("table_action", "show"),
            ("mode", "dsheet_list"),
        ]
        html = ""
        async with self._http_client() as client:
            for url in (
                f"{self.base_url}/app/soldeazy/datasheet",
                f"{self.base_url}/app/soldeazy/datasheet/dsheet_list",
            ):
                posted = await client.post(
                    url,
                    content=urlencode(pairs).encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if posted.status_code == 200 and re.search(
                    r"<table\b[^>]*id=[\"']tbl_dSheet[\"']", posted.text, re.I
                ):
                    html = posted.text
                    break
        if not html:
            return {"rows": [], "total_records": 0, "columns": []}

        self._last_sql_token = extract_sql_token(html)
        matrix = parse_result_table(html)
        if not matrix:
            return {
                "rows": [],
                "total_records": extract_total_records(html),
                "columns": [],
            }
        header, records = parse_rows(matrix, expand_variants=True)
        return {
            "site": site,
            "shop": shop,
            "search": query,
            "columns": header,
            "row_count": len(records),
            "rows": records,
            "total_records": extract_total_records(html),
        }

    async def _read_in_batches(
        self,
        expected: int,
        site: str,
        query: str,
        *,
        only_listed: bool,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """按序号区间分几批导出并合并。

        这就是"连续执行多次同样的导出"：每批一次 ``export_via_api``，
        各自校验条数，最后把字典合并。**没有跨批协调，也没有新语义**。

        合并的合法性来自三点（都已实测）：

        - 区间是无重叠、无遗漏的双射（``bulk_batches`` 保证，有测试钉住）；
        - 每批的行集互不重叠（按数据表切段，数据表不会跨批）；
        - 导出的 (rowid, sku) 是唯一定位，重复即同一行。

        合并后**仍要核对总数**：各批加起来的子体行数未必等于 ``expected``
        （每个数据表可能有多个子体行），所以核对的是**数据表数**。

        ``last_export_path`` 指向最后一批——那是 ``review_files`` 会读的文件，
        所以批模式下调用方不能只依赖它生成核对文件（多批各有一份）。
        """

        from soldeazy_mcp_server.providers.soldeazy_workbook import read_matrix

        ranges = bulk_batches(expected)
        workdir = self.settings.data_dir / "reconcile"
        workdir.mkdir(parents=True, exist_ok=True)

        merged: dict[tuple[str, str], dict[str, Any]] = {}
        datasheets: set[str] = set()
        paths: list[Path] = []
        warnings: list[str] = []

        for index, (start, end) in enumerate(ranges, 1):
            # **分批时不能清理旧导出**：每批的 xlsx 都要留着（人工核对要按批
            # 打开，且 `last_export_batch_paths` 会引用它们）。实测（2026-11-18）
            # 7 批 × 每批先清一次，把只保留最近 5 份的策略变成了"删掉第 1 批"，
            # 核对文件当场失效。单批路径仍照旧清理，不改变原有行为。
            if index == 1:
                _prune_old_exports(workdir, keep=KEEP_EXPORT_FILES + len(ranges))
            stamp = int(asyncio.get_running_loop().time())
            target = workdir / f"state_{stamp}_b{index}.xlsx"
            await self.export_via_api(target, bulk_from=start, bulk_to=end)
            _sheet, matrix = read_matrix(target)
            values, ids, shops = _read_export_rows(matrix)

            # 每批自校验：这一段应当导出 end-start+1 个数据表（末批可能不足）
            want = min(end, expected) - start + 1
            if len(ids) < want:
                raise SoldeazyError(
                    "SOLDEAZY_RESULT_TRUNCATED",
                    f"第 {index}/{len(ranges)} 批（序号 {start}..{end}）"
                    f"只导到 {len(ids)} 个数据表，应为 {want} 个。"
                    "分批导出不完整，拒绝用残缺数据当现值。",
                    details={
                        "batch": index,
                        "batches": len(ranges),
                        "range": [start, end],
                        "expected_datasheets": want,
                        "exported_datasheets": len(ids),
                    },
                )

            merged.update(values)
            datasheets.update(ids)
            paths.append(target)
            if not self.last_export_shops and shops:
                self.last_export_shops = shops

        self.last_export_path = paths[-1] if paths else None
        self.last_export_batch_paths = paths
        self.last_export_total = expected
        self.last_export_warnings = warnings

        if len(datasheets) < expected:
            raise SoldeazyError(
                "SOLDEAZY_RESULT_TRUNCATED",
                f"分批导出合计 {len(datasheets)} 个数据表，应为 {expected} 个。"
                "区间之间可能有重叠或缺口，拒绝用残缺数据当现值。",
                details={
                    "batches": len(ranges),
                    "expected_datasheets": expected,
                    "exported_datasheets": len(datasheets),
                },
            )

        return merged

    def _job_owner_keys(self) -> set[str]:
        """从登录用户名推导可能出现在工序任务里的归属标识。

        实测：登录名形如 ``company_team`` 时，对应任务是
        ``queue_job_createby="company"`` + ``queue_job_user_nick="team"``
        —— 正好是登录名按 ``_`` 拆开的两段。

        所以这里把登录名本身和它的各段都当作候选 key。不硬编码任何具体账号，
        换账号也能用；账号格式变了只是匹配不上，会走下面的回退逻辑。
        """

        raw = (self.settings.soldeazy_username or "").strip()
        if not raw:
            return set()
        keys = {raw, *raw.split("_")}
        return {key.upper() for key in keys if key}

    def _owns_job(self, job: dict[str, Any]) -> bool:
        """判断某条工序任务是不是当前登录账号发起的。"""

        keys = self._job_owner_keys()
        if not keys:
            return True  # 没配用户名，无从判断，不拦
        for field in ("queue_job_user_nick", "queue_job_createby"):
            value = str(job.get(field) or "").strip().upper()
            if value and value in keys:
                return True
        return False

    async def queue_jobs(
        self, *, limit: int = 20, only_mine: bool = True
    ) -> list[dict[str, Any]]:
        """读工序管理任务列表（``queue-job/ajax/get-queue-jobs``）。

        **默认只返回当前登录账号的任务。** 请求本身不带任何过滤（``form={}``），
        返回的是整个账号下所有运营的队列。多账号并行时"新出现的任务"很可能是
        别人的——拿别人的 ``total_items`` 和我们的计划比，就会得出"同步条数不一致"
        这种假问题（2026-11-17 用户反馈的四个原因里最可能的那个）。

        **保险**：如果按归属过滤后一条都不剩，但原始列表非空，说明我们的归属
        判断规则对这个账号不成立 —— 这时**返回全部并留痕**，而不是返回空列表。
        返回空列表会让 ``_await_latest_job`` 永远等不到任务，把"判断规则不适用"
        升级成"整个同步链路失效"，那比抓错任务更糟。
        """

        url = f"{self.base_url}/app/soldeazy/queue-job/ajax/get-queue-jobs"
        # 有浏览器时先落到工序页（页面上下文才带得到同源 cookie）
        _status, text = await self._request_text(
            "post",
            url,
            form={},
            timeout=60_000,
            page_path="/app/soldeazy/queue-job",
            login_hint=(
                "cookie 已失效（工序查询被重定向到登录页）。先调 soldeazy_open_login 重新登录。"
            ),
        )
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise SoldeazyError(
                "SOLDEAZY_QUEUE_BAD_RESPONSE",
                "工序管理列表不是 JSON",
                details={"head": text[:160]},
            ) from exc
        jobs = (payload.get("body") or {}).get("queue_jobs") or []

        if not only_mine or not self._job_owner_keys():
            return jobs[:limit]

        mine = [job for job in jobs if self._owns_job(job)]
        if not mine and jobs:
            self.last_queue_ownership_warning = (
                f"工序队列里 {len(jobs)} 个任务的归属都匹配不上登录账号"
                f"（{self.settings.soldeazy_username!r}），已改用全量列表；"
                "条数比对可能包含别人的任务"
            )
            logger.warning(self.last_queue_ownership_warning)
            return jobs[:limit]

        self.last_queue_ownership_warning = ""
        return mine[:limit]

    async def queue_result(self, job_id: str) -> dict[str, Any]:
        """读某个任务的逐行处理结果（``queue/result?job_id=…``，返回 HTML）。"""

        url = f"{self.base_url}/app/soldeazy/queue/result"
        _status, html = await self._request_text(
            "post",
            url,
            params={"job_id": str(job_id)},
            timeout=60_000,
            ensure_page=False,
            login_hint=(
                "cookie 已失效（工序明细被重定向到登录页）。先调 soldeazy_open_login 重新登录。"
            ),
        )

        def pick(pattern: str) -> str:
            match = re.search(pattern, html, re.S)
            return re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""

        rows = []
        for block in re.findall(r"<tr>(.*?)</tr>", html, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", block, re.S)
            if len(cells) < 3:
                continue
            file_name = re.sub(r"<[^>]+>", "", cells[0]).strip()
            rowid = re.sub(r"<[^>]+>", "", cells[1]).strip()
            outcome = re.sub(r"<[^>]+>", "", cells[2]).strip()
            if rowid.isdigit():
                rows.append({"rowid": rowid, "file": file_name, "result": outcome})

        return {
            "job_id": str(job_id),
            "act": pick(r"<div class='job-act-label'>([^<]+)</div>"),
            "processed": pick(r"已处理:</th><td>([^<]+)</td>"),
            "success": pick(r"成功:</th><td>([^<]+)</td>"),
            "failed": pick(r"失败:</th><td>([^<]+)</td>"),
            "rows": rows,
        }

    async def delete_datasheets(self, rowids: Sequence[str]) -> dict[str, Any]:
        """删除数据表（实测：整表单提交，不是 AJAX）。

        关键参数：`table_action=bulk-delete` + `mode=dsheet_datatable`
        + `table_row_ids`。`mode` 必须是 `dsheet_datatable`，
        用搜索的 `dsheet_list` 提交不会生效（也不会报错）。
        """

        ids = [str(rowid).strip() for rowid in rowids if str(rowid).strip()]
        if not ids:
            raise SoldeazyError("SOLDEAZY_NO_ROWID", "没有给出要删除的数据表编号")

        page = await self._ensure_page()
        await self._goto(f"{self.base_url}{LIST_PATH}")
        await asyncio.sleep(1.5)

        # 删除表单必须带着当前搜索上下文提交，先把目标行搜出来
        await page.locator("input[name=txtdsheetrowid]").first.fill(ids[0])
        await page.locator("#btn_search").first.click()
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)

        await page.evaluate("""
        ([ids]) => {
          const form = document.querySelector('form');
          if (!form) return false;
          const put = (id, name, value) => {
            let el = form.querySelector('#' + id);
            if (!el) {
              el = document.createElement('input');
              el.type = 'hidden'; el.id = id; el.name = name;
              form.appendChild(el);
            }
            el.value = value;
          };
          put('table_action', 'table_action', 'bulk-delete');
          put('mode', 'mode', 'dsheet_datatable');
          put('second_mode', 'second_mode', 'dsheet_list');
          put('table_row_ids', 'table_row_ids', ids.join(','));
          form.submit();
          return true;
        }
        """, [ids])
        await asyncio.sleep(6)
        return {"success": True, "requested": ids, "url": page.url}

    async def create_from_listing_spy(
        self,
        item_ids: str,
        shop_idx: str,
        *,
        channel_type: str = "EBAY",
        template_idx: str = "",
        profile_idx: str = "",
    ) -> dict[str, Any]:
        """用刊登编号创建数据表（封存文档里验证过的接口）。

        先过协议门禁 `mode=agree`，再调 `mode=create_datasheet_from_listing_spy`。
        注意：`res_flag=0` 不代表内容是真的，调用方仍需校验返回标题。
        """

        page = await self._ensure_page()
        request = page.context.request
        ajax = f"{self.base_url}/app/soldeazy/datasheet_ajax"

        await request.post(ajax, form={"mode": "agree"}, timeout=60_000)
        response = await request.post(
            ajax,
            form={
                "mode": "create_datasheet_from_listing_spy",
                "channel_type": channel_type,
                "item_ids": item_ids,
                "shop_idx": str(shop_idx),
                "template_idx": template_idx,
                "profile_idx": profile_idx,
            },
            timeout=180_000,
        )
        text = (await response.text()).lstrip("\ufeff").strip()
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise SoldeazyError(
                "SOLDEAZY_CREATE_BAD_RESPONSE",
                "创建接口返回的不是 JSON",
                details={"head": text[:200]},
            ) from exc

        items = payload if isinstance(payload, list) else [payload]
        summary = []
        for item in items:
            dsheet = item.get("res_dsheet") or {}
            summary.append(
                {
                    "item_id": item.get("res_item_id"),
                    "rowid": item.get("res_rowid"),
                    "flag": item.get("res_flag"),
                    "message": item.get("res_message"),
                    "title": dsheet.get("dsheet_titleexternal") or dsheet.get("dsheet_titleinternal"),
                    "site": dsheet.get("dsheet_sitecode"),
                    "product_code": dsheet.get("dsheet_product_code"),
                }
            )
        return {"success": True, "created": len(summary), "items": summary}

    async def upload_via_api(self, path: Path) -> dict[str, Any]:
        """用页面同款接口上传 _done.xlsx（multipart，字段 filePackage）。

        与点页面等价，但省掉 UI 等待，返回服务端的 `handle` 便于后续追踪。
        """

        payload = path.read_bytes()
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        url = f"{self.base_url}{UPLOAD_PATH}/upload"
        status, text = await self._request_text(
            "post",
            url,
            multipart={
                "filePackage": {"name": path.name, "mimeType": mime, "buffer": payload}
            },
            ensure_page=False,
            login_hint=(
                "cookie 已失效（上传被重定向到登录页）。先调 soldeazy_open_login 重新登录。"
            ),
        )
        if status != 200:
            raise SoldeazyError("SOLDEAZY_UPLOAD_FAILED", f"上传接口返回 HTTP {status}")
        body = json.loads(text) if text else {}
        handle, stored = resolve_upload_handle(body, path.name)
        return {
            "success": True,
            "path": str(path),
            "file": path.name,
            "stored_as": stored,
            "handle": handle,
            "response": body,
        }


def _squash_name(name: str) -> str:
    """比较文件名时忽略空白与大小写：服务端会把空格转义成 ``%20``。"""

    return re.sub(r"\s+", "", str(name or "")).lower()


def resolve_upload_handle(body: Any, filename: str) -> tuple[Any, str]:
    """从上传响应里取出 ``(handle, 服务端存下的文件名)``。

    服务端遇到重名会自动改名 ``a.xlsx`` → ``a (1).xlsx``，而 ``handle``
    字典的 key 是**改名后**的名字。直接用本地文件名去查会恒为 ``None``，
    于是明明上传成功、工序也跑完了，核对却在第一步就判失败
    （还会白白跳过工序核对，让"少改"漏过去）。

    按可信度依次尝试：原名 → 去掉 `` (n)`` 后缀的模糊匹配 → 唯一一项。
    """

    handles = body.get("handle") if isinstance(body, dict) else None
    if not isinstance(handles, dict) or not handles:
        return None, ""

    wanted = _squash_name(filename)
    for name, value in handles.items():
        if _squash_name(name) == wanted:
            return value, str(name)

    stem = _squash_name(Path(wanted).stem)
    suffix = Path(wanted).suffix
    for name, value in handles.items():
        candidate = _squash_name(name)
        if not candidate.endswith(suffix):
            continue
        # 服务端改名规则是 "<原名> (1)<后缀>"，比对时把 " (n)" 摘掉
        bare = re.sub(r"\(\d+\)$", "", Path(candidate).stem)
        if bare == stem:
            return value, str(name)

    if len(handles) == 1:
        name, value = next(iter(handles.items()))
        return value, str(name)
    return None, ""

# ---------- 纯解析（可单测） ----------
def resolve_columns(header: Sequence[str]) -> dict[str, int]:
    """Map logical field names to column indexes using the table header.

    真机表头会把文字折行（`数据表\n编号`），所以比对前先把空白全部去掉。
    """

    def squash(value: object) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    normalized = [squash(cell) for cell in header]
    mapping: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        wanted = {squash(alias) for alias in aliases}
        for index, cell in enumerate(normalized):
            if cell in wanted:
                mapping[field] = index
                break
    return mapping


def _number(text: str) -> float | None:
    """解析金额。``0;8`` 这种带分号的取分号前那一段（运费列常见格式）。"""

    raw = (text or "").strip()
    for splitter in (";", "；"):
        if splitter in raw:
            raw = raw.split(splitter, 1)[0]
            break
    cleaned = raw.strip().replace(",", "").replace("£", "").replace("€", "")
    if not cleaned or cleaned in {"-", "--"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _row_record(
    *,
    rowid: str,
    shop: str,
    site: str,
    title: str,
    sku_lines: list[str],
    price_lines: list[str],
    postage_lines: list[str],
    index: int,
    qty_lines: list[str] | None = None,
) -> dict[str, Any]:
    """按变体下标取一格记录。

    运费格通常只有一行（整表共用），所以下标越界时退回第一行；
    SKU / 价格越界就给空——那是"这一行没有这个变体"，不能编一个值出来。
    """

    if index < len(price_lines):
        price = _number(price_lines[index])
    else:
        price = None
    if index < len(postage_lines):
        postage = _number(postage_lines[index])
    elif postage_lines:
        postage = _number(postage_lines[0])
    else:
        postage = None
    # 数量来自搜索响应（检视=所有时才有这一列）；没有就为 None。
    qty = None
    if qty_lines:
        qty = _number(qty_lines[index]) if index < len(qty_lines) else _number(qty_lines[0])
    return {
        "rowid": rowid,
        "shop": " ".join(shop.split()),
        "site": site,
        "title": title,
        "sku": sku_lines[index] if index < len(sku_lines) else "",
        "price": price,
        "postage": postage,
        "qty": qty,
        "variants": len(sku_lines),
        "variant_index": index,
    }


def _header_offset(header: Sequence[str], cells: Sequence[str]) -> int:
    """算「表头下标 → 数据下标」的偏移。

    实机（2026-11-18，检视=所有）：表头 20 格、数据 21 格，多出的那格对应
    表头里的**空名列**（变体数），于是从那里起数据整体右移一格。
    默认检视（14 格表头 / 16 格数据）则右移两格。

    所以偏移就是 ``len(cells) - len(header)``，按行算（每行格数可能不同）。
    算不出来（数据比表头少）就返回 0，走内容锚定兜底。
    """

    shift = len(cells) - len(header)
    return shift if shift > 0 else 0


def _pick_stacked(
    cells: Sequence[str], index: int | None
) -> list[str]:
    """取某一列的多行堆叠值（变体在同一个格里用换行分隔）。"""

    if index is None or index < 0 or index >= len(cells):
        return []
    raw = cells[index] or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _rows_from_header(
    cells: Sequence[str], header: Sequence[str]
) -> tuple[list[str], list[str], list[str], list[str]] | None:
    """按表头定位取 (sku, price, qty, postage) 的堆叠值；认不出来返回 None。

    这是「摆脱导出」的关键：搜索结果响应里本来就有价格和数量
    （检视=所有时），按表头名字取就能拿到，不必导 xlsx。
    """

    mapping = resolve_columns(header)
    if not {"sku", "price"} <= set(mapping):
        return None

    shift = _header_offset(header, cells)

    def at(key: str) -> int | None:
        index = mapping.get(key)
        return None if index is None else index + shift

    return (
        _pick_stacked(cells, at("sku")),
        _pick_stacked(cells, at("price")),
        _pick_stacked(cells, at("qty")),
        _pick_stacked(cells, at("postage")),
    )


def _records_for_row(
    cells: Sequence[str],
    *,
    site_names: set[str],
    has_shop_column: bool,
    expand_variants: bool,
    header: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """把一行单元格转成 1..N 条记录（多变体时 N > 1）。"""

    if len(cells) < 8:            # 变体展开行
        return []
    rowid = _find_rowid(cells)
    if not rowid:
        return []

    site, site_index = _find_site(cells, site_names)
    shop = _find_shop(cells, site_index, has_shop_column)

    # 优先按表头定位：搜索响应里就有价格/数量，不必导出 xlsx。
    # 表头认不出来才退回旧的负下标（那条路在「检视=所有」下会取错列）。
    from_header = _rows_from_header(cells, header) if header else None
    if from_header is not None:
        sku_lines, price_lines, qty_lines, postage_lines = from_header
    else:
        sku_lines, price_lines, postage_lines = _split_stacked_columns(cells)
        qty_lines = []

    def build(index: int) -> dict[str, Any]:
        return _row_record(
            rowid=rowid,
            shop=shop,
            site=site,
            title=cells[7] if len(cells) > 7 else "",
            sku_lines=sku_lines,
            price_lines=price_lines,
            postage_lines=postage_lines,
            index=index,
            qty_lines=qty_lines,
        )

    if expand_variants and len(sku_lines) > 1:
        return [build(index) for index in range(len(sku_lines))]

    record = build(0)
    if not expand_variants:
        record.pop("variant_index")
    return [record]


def parse_rows(
    rows: Sequence[Sequence[str]], *, expand_variants: bool = False
) -> tuple[list[str], list[dict[str, Any]]]:
    """把 `#tbl_dSheet` 的单元格矩阵解析成数据表记录。

    真机实测的坑（2026-11-02）：表头 14 个 `th`，数据行却是 15~16 个 `td`
    （多出勾选框、重复标题格），**按表头下标取列会整体串位**；筛选前后列数还会变。

    所以取列分两级，**优先用表头定位，表头认不出来才退到内容锚定**：

    1. **表头定位**（首选）：``COLUMN_ALIASES`` 里有 ``渠道帐号`` / ``站点`` 等别名，
       用 ``resolve_columns`` 把逻辑列名映射到真实下标。页面加一列也不影响。
    2. **内容锚定**（兜底）：站点按内容识别（US / UK / Germany…），
       渠道账号取站点**左边那一格**。

    第 2 级里"站点左邻是店铺"是**位置假设**，只在表头缺失时才会用到；
    2026-11-18 之前它是唯一路径，页面一改动就会把店铺读成别的列，
    进而让 `shop_problems` 的店铺校验拿到错名字——可能误判成"店铺一致"而放行。

    * 行尾 5 列固定为 `SKU / 价格 / 运费 / 促销刊登 / eBay促销`，用负下标取；
    * 数据表编号取前 6 格里第一个 6 位以上数字。

    ``expand_variants=True`` 时，一个数据表里的多个变体各成一条记录
    （``variant_index`` 从 0 起），而不是只留第一个。**这点很要紧**：
    SKU 格和价格格都是多行堆叠，只取第一行会让同一数据表里其余 SKU 凭空消失
    ——2026-11-13 控价就因此把 ``ZMDC0063``（第 5 个变体）误判成"线上找不到"。
    默认 False 保持旧口径（每数据表一条 + ``variants`` 计数），不打扰既有调用方。

    单行的解析在 ``_records_for_row``，单格的取值在 ``_row_record``。
    """

    def tidy(value: object) -> str:
        # 只压缩空格/制表符，保留换行——多属性行的变体是靠换行分隔的
        return re.sub(r"[ \t]+", " ", str(value or "")).strip()

    cleaned = [[tidy(cell) for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return [], []

    header = cleaned[0]
    site_names = {name.upper() for name in SITE_LABELS.values()} | set(SITE_LABELS)

    # 表头既用来确认语义列存在，也用来**按名字定位**价格/数量。
    # 搜索响应（检视=所有）里本来就有这两列，所以不必导出 xlsx ——
    # 这是「查询和对账摆脱导出依赖」的关键。
    #
    # 注意数据行可能比表头多几格（默认检视 +2、检视=所有 +1），
    # 偏移在 `_rows_from_header` 里按行算。
    has_shop_column = "shop" in resolve_columns(header)

    records: list[dict[str, Any]] = []
    for cells in cleaned[1:]:
        records.extend(
            _records_for_row(
                cells,
                site_names=site_names,
                has_shop_column=has_shop_column,
                expand_variants=expand_variants,
                header=header,
            )
        )
    return header, records


def _parse_shop_options_nonempty(html: str) -> list[dict[str, str]]:
    """``parse_shop_options`` + 去掉空 id 的项（"全部"那种占位不算渠道账号）。"""

    return [option for option in parse_shop_options(html) if option.get("id")]


def _count_datasheets(records: Sequence[dict[str, Any]]) -> int:
    """记录里涉及多少个**数据表**（去重 rowid）。

    变体展开后 ``row_count`` 是行数，一个数据表可能占多行。
    两个口径都要给出：行数回答"有几条变体"，
    数据表数才回答"要改几个表"（批量上限按后者算）。
    """

    return len({str(record.get("rowid") or "") for record in records if record.get("rowid")})


def _find_rowid(cells: Sequence[str]) -> str:
    """数据表编号：前 6 格里第一个 6 位以上纯数字。"""

    for cell in cells[:6]:
        if cell.isdigit() and len(cell) >= 6:
            return cell
    return ""


def _find_site(cells: Sequence[str], site_names: set[str]) -> tuple[str, int]:
    """按内容识别站点，返回 ``(站点原文, 列下标)``；找不到给 ``("", -1)``。

    不依赖列序：在中间几列里找长得像站点名的 token。
    """

    for index in range(4, min(9, len(cells))):
        token = cells[index].strip().upper()
        if token and (token in site_names or token in SITE_CODES):
            return cells[index].strip(), index
    return "", -1


def _find_shop(cells: Sequence[str], site_index: int, has_shop_column: bool) -> str:
    """取渠道账号。

    **只在表头确认存在「渠道帐号」列时**才用"站点左邻"这个位置假设。
    位置假设在页面加一列时会静默取错，而店铺名会喂给 `shop_problems` 的
    "有没有切账号"校验 —— 取错就可能误判成"店铺一致"而放行，
    把改动写到别的店铺去。所以表头里没有这一列时，宁可不给店铺名。
    """

    if not has_shop_column:
        return ""
    if site_index > 0:
        return cells[site_index - 1]
    return cells[5] if len(cells) > 5 else ""


def _split_stacked_columns(
    cells: Sequence[str],
) -> tuple[list[str], list[str], list[str]]:
    """拆出行尾 5 列里**多行堆叠**的 SKU / 价格 / 运费。

    一个数据表的多个变体在这三列里是按换行堆叠的，所以要按行拆开对齐。
    """

    def lines(offset: int) -> list[str]:
        raw = cells[-offset] if len(cells) >= offset else ""
        return [line.strip() for line in raw.splitlines() if line.strip()]

    return lines(5), lines(4), lines(3)


def _read_export_rows(
    matrix: Sequence[Sequence[Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[str], list[str]]:
    """把导出矩阵读成 ``{(母体rowid, 子体SKU): {...}}``。

    子体行没有 Row ID，按"最近一个带 rowid 的行"归属；母体 SKU 也来自那一行。

    返回 ``(现值, 数据表集合, 导出里出现的店铺)``。
    """

    from soldeazy_mcp_server.providers.soldeazy_workbook import header_row_count

    header = header_row_count(matrix)
    values: dict[tuple[str, str], dict[str, Any]] = {}
    shops = sorted(
        {
            str(row[1] or "").strip()
            for row in matrix[header:]
            if len(row) > 1 and str(row[1] or "").strip()
        }
    )

    current = ""
    parent = ""
    datasheet_ids: set[str] = set()
    for row in matrix[header:]:
        rowid = str(row[0] or "").strip()
        psku = str(row[2] or "").strip()
        sku = str(row[3] or "").strip()
        if rowid:
            current = rowid
            parent = psku
            datasheet_ids.add(rowid)
        if not sku:
            continue
        values[(current, sku)] = {
            "rowid": rowid,
            "parent": parent,
            "sku": sku,
            "qty": row[4],
            "price": row[5],
        }
    return values, datasheet_ids, shops


def _claim_too_large(expected: int, site: str, query: str, shop: str) -> None:
    """超过平台单次批量上限时直接拒：导出注定残缺，省掉一次大请求。"""

    if expected <= PLATFORM_BULK_LIMIT:
        return
    raise SoldeazyError(
        "SOLDEAZY_RANGE_TOO_LARGE",
        f"这次筛选命中 {expected} 个数据表，超过平台单次批量上限 "
        f"{PLATFORM_BULK_LIMIT} 个（导出/删除/同步都受此限制）。"
        "范围太大，拒绝用残缺数据当现值；请加店铺 / 站点 / SKU 筛选"
        f"缩小到 {PLATFORM_BULK_LIMIT} 个以内再试。",
        details={
            "total_records": expected,
            "platform_bulk_limit": PLATFORM_BULK_LIMIT,
            "excess": expected - PLATFORM_BULK_LIMIT,
            "site": site,
            "shop": shop,
            "search": query,
        },
    )


def _assert_export_complete(expected: int, exported: int) -> list[str]:
    """导出完整性自检。

    导出里的数据表数 < 搜索报的总数 → 已经截断了，拒。
    总数恰好等于上限 → 无法区分"正好这么多"和"被截断"，只留痕不阻断
    （否则刚好这个规模的账号永远改不了）。

    返回留痕用的 warning 列表。
    """

    if expected and exported < expected:
        raise SoldeazyError(
            "SOLDEAZY_RESULT_TRUNCATED",
            f"搜索命中 {expected} 个数据表，导出只拿到 {exported} 个"
            f"（平台单次导出上限 {PLATFORM_BULK_LIMIT} 个）。范围太大，"
            "拒绝用残缺数据当现值；请指定店铺/站点/SKU 缩小范围后重试。",
            details={
                "expected_datasheets": expected,
                "exported_datasheets": exported,
                "platform_bulk_limit": PLATFORM_BULK_LIMIT,
            },
        )

    if expected == PLATFORM_BULK_LIMIT and exported == PLATFORM_BULK_LIMIT:
        return [
            f"本次导出 {exported} 个数据表，恰好等于平台上限 "
            f"{PLATFORM_BULK_LIMIT}：无法区分「本来就有这么多」和「被截断」，"
            "如需确认请缩小范围再核对一次"
        ]
    return []
