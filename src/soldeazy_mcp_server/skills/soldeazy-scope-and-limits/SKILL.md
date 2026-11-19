---
name: soldeazy-scope-and-limits
description: Understand Soldeazy platform limits (3000 datasheets per batch, 200 rows per search page) and how to split a large change. Use when a search or plan hits the bulk limit or returns truncated rows.
---

# 范围与平台上限

## 两个不同的截断点，别混

| 限制 | 数值 | 作用在哪 |
|---|---|---|
| 导出/删除/同步批量上限 | **3000 个数据表** | 一次批量动作最多处理这么多 |
| 搜索单页行数 | **200 行** | 页面一次渲染这么多行 |

**一行 ≠ 一个数据表**：一个多变体数据表占多行。实测某店铺是
1636 个数据表 / 7075 行，平均 4.3 行/表。

## 超 3000 时会怎样

超过不会报错，只是**静默少做一部分**——这是最危险的失败形态：
工序显示成功，对账才发现一堆"查不到"。

实测取证（2026-11-17）：某店铺搜索命中 76,096 个数据表，
whole_result 导出只回 3,000 个，**漏 96%**。

所以工具会主动拦：

| 位置 | 行为 |
|---|---|
| `search` | 返回 `bulk_limit_exceeded` / `bulk_limit_excess` / `bulk_limit_notice` |
| 读现值 | 总数 > 3000 时**先看数再导**，直接报 `SOLDEAZY_RANGE_TOO_LARGE`（省掉一次 180 秒大请求） |
| 上传前 | 计划涉及 > 3000 个数据表时**中止**，`ok=false` 且"未上传" |

恰好等于 3000 不阻断，只在 `warnings` 里留痕（无法区分"本来就有这么多"和"被截断"）。

## 看到 RANGE_TOO_LARGE 怎么办

**不要放弃，也不要猜。** 按下面的顺序缩小：

### 1. 先看命中数

```
soldeazy_search_datasheet(skus=[...], site="", include_unlisted=true)
```

返回的 `total_records` 是命中数据表数。若 > 3000，需要拆分。

### 2. 按站点拆（最自然）

```
soldeazy_list_sites()          # 拿到真实站点名
soldeazy_change_datasheets(prices={...}, site="UK")
soldeazy_change_datasheets(prices={...}, site="Germany")
```

注意：实测某些店铺 99.98% 的数据表集中在单一站点
（ANO 的 20,421 个里 20,417 个在 DE）。这种账号按站点拆没用。

### 3. 按店铺拆

```
soldeazy_list_shops()          # 88 个渠道账号
soldeazy_change_datasheets(prices={...}, shop="ACC")
```

实测前 3 个店铺各 7,143 / 14,566 / 20,421 个数据表，**单店就超限**。

### 4. 按 SKU 分批

改价天然就是按 SKU 限定的：一次只改几十个 SKU，
命中的数据表通常远小于 3000。实测 12 个 SKU 只命中 67 个数据表。

**这是最有效的办法**：不要一次把几百个 SKU 全塞进去。

## 平台没有数据接口

实测（2026-11-19，Playwright 监听 15 个请求）：搜索/翻页/切检视
全部是**整页 POST 返回 HTML**，**没有任何 XHR/JSON 接口**。

所以：

- 不要期待"读取字段"的更优解，平台没提供；
- 要值有两条路：读搜索响应（小范围快）、导 xlsx（大范围快）。

体积对照（实测 ANO/DE）：

| 方式 | 体积 | 每行成本 |
|---|---|---|
| 抓 HTML | 10,033 KB / 3,778 行 | 约 3.7 KB/行 |
| 导出 xlsx | 470 KB / 14,826 行 | 约 0.03 KB/行 |

## 不要做的事

- 不要在超限时"先改一部分再说"：那正是静默少改的来源。
- 不要假设按站点拆一定有效：先看分布。
- 不要用 `limit` 调大来拿全搜索结果：单页硬上限 200。
