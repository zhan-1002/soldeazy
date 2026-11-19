"""``SoldeazyBrowser`` 对外暴露的协作接口（Protocol）。

为什么要有这个文件：

``soldeazy_apply`` / ``soldeazy_change`` 里的函数第一参数写成 ``browser: Any``，
于是浏览器依赖完全不受类型检查保护——方法名拼错、参数名改了都只能在运行时炸。
170 处 ``Any`` 里，这一处最值得收口：它决定"执行/对账链路能不能调用到浏览器"。

用 ``Protocol`` 而不是抽象基类：``SoldeazyBrowser`` 不需要继承任何东西，
测试里的假对象也照样能用（结构子类型），零运行时改动。

接口清单是**从实际调用反推**的（2026-11-18），不是拍脑袋列的：

    grep 'browser\\.' + getattr(browser, ...) 得到的 7 个方法 + 5 个属性。

新增依赖时这里必须同步更新，否则类型检查会报错——这本身就是文档。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class DatasheetBrowser(Protocol):
    """执行 / 对账链路需要的最小浏览器能力。

    只管「按已设好的筛选导出、上传、同步、读工序」这一路；
    登录、切换店铺等会话动作不在协议内——那些由工具层直接持有具体实现。
    """

    #: 当前活动渠道账号 ``{id, name}``；没设过就是 None。
    #:
    #: 可写：执行时若计划是"全部店铺"，会把它清成 None，
    #: 保证导出/同步的范围与预览一致（否则会沿用上次切过的店）。
    active_shop: dict[str, str] | None

    #: 最近一次导出里出现的渠道账号名（对账时校验"读的是不是同一个店"）
    last_export_shops: list[str]

    #: 最近一次导出无法判断是否截断时的留痕（例如总数恰好等于平台上限）
    last_export_warnings: list[str]

    #: 工序任务归属判断不成立时的留痕（回退到全量列表）
    last_queue_ownership_warning: str

    #: 最近一次导出落地到哪个文件（生成核对文件要复用同一份）
    last_export_path: Path | None

    #: 本次执行**实际使用**的导出文件。
    #:
    #: 与 ``last_export_path`` 的区别：后者是"最近一次读取现值"的文件，
    #: 而这里记录 ``apply_plan`` 这次到底用了哪一份（可能是复用的，
    #: 也可能是自己新导的 ``<plan_id>.export.xlsx``）。
    #: ``build_upload_file`` 必须基于同一份，不能自己拼路径——
    #: 复用时 ``<plan_id>.export.xlsx`` 根本不存在（2026-11-19 实测崩溃点）。
    last_apply_export: Path | None

    async def search(
        self, sku_terms: str, site: str, shop: str = "", *, only_listed: bool = True
    ) -> dict[str, Any]:
        """按当前店铺/站点/SKU 搜索，返回含 ``total_records`` 的结果。"""
        ...

    async def export_via_api(self, dest: Path) -> dict[str, Any]:
        """把当前搜索条件下的**整个结果集**导出成 xlsx 到 ``dest``。"""
        ...

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
        """读现值：``{(母体rowid, 子体SKU): {qty, price, parent, ...}}``。

        ``respond_only=True`` 时**只读搜索响应、不导出 xlsx** ——
        对账每轮都要读现值，走这条路能省掉 242KB 落盘与 openpyxl 解析。
        """
        ...

    async def upload_via_api(self, path: Path) -> dict[str, Any]:
        """上传改好的 xlsx，返回里带工序 ``handle`` 用于追踪。"""
        ...

    async def sync_up_via_api(
        self,
        *,
        price_postage: bool = True,
        quantity: bool = False,
        row_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """把指定数据表同步到线上，勾选由两个开关决定。"""
        ...

    async def queue_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """读工序任务列表（默认只返回当前账号的）。"""
        ...

    async def queue_result(self, job_id: str) -> dict[str, Any]:
        """读某个工序任务的逐行处理结果。"""
        ...
