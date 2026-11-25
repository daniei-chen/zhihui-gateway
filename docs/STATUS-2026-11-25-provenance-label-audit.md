# `/v1/models` 被标成「用量未知」：是真问题，但不值得改（第 52 轮）

## 1. 怎么发现的

按规范 §5.4 查 `usage_events` 的 `provenance` 分布：

```
provider-reported   5293
unknown-credit        42
```

42 条「用量未知」。这个数字本身没毛病——上游不返回计费信息是常态。
但我想知道它们来自哪里：

| model | realm | 条数 |
|---|---|---|
| **（空）** | cn | **23** |
| `deepseek-v4.1-flash` | cn | 12 |
| `ren/deepseek-v4.1-flash` | cn | 7 |

**`model` 为空的有 23 条。**再查端点：

| 端点 | 条数 |
|---|---|
| **`/v1/models`** | **23** |
| `/v1/chat/completions` | 19 |

**`/v1/models` 是只读的模型列表端点**，它不消耗 token。它出现在用量账本里，
本身就是可疑的。

## 2. 第一层判断：数据层是对的

先看这 23 条的完整字段：

```
terminal_state  counted  prompt_tokens  completion_tokens  credit_units   n
completed       0        0              0                  (NULL)         23
```

**`counted = 0`。**

这一列由 `gateway.py` 这样算出来：

```python
counted = bool(key and (pt+ct or credit))
```

`pt+ct = 0`、`credit = None` → `counted = False`。**所以它本来就不计入用量统计。**

**数据层没有问题。**真正的问题只在 `provenance` 这一列的**标签**：

```python
# ledger.py:141
'provider-reported' if credit is not None else 'unknown-credit'
```

**只用 `credit` 是否为 `None` 来分两类**，把三种完全不同的情况压成了同一个标签：

| 来源 | terminal_state | counted | credit | 当前标签 | 实质 |
|---|---|---|---|---|---|
| `/v1/models` | completed | 0 | NULL | `unknown-credit` | **本来就不该计费** |
| chat 取消 | client_cancelled | 0 | NULL | `unknown-credit` | 用量可能已产生但未知 |
| chat 上游错 | upstream_error | 0 | NULL | `unknown-credit` | 用量可能已产生但未知 |

**第一行和二三行是两回事。**前者是「没有计费这回事」，后者是「该计费但不知道多少」。

## 3. 第二层判断：想修，但修法有坑

最直觉的修法是让 `record()` 区分出来。但 `record()` 拿不到路径，只有
`counted`、`terminal`、`credit` 三个参数。

试过的判据：

```python
elif not counted and terminal == 'completed':
    prov = 'not_billable'
```

**问题是 `counted=0` 同时覆盖三种情况**（上表就是证据），
单靠它区分不了。能区分的是 `terminal_state`，而这个字段**已经被正确记录了**。

所以真正需要的信息其实**已经在库里**，只是 `provenance` 这个冗余标签没跟上。

要让它跟上，只能显式传入 `spend_kind`——而 `record()` 的调用方之一是
**受保护文件 `server/routers/gateway.py`**。

## 4. 第三层判断：查它是否真的有人依赖

在决定改不改之前，先查 `provenance` 被谁读：

```python
# budget.py:585
"AND e.credit_units=0 AND e.provenance='provider-reported' THEN 1 ELSE 0 END) zero_event_count "
```

**这是唯一一处真正按 `provenance` 值做判断的地方。**它用来识别
「上游明确回报了 0 用量」的事件。

关键条件有两个：`credit_units = 0` **且** `provenance = 'provider-reported'`。

而 `/v1/models` 的 `credit_units` 是 **NULL**，不是 0。

**所以它本来就不满足这个条件，标签怎么叫都不影响这段逻辑。**

其余引用（`ledger.py:314` 的 `sorted({...})`、`budgetreview.py:91` 的列选择）
都只是原样展示，不做分支判断。

## 5. 结论：不改

| 维度 | 判断 |
|---|---|
| 数据正确性 | **没问题**（`counted=0` 已排除统计） |
| 有无下游依赖 | **没有实质依赖**（唯一的分支判断不受影响） |
| 修正成本 | 要改**受保护文件** `gateway.py` 的调用签名 |
| 收益 | 一个冗余标签更准确；真需要区分时查 `terminal_state` 即可 |

**成本 > 收益。这一轮不改。**

规范 §2.1 说「不要在没有 P0/P1 的时候硬凑改动」。这一条连 P2 都算不上——
P2 的前提是「体验或可维护性真的受损」，而这里受损的只是一个没人依赖的标签。

## 6. 但有一条值得记住

`unknown-credit` 这个数字里**有 55%（23/42）其实是「本来就不该计费」**。

将来如果有人盯着 `unknown-credit` 想判断「上游是不是开始不回计费信息了」，
会看到一个虚高一倍的数。

**所以不改代码，但要把这件事写下来。**查的时候请配合 `terminal_state` 一起看：

```sql
-- 真正的「上游没回计费」只看这些
SELECT * FROM usage_events
WHERE provenance='unknown-credit'
  AND terminal_state IN ('client_cancelled','upstream_error','interrupted');
```

## 7. 这一轮顺带确认的账本健康度

```
terminal_state        近 24 小时
completed               5316
client_cancelled          12
upstream_error             7
```

`upstream_error` 7 条，与 `request_outcomes` 里的 7 个 5xx 完全吻合
（11-24 21:18 那批 `no_healthy_account` 容量拒绝）。**账本与请求记录对得上。**

## 8. 待办

| 项 | 状态 |
|---|---|
| v1.5.83 Release | CI 仍在构建（2/4 资产） |
| `provenance` 标签精确化 | **不改**，成本大于收益 |
| age 私钥离线保管 | 运维（一次拷贝） |
| 异地备份 | 运维（需外部存储） |