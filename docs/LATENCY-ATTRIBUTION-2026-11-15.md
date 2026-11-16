# 延迟归因 — 2026-11-15 实测

**结论先说**：这条链路的延迟主要由 **prompt 长度**决定，不是网关。
网关自身的开销约 **1 秒**。

这份记录的目的是：下次有人说「网关慢」时，先看这里，别重新测一遍。

---

## 一、端到端实测

近 24 小时、只统计 `status=200`：

| 分组 | 次数 | 平均 prompt_tokens | 平均耗时 |
| --- | --- | --- | --- |
| **流式** | **8,530** | **314,763** | **7.3 s** |
| 非流式 | 140 | 5,632 | 4.8 s |

| 分位 | 端到端 | 首字节 |
| --- | --- | --- |
| P50 | 4,970 ms | **2,516 ms** |
| P95 | 21,261 ms | 12,546 ms |
| P99 | 47,973 ms | — |

**端到端 P50 4.97 秒，首字节 P50 2.52 秒。** 差值约 2.4 秒，
其中约 **1 秒**是网关自身处理（鉴权、路由、转发、记账、日志），其余在流式读 body。

## 二、慢请求长什么样

超过 30 秒的 200 响应共 **306 次**，特征高度一致：

| 模型 | 流式 | 次数 | 平均 prompt | 平均耗时 |
| --- | --- | --- | --- | --- |
| `deepseek-v4.1-flash` | 是 | **306** | **355,980** | 44.6 s |
| `cn:deepseek-v4.1-flash` | 是 | 3 | 202,901 | 50.1 s |
| `cn:deepseek-v4.1-flash` | 否 | 1 | 7,379 | 41.2 s |

**慢请求的平均 prompt 是 35.6 万 token。** 非流式那次只有 7,379 token
却用了 41.2 秒 —— 那次是 **completion 8,061 token**，属于长输出。

**换句话说：慢的要么是超长输入，要么是超长输出。没有一次是网关在磨蹭。**

## 三、为什么 prompt 会到 31 万

这是**业务形态**，不是异常：

- 客户端是 Claude Code / Codex 这类编码工具
- 它们把整个仓库上下文、对话历史、工具结果一起塞进请求
- 31 万 token 约等于 **1.2 MB** 的文本（按 4 字节/token 粗算）

**给这种请求设一个「60 秒内必须返回」的门槛，等于把它全部打死。**
这正是 `WB_FIRST_BYTE_TIMEOUT_S=240` 存在的原因（实测有 158 次首字节超过 60 秒，
最长 186 秒）。

## 四、容量侧的事实

| 指标 | 观测值 | 配置 |
| --- | --- | --- |
| 在飞请求 | 1 | 上限 64 |
| 排队 | 0 | — |
| 容量拒绝 | **0 次** | — |
| 在飞请求体 | 3.4 MB | 上限 384 MB |

**容量从未成为瓶颈。** 一天 8,600 次请求 ≈ 每分钟 6 次，离 64 并发很远。

## 五、所以「优化延迟」该往哪里使劲

按有效性排序：

1. **减少 prompt**（客户端侧：更聪明的上下文裁剪）。这是唯一能显著改变 P95 的手段
2. **选更快的上游/模型**。P99 那 48 秒里，绝大部分是上游 prefill
3. **网关侧**：已经只有约 1 秒，可压榨空间有限

**先别动网关的超时和并发参数** —— 它们不是瓶颈，动它们只会引入风险。

## 六、怎么复测

```bash
DB=/opt/workbuddy-manager/data/manager.db

# 端到端分位（只算成功的）
sqlite3 "$DB" "WITH t AS (
  SELECT latency_ms v FROM request_logs
  WHERE status=200 AND latency_ms>0 AND ts > strftime('%s','now')-86400)
SELECT 'p50='||(SELECT v FROM t ORDER BY v LIMIT 1 OFFSET (SELECT count(*)/2 FROM t))
    ||' p95='||(SELECT v FROM t ORDER BY v LIMIT 1 OFFSET (SELECT count(*)*95/100 FROM t));"

# 慢请求的特征
sqlite3 "$DB" "SELECT model, stream, count(*), round(avg(prompt_tokens)), round(avg(latency_ms)/1000.0,1)
  FROM request_logs WHERE status=200 AND latency_ms > 30000
    AND ts > strftime('%s','now')-86400 GROUP BY model, stream;"

# 首字节分位
sqlite3 "$DB" "WITH t AS (
  SELECT first_token_ms v FROM request_logs
  WHERE status=200 AND first_token_ms>0 AND ts > strftime('%s','now')-86400)
SELECT 'p50='||(SELECT v FROM t ORDER BY v LIMIT 1 OFFSET (SELECT count(*)/2 FROM t));"
```

## 七、已知不算问题的那几类

| 现象 | 判定 |
| --- | --- |
| 7 次 499 | 用户在 1.8–15.7 秒内主动停的，`prompt_tokens=0`，未计费 |
| 140 次 `first_token_ms` 为空 | **全是非流式**，那种请求没有「首 token」概念，只有首字节 |
| 4 次 `actual_platform` 为空 | 两次列模型 + 两次探测请求（路由阶段 400，从未转发） |
| 10 次 5xx | 全是 `upstream_transport_uncertain`，上游传输中断，**故意不重试**（不确定是否已处理，重放会重复计费） |

**最后一条值得强调**：那 10 次错误率 0.116%，全部来自上游，而且 `error_stage` 是空串
（不是 NULL）—— 这是既定形态，不是缺陷。
