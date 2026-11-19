---
name: soldeazy-stock-change
description: Change Soldeazy datasheet quantities by parent SKU, expand to every child row, and understand why it is the same call as a price change. Use when a user asks to update stock or quantity.
---

# 改数量（库存）

## 与改价是同一个工具

`soldeazy_change_datasheets` 同时接受两个参数：

- `prices` = `{子体SKU: 新价格}` —— 按子体**精确**匹配
- `quantities` = `{母体SKU: 新数量}` —— 按母体展开**全部子体行**

两个都给只走一遍：一次导出、一次上传、一次同步。不要拆成两次调用。

## 母体与子体的关系

一个数据表 = 一个母体行 + 它旗下的子体行。实测（FUN/US）：

```
数据表 3117211（5 行）
  C=母体          D=SKU           E=数量   F=价格
  GT-D1-FUN-P     FGS50W110V       100    22.77
  GT-D1-FUN-P     FGS100W110V       50    27.48
  GT-D1-FUN-P     FGS200W110V      100    45.50
  GT-D1-FUN-P     FGS300W110V       50    59.16
  GT-D1-FUN-P     FGS500W110V      100    99.56
```

所以 `quantities={"GT-D1-FUN-P": 60}` 会把这张表里 5 个子体行的数量
**全部写成 60**。这是库存的语义：库存挂在母体上，子体只是不同规格。

### 同一母体的原有数量可能不一致

上面例子里 100/50/100/50/100 各不相同。统一写 60 会把这些差异抹平。
如果用户只想去掉"低于某值"的，那是另一回事——按用户给的数字改，不要自作聪明。

## 搜索是怎么命中的

**搜索按数据表匹配；命中后返回整张表的全部行。**

实测（2026-11-19）：
```
搜母体 *GT-D1-FUN-P  → 命中 1 个数据表 / 返回 5 行（5 个子体全在）
搜子体 *FGS100W110V  → 命中 33 个数据表 / 返回 370 行
```

这意味着：搜母体名就能拿到它旗下全部子体，不需要先读全店再本地展开。

## 数量不做打包换算

`SKU x4` 这种套装行，库存直接写用户给的数字，不乘 4。
依据：库存针对的是**这一行刊登本身的件数**，与它是不是套装无关。

（历史上有过"按最低售价反推件数"的算法，2026-11-09 已作废。）

## 常见错误码

| 错误 | 含义与做法 |
|---|---|
| `SOLDEAZY_PLAN_EMPTY` | `quantities` 为空 |
| `SOLDEAZY_SHOP_REQUIRED` | 计划里没有店铺，传 `shop` 或先 `soldeazy_switch_shop` |
| `SOLDEAZY_RANGE_TOO_LARGE` | 命中超过 3000，见 `soldeazy-scope-and-limits` |

## 不要做的事

- 不要把 `quantities` 的键写成子体 SKU：那是按母体匹配的，写子体匹配不到。
- 不要为了"只改一个子体"而传子体名——工具的语义是母体展开。
  确实要针对单行时用 `prices` 的思路（按子体精确匹配），但数量暂不支持按子体改。
