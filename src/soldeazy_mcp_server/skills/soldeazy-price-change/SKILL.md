---
name: soldeazy-price-change
description: Change Soldeazy datasheet prices end to end — decide the scope when site/shop are omitted, preview, verify the review file, execute, and confirm. Use when a user asks to update prices for one or more SKUs.
---

# 改价完整流程

## 范围怎么定（**不要反问用户**）

调用方常只给 SKU 和价格，不给站点、不给店铺。这是**合法且常见**的：

- `site` 留空 = **全部站点**（平台语义：不勾选即全部）
- `shop` 留空 = **全部店铺**（平台语义：不选即全部）
- `listing_format` 默认 **`BIN`**（长期在线），**拍卖（`AUC`）不碰**

**这时不要停下来问"改哪个店铺"。** 工具会一次算清范围，并在返回的
`preview.作用范围` 里列出实际命中的账号与站点。

### 刊登方式为什么默认 BIN

页面上「刊登方式」下拉有三个真实选项（从 HTML 原文读到的，未硬编码）：

| value | 含义 |
|---|---|
| `''` | 全部 |
| `AUC` | 拍卖 |
| `BIN` | 长期在线（Buy It Now） |

日常改价/改库存只针对长期在线刊登；拍卖改价没有意义。
所以默认 `BIN`，要包含拍卖时**显式**传 `listing_format=""`（注意空串是合法值，
不是"没传"）。导出会沿用搜索时用过的那个值，不会出现"搜 BIN、导全部"。

### 同一 SKU 在多个店铺都有，怎么办

这是常态，不是异常。实测（2026-11-19，4 个 SKU）：

```
JJWB0003   出现在 3 个 (店铺,站点)
QPCC0003   出现在 2 个
JJEBE003   出现在 7 个
ZMFLS0004  出现在 8 个
```

`prices` 按 **子体 SKU 精确匹配**，所以同一 SKU 在所有店铺/站点的行
**会一起改**。这是"改这个 SKU 的价格"的本来含义。

判断"该不该改这么多"，看返回里的 `preview.作用范围` 与 `preview.概览`：
命中范围与你预期一致就直接往下走；不一致才需要问用户。

## 四个步骤

### 1. 预览（默认就是这一步）

```
soldeazy_change_datasheets(prices={"SKU": 29.9}, dry_run=true)
```

返回里只有**计数**，不给逐条明细（明细在磁盘文件里）：

| 字段 | 含义 |
|---|---|
| `items` | 要改几条 |
| `datasheets` | 涉及几个数据表 |
| `preview.概览` | 按价格/数量分别统计条数、涉及数据表数、涨跌方向 |
| `preview.作用范围` | 实际命中的账号、站点、状态筛选 |
| `preview.逐条样本` | 前 20 条（多了会截断并标注） |
| `must_confirm` | **必须让用户看见的提醒** |
| `review_after` | 变更后的完整文件路径，逐行可读 |

`must_confirm` 里出现这些时，要如实转述给用户：

- **套装/变体**：同母体下还有 `SKU x2` 之类没被点名，不会改
- **⚠ 改动方向单一**：多条全部上调或全部下调，多半是参数写错了
- **⚠ 范围不符**：参数指定账号与导出里实际账号不一致
- **线上找不到**：某些 SKU 没匹配到行

### 2. 让用户核对文件

`review_after.xlsx` 才是判断依据。**不要把明细倒进对话**。

### 3. 执行

```
soldeazy_change_datasheets(prices={"SKU": 29.9}, dry_run=false)
```

同一次调用里完成：导出（只导命中的子集）→ 生成上传文件 → 上传 →
BULK SYNC → 对账。价格与数量一起给也只走一遍。

### 4. 读判决

| 字段 | 含义 |
|---|---|
| `ok` | 全部通过才是 true |
| `uploaded` | 上传是否成功 |
| `synced` | `ok` / `unverified` / `partial` / `skipped` |
| `reconciled` | 例如 `33/33`，读的是线上真实值 |
| `problems` | 失败原因（有条数上限） |
| `warnings` | 不判失败但必须知情的情况 |

`synced: "unverified"` 是**正常**的：没有在线刊登的数据表不会产生同步工序，
这时只记警告、不判失败，条数以上传为准。

## 常见错误码

| 错误 | 含义与做法 |
|---|---|
| `SOLDEAZY_SHOP_REQUIRED` | 计划里没有店铺。传 `shop` 或先 `soldeazy_switch_shop` |
| `SOLDEAZY_PLAN_EMPTY` | `prices` 和 `quantities` 都没给 |
| `SOLDEAZY_RANGE_TOO_LARGE` | 命中超过平台批量上限 3000，见 `soldeazy-scope-and-limits` |
| `SOLDEAZY_LOGIN_REQUIRED` | 登录态失效，调 `soldeazy_open_login` |

## 不要做的事

- 不要为了让 AI 自己判断而对每个 SKU 调一次搜索：改价按子体精确匹配，
  一次调用就能算清全部范围。
- 不要在看到 `items` 很大时自动改小范围：先看 `preview.作用范围` 是否合理。
- 不要跳过预览直接执行。
