# 审计 D-补：配额双路径 / 红包 / dailyquota / 账本 replay 对账

- 仓库：`daniei-chen/zhihui-gateway`，`main` @ `ec87de9`（`git log --oneline -1` → `ec87de9 Merge pull request #58 ...`）
- 只读审计：未修改仓库、未 commit/push；复现脚本在 `/tmp/wb-sub-d/repro_d4.py`、`repro_b2.py`，临时库在 `tempfile.mkdtemp()` 下。
- 行号均以 `ec87de9` 为准，均来自 `sed -n` / `grep -n` 实读。

---

## A) credit 配额 vs token 配额：两条准入路径的差异

### A1 [P0 业务口径] credit 路径没有任何金额预留，只有「已花 ≥ 额度」这一道零值闸

`server/budget.py:284-295`：预留行写入的 `reserved_credit` **恒为 0.0**，credit 判据只有一个 `<= 0`：

```
284:        tokens = estimated_input + output
287:        credit = 0.0                       # reserved_credit 仍写 0：本项目没有本地定价表
288:        if qc:
291:            remaining_credit = max(0, qc - uc) / ledger.SCALE
292:            if remaining_credit <= 0:
293:                raise BudgetRejected('积分预算已用尽', 'credit_quota_exhausted')
294:        db.execute('INSERT INTO budget_reservations(request_id,...,reserved_credit,...) VALUES(...)',
295:                   (rid,key['id'],now,'reserved',tokens,credit,now,budgetowner.identity(),1,now+60))
```

对比 token 路径 `budget.py:274-279`：`remaining = q - used - total_held`，并且**收缩 max_tokens**：

```
274:        if q:
275:            remaining = q - used - total_held
276:            if remaining <= estimated_input: raise BudgetRejected(..., 'insufficient_quota')
278:            output = min(output, remaining - estimated_input); outputlimits.apply(body, output)
```

实测（`/tmp/wb-sub-d/repro_d4.py`，真实 `budget.prepare`→`forwarded`→`settle`→`keysvc.touch`）：

```
A1 key before prepare: {'quota': 0, 'quota_credit': 2e-06, 'used_credit': 1e-06, 'used_credit_units': 1}
A1 reservation after prepare: {'state': 'reserved', 'reserved_tokens': 6200, 'reserved_credit': 0.0, 'charged_credit': 0.0}
A1 key after one 0.5-credit request: {'quota_credit': 2e-06, 'used_credit': 0.500001, 'used_credit_units': 500001} overrun x250001
A1 second prepare rejected: BudgetRejected credit_quota_exhausted
```

**结论（回答题面）**：设了 credit 配额的密钥，剩余 1 微单位（1e-6 credit）时准入放行，单笔请求实际可以花掉 0.5 credit —— 本算例超支 **250001 倍**。超支上限 = 「已受理但未结算」的请求数 × 单笔真实金额：

- 并发闸对有配额密钥默认只允许 1 个在途（`budget.py:225-231`，`_limit = WB_KEY_BUDGET_PENDING_QUOTA` 默认 1），所以默认上限≈单笔金额；
- 该 env 可被调高（`budget.py:222-223` 源码直读 env），抬高多少就乘多少；
- **没有任何一道闸按金额预留**。真正能拦住的只有「已花 ≥ 额度」这个后置零值判据，以及 A3 的 `credit_quota_unconfirmed`（硬 429）—— 都是事后，不是预留。

### A2 daily 两条判据的边界不同：token 要留输入余量，credit 相等才拒

`server/budget.py:263-273`：

```
263:            if fresh['daily_token_quota']:
264:                remaining_daily = int(fresh['daily_token_quota']) - int(daily['tokens']) - held
265:                if remaining_daily <= estimated_input:                 # 需要留出本次输入估算
266:                    raise BudgetRejected(..., 'daily_token_quota_exhausted')
267:                output = min(output, remaining_daily - estimated_input); outputlimits.apply(body, output)
270:            if fresh['daily_credit_quota']:
271:                if any(r['state'] == 'unknown' for r in outstanding):
272:                    raise BudgetRejected(..., 'daily_credit_unconfirmed')
273:                if int(daily['credit_units']) >= ledger.units(fresh['daily_credit_quota']):
                    raise BudgetRejected(..., 'daily_credit_quota_exhausted')
```

差异：token 侧是「剩余额度必须覆盖输入估算」并收缩 max_tokens（`held` = 当日未结算的 token 预留，`budget.py:262`）；credit 侧是「已花 ≥ 额度」，不减去任何 in-flight 金额、不收缩 max_tokens。即 daily 积分额度剩 1 微单位时，同样放行一整笔。

### A3 fail-closed 判据覆盖的状态集合不一致

`server/budget.py:229-240`：

```
229:                'SELECT COALESCE(SUM(MAX(0,reserved_tokens-charged_tokens)),0) AS held,'
230:                "COALESCE(SUM(CASE WHEN state='unknown' AND reserved_tokens<=0 THEN 1 ELSE 0 END),0) AS missing_estimate,"
231:                "COALESCE(SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0) AS unknown_count "
232:                "FROM budget_reservations WHERE key_id=? AND state IN ('reserved','forwarded','unknown')"
233:                " AND coalesce(resolution,'') NOT LIKE ?", (key['id'], ARCHIVE_RESOLUTION_PREFIX + '%'))
237:            if q and outstanding_total['missing_estimate']:
238:                raise BudgetRejected(..., 'total_token_unconfirmed')
239:            if qc and outstanding_total['unknown_count']:
240:                raise BudgetRejected(..., 'credit_quota_unconfirmed')
```

- token 侧只对 `unknown AND reserved_tokens<=0`（没有估算量的行）fail-closed；有估算的 unknown 只按 `held` 扣减；
- credit 侧对**任何**未归档 unknown fail-closed —— 一次断连就长期 429（`budget.py:216-227` 注释自己承认「一把密钥会因为一次按 Esc 长期不可用」）；
- 归档过滤（`resolution NOT LIKE '已归档待核对：%'`，`budget.py:233`）**只在这条 lifetime 查询里**；daily 窗口查询 `budget.py:256-261` 没有该过滤：

```
256:            outstanding = db.query("SELECT b.state,b.reserved_tokens,b.charged_tokens FROM budget_reservations b "
257:                                   "WHERE b.key_id=? AND (b.state IN ('reserved','forwarded') OR "
258:                                   "(b.state='unknown' AND coalesce((SELECT l.ts FROM request_logs l ...))>=? AND ...<?))"
```

`_mark_archived`（`budget.py:657-669`）只写 `resolution`、`state` 保持 `unknown`，所以 daily 侧的 `held` 与 `daily_credit_unconfirmed` 会把已归档 unknown 继续算进去。当前窗口只有「今天」，而归档期限最小 1 天（`budget.py:647-649`、`:672-678`），现实冲突罕见；但两处口径不一致是确凿的代码事实。

### A4 「15 分钟在途」窗口对两条路径同时放宽，对 credit 密钥等于无遮挡

`server/budget.py:225-231`：`unknown` 只在 `updated_at >= now-900` 时计入 `pending`，且 `_mark_archived` 刻意不改 `updated_at`（`budget.py:660-662` 注释）。对 token 密钥，老 unknown 仍按 `reserved_tokens` 计入 `total_held`（有预留）；对 credit 密钥，老 unknown 既不占金额预留、又不再占并发计数，唯一还拦着的是 A3 的 `credit_quota_unconfirmed` 硬闸。

---

## B) dailyquota / redpacket 与主账本（api_keys / usage_events / adjustment_events）的交叉

### B1 [可达分叉] `reset_usage` 归零 api_keys，但不清 `key_daily_usage` → 重置当日仍被判「额度用尽」

`server/keysvc.py:369-393` 只改 api_keys：

```
388:        conn.execute('INSERT INTO usage_reset_events(key_id,event_units_before,previous_units,previous_tokens,ts,actor) ...')
390:        conn.execute('UPDATE api_keys SET used_tokens=0,used_credit=0,used_credit_units=0 WHERE id=?',(key_id,))
```

而日配额读的是 `key_daily_usage`（`server/dailyquota.py:29-33`，缺失时才回落 `usage_daily`），`reset_usage` 从不写这张表。实测（`/tmp/wb-sub-d/repro_b2.py`）：

```
after charge: api_keys.used_tokens=1000 key_daily_usage.tokens=1000
after reset_usage: api_keys.used_tokens=0 key_daily_usage.tokens=1000
prepare after reset rejected: BudgetRejected daily_token_quota_exhausted
```

结论：`daily_token_quota` / `daily_credit_quota` 密钥执行「重置用量」后，管理员以为额度已放开，实际到次日 00:00 前一直 429。分叉是**永久**的（当日），因为 dailyquota 的计数器只增不减（`dailyquota.py:46-49`）。

### B2 [潜在分叉] 负向调整：`max(0,...)` 钳位 vs 账本记原始 td

`server/budget.py:465` 与 `server/dailyquota.py:48` 都做下限钳位，`server/ledger.py:197-200` 的 `adjust()` 记的是**原始** `td`：

```
465:    db.execute('UPDATE api_keys SET used_tokens=max(0,used_tokens+?),used_credit_units=max(0,...)+?) WHERE id=?', ...)
dailyquota.py:48:        db.execute('UPDATE key_daily_usage SET tokens=max(0,tokens+?),credit_units=max(0,credit_units+?) ...')
budget.py:559:        ledger.adjust(request_id, row['key_id'], td, cd, actor, reason)
ledger.py:199:    db.execute('INSERT INTO adjustment_events VALUES(?,?,?,?,?,?,?,?)', (request_id+':reconcile', request_id, ..., td, units(cd), ...))
```

实测（`repro_d4.py`，`api_keys.used_tokens=100`，`charge_delta(kid, -5000, 0, allow_negative=True)` + `ledger.adjust(..., -5000, ...)`）：

```
B1 api_keys.used_tokens=0 key_daily_usage.tokens=0 usage_events+adjust=0+-5000=-5000
B1 divergence (ledger sum - api_keys) = -5000
```

钳位吃掉了 100 的真实余量、并把 -5000 记成 0，而不变账本照写 -5000 → 两边永久差 5000 tokens，只能手工核销。

**可达性如实说明**：负向 delta 默认被拒（`budget.py:451-455`、`:461-462`），当前生产代码无 `allow_negative=True` 调用点（全仓 grep 只有 `server/tests/test_late_usage_race.py:188`）。所以今天是「装了雷没引爆」：只要管理端将来加一个「下调用量」入口并传 `allow_negative=True`（`budget.resolve(..., allow_negative=True)` 已预留），这个分叉立刻出现。`metering.reconcile` 与 `retention.reconcile` 都查不出它（见 C4、D1）。

### B3 redpacket 份额与 0 额度拒绝逻辑：闭合；份额用尽不会回落到「不限」

- 拆包产物绝不为 0：`server/redpacket.py:119-126` 先换算成整数最小单位，`units_total < shares` 直接抛 `RedPacketError`；拼手气分支 `:134-144` 每次 `min(max(amt,1), remaining-(left-1))`。
- 写额度不串档：`redpacket.py:261-262` `quota=amount if kind==KIND_TOKEN else 0, quota_credit=amount if kind==KIND_CREDIT else 0`。
- 0 份额不被当「不限」：`budget.py:183-197` 先判 `limited`，未限时再查 `redpacket.zero_share_amount(int(key['id']))`（`redpacket.py:320-335`），命中即 `BudgetRejected('...0 额度的红包份额...', 'zero_share_quota', status=403)`。
- 收回红包保留份额行（`redpacket.py:436-461`：删 api_keys，`UPDATE red_packet_shares SET claimed_by_ip='', ...`），所以 `zero_share_amount` 的判据不会因为收回而失效；密钥行被删时准入先报 `invalid_api_key`。
- 份额用尽：`quota`/`quota_credit` 始终非 0（全仓 grep `SET quota` / `quota_credit=0` 无生产命中，没有自动清零路径），`used_*` 只增，`remaining <= 0` 恒成立 → 不回落「不限」。唯一例外是管理员执行 `reset_usage`（B1），等于再发一份同等额度 —— 属管理动作，非代码缺陷。

---

## C) 账本不可变性与 replay 缺口

### C1 `_settle` 的 `row is None` 分支：全额记账，但没有任何 charged_* 落库 → 只靠 request_logs 兜底

`server/ledgerreplay.py:308-310`：

```
308:    row = db.query_one('SELECT * FROM budget_reservations WHERE request_id=?', (request_id,)) if request_id else None
309:    if row is None:
310:        return plan          # plan = {'tokens': pt+ct, 'credit_units': credit_units or 0, ...} 全额
```

随后 `_touch`（`ledgerreplay.py:375`）直接 `budget.charge_delta(key_id, plan['tokens'], plan['credit_units'], plan['ts'])`，**没有**任何 `charged_tokens/charged_credit` 记录可做后续幂等判据。防止二次扣减的护栏是 `apply()` 对 `request_logs` 的存在性检查（`ledgerreplay.py:429-433`、`:443-448`：`existing is None` 才走 `_settle`，否则只 `_events`）。凡是「reservation 行丢失且 request_log 也缺失」的库（部分恢复、手工删行、从旧备份恢复 data 而 spool 保留），同一 request 的第二份 receipt 会再扣一次 —— `ledger_receipts` 的键是 receipt_id（文件名 stem），改名复制即可绕过。

### C2 `final` 分支置零 plan，但 `_touch` 仍按 facts 全量 bump；`counted` 由 plan 推导 → 账本缺口不可见

`server/ledgerreplay.py:317-333` 在 `state in ('settled','released')` 时 `plan.update(tokens=0, credit_units=0)`；但 `_touch`（`ledgerreplay.py:375-381`）无条件用 facts 的 `pt/ct/credit` 调 `db.bump_usage`：

```
375:    budget.charge_delta(key_id, plan['tokens'], plan['credit_units'], plan['ts'])   # +0
378:    if total or credit:
380:        db.bump_usage(key_id, facts.get('model') or '', pt, ct, credit, ...)          # +全量
```

于是 `api_keys.used_*` 与 `usage_daily` 走不同口径。更关键的是 `_events` 的 `counted` 由 plan 推导（`ledgerreplay.py:438-440`）：

```
438:    ledger.record(..., pt, ct, credit,
439:                  bool(facts.get('key_id') and (plan['tokens'] or plan['credit_units'])) ...)
```

`counted=False` 而 `prompt_tokens+completion_tokens>0` 的 usage_events 行一旦出现，`metering.reconcile` 的 token 比对就被跳过（`server/metering.py:364`：`if event is not None and event['counted'] and usage.complete:`），这类偏差永远不会被报出来。

### C3 幂等边界：`INSERT OR IGNORE` 静默丢弃「更正」；`adjustment_events.request_id UNIQUE` + 裸 INSERT 是硬失败

- `server/ledger.py:22` `UNIQUE(request_id,attempt,event_type)`，`:137` `INSERT OR IGNORE INTO usage_events`：同一 request 的第二份（不同的）用量事实被**静默丢弃**，账本永远停在第一份；`ledgerbackfill.py:46-52` 用 `NOT EXISTS` 选行、同样是「只补不改」。所以「迟到更正用量」在不可变账本里没有表达方式，只能靠 `adjustment_events` 另记。
- `server/ledger.py:41` `adjustment_events ... request_id TEXT NOT NULL UNIQUE`，`:199` `adjust()` 用裸 `INSERT`（不是 OR IGNORE）：同一 request 的第二次 `budget.resolve()` 抛 `UNIQUE constraint failed`，整个 resolve 事务回滚（`budget.py:556-560` 里 `charge_delta` 已先执行，但同事务回滚）。语义上「一请求只核销一次」是想要的，但异常类型是 SQLite 约束错，调用方（`budget_reconcile`、面板端点）只能当通用失败吞掉。

### C4 `metering.reconcile` 能查出什么、查不出什么

`server/metering.py:246-258` 的 10 个 category：`missing_usage_event`、`token_mismatch`、`platform_mismatch`、`attribution_missing`、`budget_unknown`、`budget_settled_without_event`、`terminal_mismatch`、`zero_token_completed`、`log_without_request_id`、`outcome_without_log`。

覆盖字段与盲区（均按代码实读）：

- `token_mismatch` 只比对 `prompt_tokens/completion_tokens`（`metering.py:364-367`），且**只对 `counted=1` 的行**；`credit_units` 在 `metering.py:326-328` 已加载却从未参与比对 → **金额偏差完全查不出**（B2 那种分叉它看不到）。
- 完全不看 `api_keys.used_tokens/used_credit_units`、`key_daily_usage`、`usage_reset_events` → api_keys 与账本累计值的分叉查不出（全仓 grep `SUM(prompt_tokens+completion_tokens)` 只有 `dailyquota.py`、`db.py:1925/1974`、`retention.py:549/559`，没有一处把 api_keys 与账本对起来）。
- `budget_unknown` / `budget_settled_without_event` 只看 reservation 的 `state`，不看 `charged_tokens/charged_credit` 与 usage_events 金额是否一致。
- 有界窗口：`limit` 只覆盖最近 N 条 `request_logs`（`metering.py:274-298`），`scope.complete_history=False`（`metering.py:408-411`），不是全库结论。
- 唯一看「投影 vs 权威事件」总额的是 `server/retention.py:513-540`，但它只比**全库总额**（`usage_daily` vs `usage_events`），不比 per-key、不含 `adjustment_events`、不涉及 api_keys。

---

## D) 对账工具缺口

### D1 `budget_reconcile.py`（33 行）只做「自动结清零证据 + 打一份摘要」

`server/budget_reconcile.py:8-13`：

```
 8: def run(now=None):
10:     settled = budget.reconcile_evidenced_unknowns(now)
11:     summary = budget.unresolved_summary(now=now)['summary']
12:     return {'settled_confirmed_zero': settled, 'unknown_remaining': summary['unknown_total'],
13:             'needs_human': summary['needs_human'], 'archived_pending': summary['archived_pending']}
```

能做：把「完整上游零用量回执」的 unknown 结清（只 `resolve(..., zero_only=False, expect_charged_zero=True, allow_negative=False)`，`budget.py:854-857`），并输出待人工/已归档计数。
不能做：不归档超期 unknown（`archive_overdue_unknowns` 在这条路径上**没有调用**，只在应用内 900s 循环里调用：`server/operational.py:1076-1080`）、不查 api_keys vs 账本、不查 daily 计数器、不看死信/积压/spool 通道状态、不做金额核对。注意它返回了 `archived_pending`，但本轮**从不产生**归档，只能反映别的进程的成果。

### D2 调度：确实被 systemd 每日任务调用，但单元定义不在仓库

- 入口：`ops/writer-guard/launch_guard.py:227-228` `if reconcile: return prefix + ['server.budget_reconcile']`；身份校验 `ops/writer-guard/runtime_identity.py:16-40`。
- 单元名：`ops/writer-guard/systemd_adapter.py:9-11` `JOB='wb-budget-autoreconcile.service'`、`TIMER='wb-budget-autoreconcile.timer'`；`service_gate.py:16`、`:43-45` 把它列为允许操作的服务。
- 但 `.service`/`.timer` 文件不在仓库（全仓 grep `OnCalendar` 无命中），周期只能从 `docs/COMPETITOR-2026-11.md:103` 的「每日 06:10 结清超期预留」得到 —— **无法从代码验证实际周期**。
- 应用内另有一条更全的路径：`operational.py:1072-1085`，`WB_BUDGET_RECONCILE_INTERVAL_SEC` 默认 900s，`config.BACKGROUND_JOBS` 才跑。两者重叠但不等价（D1）。

### D3 `admission_block()` 把死信/积压放大成**全站**新请求拒绝；三类冲突稳定产出死信

阻断点（`server/ledgerreplay.py:724-744`，被 `server/budget.py:159-161` 在**每个** `prepare()` 最前面无条件调用，含无配额密钥）：

```
159:    _channel = ledgerreplay.admission_block()
160:    if _channel['blocked']:
161:        raise BudgetRejected(_channel['reason'], _channel['code'])
724: def admission_block() -> dict:
731:    if not state['writable'] or state['pending'] < 0 or state['dead'] < 0:  -> ledger_spool_unwritable
735:    if state['dead'] > 0:                                                    -> ledger_dead_letters_pending
739:    if state['pending'] >= backpressure_limit():                             -> ledger_backpressure
```

而 `ReplayConflict` **一次即置 `MAX_ATTEMPTS`**（`ledgerreplay.py:39` `MAX_ATTEMPTS = 5`；`:529` `attempts = MAX_ATTEMPTS if isinstance(exc, ReplayConflict) else ...`；`:531-534` `_move_dead`）。即：一条冲突回执 = 一条未处置死信 = 所有密钥的新生成全部停摆，直到有人 `dispose_dead_letters(actor, reason)`（`ledgerreplay.py:746`，要求审计留痕）。

稳定产出死信的 3 个具体 raise 点（全部会走到 `_move_dead`）：

1. `server/ledgerreplay.py:323` `raise ReplayConflict('receipt conflicts with confirmed budget usage')`
   来源：reservation 已被 `budget.settle` 判为 `released`（客户端 4xx 且非上游阶段，`budget.py:372-376`）或被人工 `resolve` 成 `settled`，而迟到回执带着用量；`pt+ct > charged_tokens` 或 credit 不等即触发。
2. `server/ledgerreplay.py:337` `raise ReplayConflict('receipt accounting day differs from prior charged evidence')`
   来源：已有 charged 证据的行收到跨记账日的回执（`db.day_of(event_ts) != db.day_of(row['created_at'])`）。跨零点重放、或 `request_logs.ts` 与 spool `ts` 口径不同时命中。
3. `server/ledgerreplay.py:457-461` `_validate_existing` 的三个 raise（`receipt differs from committed accounting facts: <field>` / `... accounting credit` / `... accounting day`）
   来源：`request_logs` 已有行（幂等键按 `(key_id, prompt_tokens, completion_tokens, usage_complete, usage_source)` + credit + 记账日比对），回执与之不符 —— 典型是「先落 spool 后现场成功」的两份事实口径不一致。

（同类还有 `:304` 负数用量、`:313` 回执 key 与预留不符、`:418/:424/:428` duplicate-receipt 与 portal owner / 孤立 usage_events 三类，但上述三条是最容易在真实断连/跨日场景下稳定出现的。）

---

## 跑过的命令与结果

```
cd <repo> && WB_DATA_DIR=/tmp/wb-sub-d <venv-python> -m pytest \
  server/tests/test_concurrency_and_atomicity.py server/tests/test_ledger_replay.py \
  server/tests/test_budget_kill_closure.py -q
→ 44 passed, 2 skipped in 42.11s

cd <repo> && WB_DATA_DIR=/tmp/wb-sub-d <venv-python> -m pytest \
  server/tests/test_credits_ledger.py server/tests/test_request_budgets.py -q
→ 35 passed in 8.08s
```

（`<venv-python>` = `D:/AI/capy/work/venvs/zhihui-gateway/Scripts/python.exe`。测试全绿说明这些是**口径/覆盖缺口**，不是现有测试能抓到的回归。）

复现：

```
<venv-python> C:\Users\Administrator\AppData\Local\Temp\wb-sub-d\repro_d4.py   # A1 + B2
<venv-python> C:\Users\Administrator\AppData\Local\Temp\wb-sub-d\repro_b2.py   # B1
```
