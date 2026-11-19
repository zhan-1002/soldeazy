---
name: soldeazy-troubleshooting
description: Diagnose common Soldeazy failures — expired login, targets not found, reconcile mismatches, sync counts that do not add up. Use when a Soldeazy call returns ok=false or the numbers look wrong.
---

# 排障

## 先跑 `soldeazy_status`

任何异常先看这个：它报出 profile 目录、登录态、cookie 是否存在、
写入是否启用。很多"搜不到 / 改不了"其实是登录态失效。

## 登录态失效

**症状**：`SOLDEAZY_LOGIN_REQUIRED`，或返回里出现登录页内容。

**做法**：`soldeazy_open_login` 开有头浏览器登录一次。
登录时**勾「记住用户名」**——不勾的话服务端只发会话 cookie，关掉浏览器就没了。
工具已自动勾选，但换机器/换 profile 时要注意。

cookie 落在 `<profile>/session_cookies.json`，重启后可复用。
若 `soldeazy_status` 里 `session_save_error` 非空，说明写盘失败
（profile 目录不可写），这才是"每次都要重新登录"的真因。

## 线上找不到目标（`missing`）

按顺序查：

### 1. 是不是未刊登

`include_unlisted` 默认 `false`，只查已刊登。生产账号里未刊登的数据表
必须传 `include_unlisted=true` 才会被搜到。

### 2. 是不是套装

`prices` 按子体 SKU **精确**匹配：
- 基础 SKU `ZMFL0012` 只命中单件行
- 套装必须显式写 `ZMFL0012 x2`

### 3. 是不是母体/子体搞混

- 改价 → 键是**子体 SKU**
- 改量 → 键是**母体 SKU**

写反了就会"找不到"。

## 对账不通过

`reconciled` 形如 `33/35` 表示 35 条里只有 33 条生效。

看 `mismatched`（值不对）和 `absent`（查不到）分别是什么：

| 情况 | 含义 |
|---|---|
| `mismatched` 有值 | 线上值不等于目标值——可能被平台规则改了（价格下限/类目限制） |
| `absent` 有值 | 该行在读取范围内找不到 |

对账是**轮询到稳定**的：平台异步应用，第一轮不中会再读，最多 3 轮。
连续两轮结果相同才判定"确实没生效"。

## 同步条数对不上

`synced` 的取值要看懂：

| 值 | 含义 |
|---|---|
| `ok` | 真的核对到了同步工序 |
| `unverified` | 同步接口成功，但**工序队列里没出现任务** |
| `partial` | 核对到了，但有失败 |
| `skipped` | 没执行同步 |

`unverified` 在**未刊登数据表**上是正常的：没有在线刊登，平台不产生同步工序。
这时条数以上传为准，不该判失败。

若 `sync_detail` 里 `not_in_job` 有值——说明计划里有、工序里没有，
是"这次同步没覆盖到"，不是失败但必须报出来。

## 值读出来是错的（历史上踩过的坑）

**症状**：价格/数量读到了别的列的值，或数量看起来"为空"。

**真因**：列偏移。搜索页在「检视=所有」时数据行比表头多一格
（表头第 8 列是空名的"变体数"列），按固定下标取会整体错位，
价格会读成运费列的值。

**现状**：已改为按表头名字取值，并把偏移按「数据行格数 − 表头格数」算。
若再次出现，先确认 `parse_rows` 拿到的是不是「检视=所有」的响应。

## 不要做的事

- 不要因为一次 `ok=false` 就重试整批：先看 `problems`，可能只是一行的问题。
- 不要在未确认登录态时就重跑：会白等几十秒再失败。
- 不要把 `warnings` 当失败：`warnings` 是"要知情但没失败"。
