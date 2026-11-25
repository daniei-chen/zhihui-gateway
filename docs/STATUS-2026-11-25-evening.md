# 2026-11-25 运维诊断记录（第 19 轮）

本轮没有代码改动，是纯诊断。三条结论，都有数据支撑。

## 1. 224 条鉴权拒绝不是安全事件

近 24 小时 `error_code=gateway.auth.denied` 共 224 条，全部 `key_id` 为空。

按端点拆：

| 端点 | 条数 | 性质 |
|---|---|---|
| `/v1/usage` | 95 | 内部探测，不经过 nginx |
| `/v1/models` | 56 | 未带密钥的客户端轮询 |
| `/api/system/internal-session-verify` | 41 | 前端会话校验 |
| `/api/platforms` | 18 | 前端页面，会话过期 |
| `/v1/chat/completions` | 11 | 未带密钥的调用 |

**交叉验证**：`request_logs` 近 24 小时只有 200 与 503，没有一条 401 ——
鉴权在进入处理前就拒了，不写请求日志。查 nginx 拿到另一侧：

```
401 by IP:   27.37.52.160 × 9 / 66.249.73.23x × 4（Googlebot）/ 其余各 1
401 by path: /api/me × 10 / /api/v1/auth/me × 2 / /v1/models × 1 / /api/accounts/* × 3
```

**经过 nginx 的 401 只有 15 条，数据库记了 208 条**，差额正是那 136 条内部调用。

结论：不是攻击，不是密钥泄露，不需要改代码。

## 2. 「上游无可用账号」是历史事件

2026-11-24 21:18:30–21:18:59 的 30 秒内，同一 key 连续 7 次拿到 503。

当前额度快照（16 分钟前更新）：

| 平台 | 总量 | 剩余 | 占比 |
|---|---|---|---|
| trae/9368 | 3896 | 827.78 | 21% |
| workbuddy/pool | 85860 | 79647 | 93% |

都在阈值以上，此后未再出现同类失败。

## 3. 归档机制从未运行过 —— 需要处理

数据库 718 MB，按表拆：

```
request_logs                        124 MB   368,695 行
forwarding_events                   105 MB   ← 账本，禁止删除
request_outcomes                     65 MB
usage_events                         42 MB
attempt_events                       41 MB
provider_attempt_events_v2           34 MB
budget_reservations                  32 MB   231,492 行
```

`forwarding_events` 是 `CLASS_LEDGER_EVENT`，带 `no_delete` 触发器（`ledger.py:100`），
按规范不动。

`request_logs` 是 `CLASS_DERIVED_LOG` + `ACTION_ARCHIVE_THEN_PRUNE`，
保留策略明确允许「归档后清理」。但查归档记录：

```
archive_runs 行数：0
```

**归档一次都没跑过。** 不是失败，是从来没有执行。

### 为什么本轮不动手

`request_logs` 虽是派生日志而非账本，但它是排查问题的唯一入口 —— 本文第 1 节
就是靠它 与 `request_outcomes` 交叉验证得出的。清理窗口定多长需要先想清楚，
不能为了省 100 MB 砍掉排查能力。

下一轮要做的：

1. 读 `server/retention.py:155` 处 `request_logs` 的 `archive_filter` 与窗口参数
2. 确认归档产物落点与可读回性（`archive_runs` 结构已确认存在）
3. 跑一次后核对：归档行数 = 清理行数，窗口内行数不变

**验收**：`archive_runs` 有记录、`request_logs` 行数下降、24 小时错误率与
TTFT 两段仍能正常出数（两者都读 `request_logs`）。