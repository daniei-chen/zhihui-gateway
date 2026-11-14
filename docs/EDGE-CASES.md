# 边界输入与异常路径：实测行为

这份文档记录**实际测过**的行为，不是设计意图的转述。每条都有状态码、行为说明和
记录位置 —— 出了问题先来这里对照，能省掉一次猜测。

测量时间：2026-11-14 / 15（版本 `v1.2.66`）。

---

## 一、请求体与格式

| 输入 | 状态码 | 响应 | 记录在 |
| --- | --- | --- | --- |
| 畸形 JSON | **400** | 「请求体不是合法 JSON」 | `request_logs` |
| 空 body | **400** | 同上 | `request_logs` |
| `model` 是数字 | **400** | 「model 必须是字符串」 | `request_logs` |
| `model` 前缀写错（如 `zhihui/x`） | **400** | 列出可用平台 + 可抄的例子 | `request_logs` |
| 请求体 65 MB（上限 64 MB） | **413** | 说明上限来源与如何调整 | **`request_outcomes`** |

**注意最后一行**：超限请求记在 `request_outcomes`，**不在** `request_logs`。
因为它在解析请求体之前就被拒了，还没到写账本那一步。

查这类拒绝要去 `request_outcomes`：

```sql
SELECT datetime(ts,'unixepoch','+8 hours'), http_status, error_code
FROM request_outcomes WHERE http_status=413 ORDER BY ts DESC LIMIT 20;
```

`error_code` 是 `gateway.request.too_large`。

---

## 二、平台前缀

网关支持三种前缀：`workbuddy/`、`qoder/`、`trae/`。写错时**不再转发给上游**。

| 输入 | 结果 |
| --- | --- |
| `trae/Doubao-Seed-Code` | 正常路由（前缀被剥离） |
| `qoder/glm-5.3-flash` | 正常路由 |
| `zhihui/whatever` | **400**，提示可用平台 |
| `qtg/x`、`workbuddy2/x`、`buddy/x` | **400**，同上 |
| `meta-llama/Llama-3-70B` | **正常通过**（真实模型名带斜杠，不误伤） |
| `Qwen/Qwen2.5-72B` | 正常通过 |

**为什么不是「凡带斜杠就拒绝」**：真实模型名也有带斜杠的，一律拒绝会误伤。只有
前缀**像平台名却不是**时才提示。

---

## 三、取消与中断

| 场景 | 状态码 | 终态 | 阶段 | 资源 |
| --- | --- | --- | --- | --- |
| 客户端在流式传输中断开 | **499** | `client_cancelled` | `client` | **完全释放** |
| 上游在流中异常 | 200（流已开始） | `transport_error` 等 | `stream_read` | 完全释放 |

**实测数据**（断开后连续采样）：

```
断开后 1s：在途 0，在途字节 0
断开后 3s：在途 0，在途字节 0
断开后 4s：在途 0，在途字节 0
```

499 **不记错误文本** —— 主动断开不是故障，`error_stage='client'` 已足够定位。

---

## 四、容量拒绝

| 类型 | 状态码 | 响应里的 `code` |
| --- | --- | --- |
| 输出预留内存预算 | **503** | `gateway_capacity_exceeded` |
| 全局准入 | **503** | `gateway_capacity_exceeded` |
| 入站 + 转发体预算 | **503** | `gateway_capacity_exceeded` |
| 大上下文专用上限 | **503** | `gateway_capacity_exceeded` |
| **每密钥并发** | **429** | `key_concurrency_exceeded` |
| 管理入口并发 | **503** | — |

**每密钥并发用 429 而不是 503** —— 那是限流，不是服务故障，客户端不该当成故障重试。

容量拒绝都带 `Retry-After`，输出预留那条还带 `gateway_hint` 说明是容量保护。

### 怎么判断到底是哪一种

拒绝时的日志会写明：

```
输出预留被拒 key_id=1 声明输出=XXXX 在途字节=XXXX/402653184
```

配合 `/metrics` 的准入指标一起看：

| 指标 | 说明 |
| --- | --- |
| `workbuddy_gateway_inflight` | 在途请求数 |
| `workbuddy_gateway_inflight_body_bytes` | 已预留的字节 |
| `workbuddy_gateway_capacity_rejections_total` | 被拒总数 |
| `workbuddy_gateway_queued` | 排队中的请求 |
| `workbuddy_gateway_queue_timeouts_total` | 排队等到超时 |

---

## 五、`error_code` 的取值

`request_outcomes.error_code` 里 **`'none'` 表示「没有错误」**，不是某个具体错误码。

统计错误时记得排除它：

```sql
SELECT error_code, count(*) FROM request_outcomes
WHERE ts > strftime('%s','now') - 86400
  AND error_code IS NOT NULL AND error_code != 'none'
GROUP BY 1 ORDER BY 2 DESC;
```

这是有意设计（代码里有具名常量 `ERROR_CODE_NONE`），不是遗漏。

---

## 六、怎么复现上面这些测量

需要一把临时密钥。数据库只存哈希，所以做法是临时插入、测完删除：

```bash
TOKEN="sk-$(openssl rand -hex 16)"
HASH=$(printf '%s' "$TOKEN" | sha256sum | awk '{print $1}')
# 插入（platforms 要含要测的平台），测完 DELETE
# 然后：
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:7866/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"zhihui/whatever","messages":[{"role":"user","content":"hi"}]}'
```

**务必用 `trap` 保证删除**，否则会在生产留下可用的密钥。

---

## 维护提示

新测出边界行为就往对应表里加一行，并写清**记录在哪个表** —— 这一点比状态码更容易
被忽略，也更容易在排查时白找。
