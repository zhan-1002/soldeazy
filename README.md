# Soldeazy MCP Server

Soldeazy 数据表写入链路的独立 MCP 服务：**改价 / 改库存 / 创建 / 删除 / 图片 / 同步 / 工序对账**，
17 个工具 + 4 份内置流程知识。

为什么是独立服务：Soldeazy 走的是浏览器 + 表单接口（不是公开 API），依赖
Playwright 与 openpyxl，挂进别的服务会把这些依赖和工具 schema 一起拖进上下文。

## 它做什么

Soldeazy 没有公开接口文档。生产链路是「导出 xlsx → 离线改 → 上传 → BULK SYNC」
（原影刀应用的流程）。本服务按抓包得到的真实接口复刻，**不点页面按钮**。

一次改动的生命周期：

```
① 预览  读现值 → 逐条算出 old/new → 写 plan.json + review_before/after.xlsx
② 人工  打开 review_after.xlsx 逐行核对（明细只在文件里，不在对话里）
③ 执行  导出子集 → 改 E/F 列 → 上传 → BULK SYNC → 对账
④ 判决  只回 ok / 计数 / problems，明细写文件
```

## 关键设计

### 价格和数量是同一次授权

不是两条链路。上传本来就要整行覆盖（E 列数量 + F 列价格同时写），
线上生效只在同步那一步。所以只有**一个**工具 `soldeazy_change_datasheets`，
`prices` 和 `quantities` 可以只给一个，也可以都给。

### 站点和店铺留空 = 全部范围

平台语义：不勾选 = 全部。所以 `site=""` / `shop=""` 是合法的，
工具会按全部范围执行，并在 `preview.作用范围` 里如实列出命中的账号。

同一 SKU 在多个店铺都有是常态（实测 4 个 SKU 分别在 2~8 个店铺出现），
`prices` 按子体 SKU 精确匹配，这些行**会一起改**。

### 只导出命中的子集

实测（单店 / US 站，目标 SKU 散落 33 个数据表）：

| 方式 | 耗时 | 数据表 | 文件 |
|---|---|---|---|
| 空查询 + 导全店 | 36.45s | 1636 | 242 KB |
| 合并搜索 + 导子集 | **5.39s** | 33 | **19 KB** |

`prices` 和 `quantities` 的键会拼成 `*SKU1 *SKU2 ...` 的合并查询，
导出的就是这个子集。**每个词必须带 `*`**，否则平台会静默漏数据表。

### 对账不导出

对账只需要读 price/qty，搜索响应里就有。每轮不落 242KB 文件、
不用 openpyxl 解析，也不重复搜索（搜索参数已在平台上，重发即刷新）。

### 一次调用只用一份原始导出

预览导出的那份在执行时被复用（`_reusable_export`），上传文件基于同一份生成。
复用旧快照的安全性由「上传前只核对目标那几十行」兜底：值变了就报
「快照过期，请重新预览」，不静默覆盖。

## 平台限制

两个**不同**的截断点，别混：

| 限制 | 数值 | 作用 |
|---|---|---|
| 导出/删除/同步批量上限 | **3000 个数据表** | 一次批量动作 |
| 搜索单页行数 | **200 行** | 页面一次渲染 |

**一行 ≠ 一个数据表**：一个多变体数据表占多行（实测平均 4.3 行/表）。

超过 3000 不会报错，只会**静默少做一部分** —— 工序显示成功、对账才发现一堆
"查不到"。所以三道闸都会主动拦：搜索侧提示、读侧先看数再导、上传前中止。

## 内置流程知识（skills）

工具的 docstring 说不完整条链路。4 份 skill 覆盖那些容易判断错的点：

| Skill | 回答什么 |
|---|---|
| `soldeazy-price-change` | 店铺留空能不能改、同一 SKU 跨店铺怎么办、预览看哪些字段 |
| `soldeazy-stock-change` | 母体展开子体、为什么和改价是同一个工具 |
| `soldeazy-scope-and-limits` | 超 3000 怎么办、按什么维度拆 |
| `soldeazy-troubleshooting` | 登录失效、找不到目标、对账不符 |

读取方式二选一：

- MCP 资源：`skill://soldeazy-price-change/SKILL.md`
- 工具（老客户端兼容）：`soldeazy_read_skill(skill_name="soldeazy-price-change")`

## 工具清单（17 个）

| 工具 | 用途 |
|---|---|
| `soldeazy_status` | profile / 凭据 / 写入开关状态，**任何调用前先看它** |
| `soldeazy_open_login` | 有头浏览器登录一次 |
| `soldeazy_search_datasheet` | 读数据表行（价格/数量/运费） |
| `soldeazy_list_sites` / `soldeazy_list_shops` | 站点 / 88 个渠道账号 |
| `soldeazy_switch_shop` | 切换当前渠道账号 |
| `soldeazy_change_datasheets` | **改价 + 改库存，一次上传一次同步** |
| `soldeazy_plan_changes` / `soldeazy_confirm_plan` / `soldeazy_apply_plan` | 分步路径（需要人工确认令牌时用） |
| `soldeazy_reconcile_plan` | 对账 |
| `soldeazy_change_history` | 读审计（跑了什么、结果如何） |
| `soldeazy_stock_template` | 库存模板 |
| `soldeazy_create_datasheet` / `soldeazy_delete_datasheet` | 创建 / 删除 |
| `soldeazy_upload_photo` | 上传图片 |
| `soldeazy_read_skill` | 读流程知识 |

## 安装

```bash
uv sync                       # 或 pip install -e ".[dev]"
uv run playwright install chrome   # 没装 Chrome 时
cp .env.example .env          # 填可选配置
```

## 配置到 MCP 客户端

**最简形式（推荐）**：只写启动命令，其余全部自动推导。

```json
{
  "soldeazy": {
    "type": "stdio",
    "command": ".venv/Scripts/python.exe",
    "args": ["-m", "soldeazy_mcp_server"],
    "env": { "SOLDEAZY_HEADLESS": "1", "SOLDEAZY_WRITE_ENABLED": "1" }
  }
}
```

为什么可以不写路径：

- **数据目录**由包的位置反推（`<repo>/.soldeazy-data`），**不依赖 cwd**
  —— 客户端从 `C:\Windows\System32` 拉起也一样落在仓库内
- **profile 目录**默认就是 `<数据目录>/profile`，不用单独配

`command` 是相对路径时，取**客户端配置文件的所在目录**或**你启动客户端时的工作目录**。
不同客户端解析方式不同，若报"找不到命令"，改成绝对路径即可 ——
这是唯一**必须**手工改的地方，因为它由客户端决定，代码无从推导。

**绝对路径写法**（客户端不支持相对命令时）：

```json
{
  "soldeazy": {
    "type": "stdio",
    "command": "C:/path/to/soldeazy/.venv/Scripts/python.exe",
    "args": ["-m", "soldeazy_mcp_server"],
    "env": { "SOLDEAZY_HEADLESS": "1", "SOLDEAZY_WRITE_ENABLED": "1" }
  }
}
```

`env` 里**不再需要** `SOLDEAZY_PROFILE_DIR`。只有在要把数据放到仓库外时才显式设它：

```json
"env": { "SOLDEAZY_PROFILE_DIR": "D:/soldeazy-data/profile" }
```

那时**必须用绝对路径**：相对路径会按仓库根解析，不是按 cwd。

**不要用 `uv run` 启动**：客户端以管道方式拉起子进程时，uv 初始化自己的缓存
可能失败（`Failed to initialize cache ... 拒绝访问`），服务在 MCP 握手之前就死了，
表现为"装上了但连不上"。直接用解释器跑没有这个问题。

## 安全提醒

- 写工具默认**关闭**（`SOLDEAZY_WRITE_ENABLED=0`），先把读链路跑通，再开写入。
- `.soldeazy-data/` 含登录 cookie，**已在 .gitignore 里**，不要提交。

## 文档

- [`docs/soldeazy-写入链路架构.md`](docs/soldeazy-写入链路架构.md) — 计划生命周期、对账保证、限制取证
- [`docs/soldeazy-创建与图片-接口说明.md`](docs/soldeazy-创建与图片-接口说明.md) — 创建数据表与图片上传接口
- [`docs/soldeazy-验收清单.md`](docs/soldeazy-验收清单.md) — A–F 六组验收项及结果
- [`docs/待确认清单-影刀改价.md`](docs/待确认清单-影刀改价.md) — 与影刀原流程的差异记录

## 测试

```bash
uv run pytest -q
```

覆盖：解析（含真实响应结构）、上限三道闸、计划冻结与哈希、对账、导出复用、
skills 完整性、以及"不能静默失败"专项。
