# 账本对账报告

测量时间：2026-11-15（版本 `v1.2.73`）。数字全部来自生产库实测。

---

## 一、token 总量：分毫不差

近 24 小时，日志侧与账本侧的 token 合计：

| 来源 | prompt_tokens | completion_tokens |
| --- | --- | --- |
| `request_logs` | 1 688 667 258 | 4 617 053 |
| `usage_events` | **1 688 667 258** | **4 617 053** |

**两侧完全一致。**

这是一个比它看起来更有力的证据：入账路径只要漏一条或多记一条，两边的总数就会
分叉。现在没有。

---

## 二、预算预留的三种状态

全量 195 974 条预留：

| 状态 | 数量 | 占比 | 含义 |
| --- | --- | --- | --- |
| `settled` | 190 344 | **97.1%** | 已结清 |
| `released` | 4 147 | **2.1%** | 已释放（请求没消耗） |
| `unknown` | 1 483 | **0.76%** | 无法自动判定 |

`unknown` 的语义是：**上游没有回用量，所以既不能按「已消耗」结清、也不能按「未消耗」
释放**。按设计它**永不自动释放、也不自动归档**，只能人工判定。

---

## 三、1 483 条 unknown 里，哪些是合理的

按请求终态分类：

| 终态 | 数量 | 判断 |
| --- | --- | --- |
| `client_cancelled` | 551 | **合理** —— 客户端断开时上游通常不回用量 |
| `upstream_error` | 468 | **合理** —— 上游报错，多半没走到计费 |
| `transport_error` | 293 | **合理** —— 传输出错 |
| `rejected` | 156 | **可疑** —— 被拒的请求不该消耗预算 |
| `completed` | 8 | **可疑** —— 成功就该有用量 |
| `protocol_incomplete` | 7 | **合理** |

**合理部分合计 1 319 条（89%）**，属于机制固有的不确定性。

---

## 四、两类可疑的，都已经不再产生

### `rejected` 156 条

典型形态：`prompt_too_long` 的 400，却预留了**约 1 790 万 token**。

也就是说请求先预留、后在校验阶段被拒，而预留没有归还。

**按时间看**：

| 日期 | 数量 |
| --- | --- |
| 2026-11-03 | **112** |
| 2026-11-10 | 19 |
| 2026-11-12 | 3 |
| 其它日期 | 各 1–6 |

**11-13 之后为 0。** 最近三天那 3 条全部落在 11-12 21:39–21:40 的同一分钟内。

### `completed` 8 条

全部是 200 但 `charged_tokens = 0`，时间都在 **11-08 及更早**。

**11-09 之后为 0。**

### 结论

**两个可疑分支都已停止产生**，剩下的只是历史残留。最近三天的 51 条 `unknown`
全部属于合理类别（上游报错 31、客户端断开 26……）。

**当前没有活跃的预算泄漏。**

---

## 五、建议怎么处置这 156 + 8 条

**不建议直接改库。** 这张表的状态流转有审计意义，绕过接口手工改会留下无法追溯的
改动。

正确做法是走已有的核对入口（每笔都要填原因与证据，会留痕）：

```
GET  /api/budgets/review?bucket=unknown        # 列出待核对的
GET  /api/budgets/review/evidence?request_id=… # 看单笔证据
POST /api/budgets/{request_id}/resolve         # 结清（reason + evidence 必填）
```

**但更值得先做的是判断：这些历史残留值不值得花时间结清。**

| 项 | 数值 |
| --- | --- |
| 待人工判定 | 164 条（156 + 8） |
| 绑定的预留量 | 约 179 亿 token 的**预留**（不是实际消耗） |

`reserved_tokens` 是**上限承诺**，不是已用量 —— 那些请求实际消耗为 0（`charged=0`）。
所以这 164 条**不影响成本核算**，影响的是「未决账目」的观感。

**结论：可以留到最后再处理，不必现在动。**

---

## 六、怎么复核

```bash
DB=/opt/workbuddy-manager/data/manager.db

# token 两侧一致性
sqlite3 "$DB" "SELECT sum(prompt_tokens), sum(completion_tokens) FROM request_logs
               WHERE ts > strftime('%s','now') - 86400;"
sqlite3 "$DB" "SELECT sum(prompt_tokens), sum(completion_tokens) FROM usage_events
               WHERE ts > strftime('%s','now') - 86400;"

# 预留状态分布
sqlite3 "$DB" "SELECT state, count(*) FROM budget_reservations GROUP BY 1;"

# unknown 的终态分类
sqlite3 "$DB" "SELECT coalesce(l.terminal_state,'(无日志)'), count(*)
               FROM budget_reservations b
               LEFT JOIN request_logs l ON l.request_id = b.request_id
               WHERE b.state='unknown' GROUP BY 1 ORDER BY 2 DESC;"

# 最近三天是否还有可疑类型
sqlite3 "$DB" "SELECT coalesce(l.terminal_state,'(无日志)'), count(*)
               FROM budget_reservations b
               LEFT JOIN request_logs l ON l.request_id = b.request_id
               WHERE b.state='unknown' AND b.created_at > strftime('%s','now') - 259200
               GROUP BY 1;"
```

**最后一条是重点**：它回答「现在还在漏吗」。前几条都是历史快照。

---

## 七、下次复核

建议与磁盘预算一起，每月看一次。关注两点：

1. **最近三天的 `rejected` / `completed` 是否仍为 0** —— 不为 0 说明有回归；
2. **`unknown` 的日增是否稳定在个位数** —— 当前是 6–7/天。

如果日增突然上到两位数，回头看是哪个上游或哪个终态贡献的。
