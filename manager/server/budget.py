"""Durable estimated budget reservations; unknown usage remains held for reconciliation.

Reservations protect concurrency and crash recovery. Provider tokenization/pricing is
not controlled here, so estimated budgets are not a hard provider billing guarantee.
"""
from __future__ import annotations
import json
import logging
import math
import os
import time
from datetime import date, timedelta
from decimal import Decimal
from . import db, observability, budgetowner, ledger, config

SCHEMA = '''
CREATE TABLE IF NOT EXISTS budget_reservations (
 request_id TEXT PRIMARY KEY, key_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
 state TEXT NOT NULL, reserved_tokens INTEGER NOT NULL DEFAULT 0,
 reserved_credit REAL NOT NULL DEFAULT 0, charged_tokens INTEGER NOT NULL DEFAULT 0,
 charged_credit REAL NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
 resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_key_state ON budget_reservations(key_id,state);
CREATE INDEX IF NOT EXISTS idx_budget_unresolved_scan ON budget_reservations(created_at,request_id)
 WHERE state IN ('reserved','forwarded','unknown');
CREATE TABLE IF NOT EXISTS budget_adjustments (
 request_id TEXT PRIMARY KEY, key_id INTEGER NOT NULL, ts INTEGER NOT NULL,
 token_delta INTEGER NOT NULL, credit_delta REAL NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL
);
'''

class BudgetRejected(Exception):
    def __init__(self, message, code='budget_unavailable'):
        super().__init__(message)
        self.code = code

#: U27（2026-10-28）：`initialize()` 的**进程级一次性标志**。
#:
#: 问题：`prepare()` 在**每个请求**的预留路径上都调 `initialize()`，而它每次要跑
#: 3 条 DDL（各自独立提交）+ 1 个 `BEGIN IMMEDIATE` 事务 + `PRAGMA table_info`。
#: 实测（生产库副本，69,647 行 / 207MB WAL）：`prepare()` p50 0.286ms，把
#: `initialize()` 换成 no-op 后 p50 0.178ms —— **约 38% 的耗时是 DDL/探测**，
#: 而且每个请求多出 4 次写事务，在 32 并发下就是 4 倍串行化。
#:
#: 键是 `(pid, DB 路径)`，两个维度都必须进键：
#:   * 测试/多库会切换 `config.DB_PATH` → 换库必须重新建表；
#:   * fork 出来的子进程 pid 不同 → 必须自己建（它有自己的连接）。
#: 失败**不写标志**（下次重试），成功才写 —— 绝不把一次失败缓存成「已就绪」。
_SCHEMA_STATE: dict = {'key': None}


def initialize(force: bool = False) -> bool:
    """建表 + 补列（幂等）。返回**本次是否真的执行了 DDL**。

    热路径语义：`prepare()` 仍然调用它（保留「表一定存在」的保证与调用点的原子性），
    但**只有首次**（或换库/fork 后首次）才真正执行 DDL，之后是一次字典比较。
    `force=True` 供迁移/测试强制重跑。
    """
    key = (os.getpid(), str(config.DB_PATH))
    if not force and _SCHEMA_STATE['key'] == key:
        return False
    # Schema initialization belongs to startup, outside an existing transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip(): db.execute(statement)
    with db.transaction() as conn:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(budget_reservations)')}
        for name, declaration in (('owner', 'TEXT'), ('fence', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('lease_until', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                conn.execute(f'ALTER TABLE budget_reservations ADD COLUMN {name} {declaration}')
        # U27（2026-10-28）：`heartbeat()` 每 10 秒执行一次
        #   UPDATE budget_reservations SET lease_until=? WHERE owner=? AND state IN (...)
        # 而这张表原先只有 (key_id,state) 索引 → 该 UPDATE 是**全表扫描**
        # （生产 69,647 行，`EXPLAIN QUERY PLAN` 实测 `SCAN budget_reservations`）。
        # 加 (owner,state) 索引把它变成索引定位：心跳从「扫 7 万行」变成「找几行」。
        #
        # 必须建在**补列之后**：`owner` 不在基础 CREATE TABLE 里，而是上面 ALTER
        # 补出来的 —— 放进 SCHEMA（在建表语句串里）会在**新库**上先建索引，
        # 直接 `no such column: owner`（本轮实测踩到过）。
        conn.execute('CREATE INDEX IF NOT EXISTS idx_budget_owner_state '
                     'ON budget_reservations(owner,state)')
    _SCHEMA_STATE['key'] = key
    return True

def recover():
    """A startup must not reclaim another running worker's reservations."""
    initialize()
    budgetowner.identity()
    with db.transaction():
        for row in db.query("SELECT request_id,state,owner,fence FROM budget_reservations WHERE state IN ('reserved','forwarded')"):
            live = budgetowner.alive(row['owner'])
            if live is True:
                continue
            # Only a demonstrably dead local owner with an unsent reservation can
            # be released. Missing/legacy/remote identity remains uncertain.
            target = 'released' if live is False and row['state'] == 'reserved' else 'unknown'
            db.execute('UPDATE budget_reservations SET state=?,fence=fence+1,updated_at=? WHERE request_id=? AND fence=?',
                       (target, int(time.time()), row['request_id'], row['fence']))

def heartbeat():
    now = int(time.time())
    db.execute("UPDATE budget_reservations SET lease_until=? WHERE owner=? AND state IN ('reserved','forwarded')",
               (now + 60, budgetowner.identity()))

def _env_int(name, default):
    """读环境变量里的正整数；缺失/非法回落默认值。C-004：阈值不该藏在带外补丁里。"""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _owned(row, state):
    return row['owner'] == budgetowner.identity() and row['fence'] == getattr(state, 'budget_fence', None)

def prepare(key, body, *, encoded_body=None):
    state = observability.current.get()
    if state is None: return
    state.key_id = int(key['id'])
    # Unlimited keys also get a durable forwarding marker, allowing a killed
    # process or failed settlement to expose uncertain spending after recovery.
    # U01（2026-10-28）：回执积压时对**新请求**背压。判据是「已落盘但还没进库」
    # 的条数，不是「数据库已经写失败」——那时新请求已经接进来了，只能事后补救。
    # 达到上限时拒绝新生成，让重放先追上；已在途的请求不受影响。
    from . import ledgerreplay
    # N02（复审 reaudit-20261028-1108）：准入**不能只看积压条数**。
    # `should_backpressure()` 只数 spool 里的文件，而 spool 不可写时那里恒为 0 ——
    # 于是"事实写不进任何地方"的通道故障反而让新请求照收。`admission_block()`
    # 把「不可写 / 有未处置死信 / 积压超限」统一成明确的可恢复错误码。
    _channel = ledgerreplay.admission_block()
    if _channel['blocked']:
        raise BudgetRejected(_channel['reason'], _channel['code'])
    initialize()
    rid = state.request_id
    with db.transaction():
        fresh = db.query_one('SELECT quota,used_tokens,quota_credit,used_credit,used_credit_units,'
                             'daily_token_quota,daily_credit_quota FROM api_keys WHERE id=?', (key['id'],))
        if fresh is None: raise BudgetRejected('密钥已被删除', 'invalid_api_key')
        limited = bool(fresh['quota'] or fresh['quota_credit'] or fresh['daily_token_quota'] or fresh['daily_credit_quota'])
        states = "('reserved','forwarded','unknown')" if limited else "('reserved','forwarded')"
        if limited:
            # A-002 修复（2026-10-27 审计 S1）：unknown = 「上游没回 usage，等核对」。
            # 它是历史遗留状态，却被永久计入「有配额密钥只允许 1 个在途」这道闸。
            # 实测积压 120-190 条（最老 2026-10-24）⇒ 一旦给密钥设配额，之后每个
            # 请求都 429 budget_pending 且不自愈。
            # 这里只把最近 15 分钟内的 unknown 视为「在途」，更老的交给对账任务，
            # 与「只有进行中的请求才该阻塞新请求」的原本意图一致。
            _cutoff = int(time.time()) - 900
            pending = db.query_one(
                'SELECT count(*) n FROM budget_reservations WHERE key_id=? AND '
                "(state IN ('reserved','forwarded') OR (state='unknown' AND updated_at>=?))",
                (key['id'], _cutoff))
        else:
            pending = db.query_one('SELECT count(*) n FROM budget_reservations WHERE key_id=? AND state IN '+states, (key['id'],))
        # C-004：未结算请求上限改为源码直读 env（默认值与历史硬编码一致：
        # 有配额 1、无配额 4）。此前只由 sitecustomize 猴子补丁抬高。
        _limit = (_env_int('WB_KEY_BUDGET_PENDING_QUOTA', 1) if limited
                  else _env_int('WB_KEY_BUDGET_PENDING', 4))
        if pending['n'] >= _limit:
            raise BudgetRejected('该密钥存在进行中或用量待核对的请求，请完成后再试', 'budget_pending')
        q, used = int(fresh['quota']), int(fresh['used_tokens'])
        qc = ledger.units(fresh['quota_credit'])
        uc = fresh['used_credit_units'] if fresh['used_credit_units'] is not None else ledger.units(fresh['used_credit'])
        # Conservative byte estimate, with framing headroom; images use extra room.
        encoded = (encoded_body if isinstance(encoded_body, bytes) else
                   json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        estimated_input = len(encoded) + 2048
        if b'"image_url"' in encoded: estimated_input += 32768
        output_field = 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
        requested = body.get(output_field)
        if requested is not None and (isinstance(requested,bool) or not isinstance(requested,int) or requested <= 0):
            raise BudgetRejected('max_tokens 必须为正整数', 'invalid_max_tokens')
        output = requested or 4096
        if fresh['daily_token_quota'] or fresh['daily_credit_quota']:
            from . import dailyquota
            now = int(time.time())
            day = db.day_of(now)
            daily = dailyquota.snapshot(key['id'], day)
            start = int(time.mktime(time.strptime(day, '%Y-%m-%d')))
            end = int(time.mktime(time.strptime(str(date.fromisoformat(day) + timedelta(days=1)), '%Y-%m-%d')))
            # Recovery/replay updates updated_at, not the accounting day. Active
            # streams still carry over midnight until their terminal facts arrive.
            outstanding = db.query("SELECT b.state,b.reserved_tokens,b.charged_tokens FROM budget_reservations b "
                                   "WHERE b.key_id=? AND (b.state IN ('reserved','forwarded') OR "
                                   "(b.state='unknown' AND coalesce((SELECT l.ts FROM request_logs l "
                                   "WHERE l.request_id=b.request_id),b.created_at)>=? AND "
                                   "coalesce((SELECT l.ts FROM request_logs l WHERE l.request_id=b.request_id),"
                                   "b.created_at)<?))", (key['id'], start, end))
            held = sum(max(0, int(r['reserved_tokens']) - int(r['charged_tokens'])) for r in outstanding)
            if fresh['daily_token_quota']:
                remaining_daily = int(fresh['daily_token_quota']) - int(daily['tokens']) - held
                if remaining_daily <= estimated_input:
                    raise BudgetRejected('今日 Token 额度不足，次日按服务器时区重置', 'daily_token_quota_exhausted')
                output = min(output, remaining_daily - estimated_input)
                body[output_field] = output
            if fresh['daily_credit_quota']:
                if any(r['state'] == 'unknown' for r in outstanding):
                    raise BudgetRejected('今日有未确认积分消耗，请先核对后继续', 'daily_credit_unconfirmed')
                if int(daily['credit_units']) >= ledger.units(fresh['daily_credit_quota']):
                    raise BudgetRejected('今日积分额度已用尽，次日按服务器时区重置', 'daily_credit_quota_exhausted')
        if q:
            remaining = q - used
            if remaining <= estimated_input:
                raise BudgetRejected('剩余 token 预算不足以覆盖当前输入估算', 'insufficient_quota')
            output = min(output, remaining - estimated_input)
            body[output_field] = output
        # C-008 修复（2026-10-27 审计 S1）：预估 token 量必须**无条件**记录。
        # 此前只在密钥设了 token 配额时才赋值，无配额密钥恒写 0 —— 实测全表
        # SUM(reserved_tokens)=0（63,805 行），于是崩溃或未结算时 unknown 行没有任何
        # 量级信息，"不确定消耗"永远估不出来，这道预留只剩并发计数作用。
        tokens = estimated_input + output
        # reserved_credit 仍写 0：本项目**没有本地定价表**，无法把 token 估成金额。
        # 真实扣费由上游返回后写入 charged_credit，这里不编造数字。
        credit = 0.0
        if qc:
            # 「积分是否用尽」单独判断，不再复用待写入字段（原实现把
            # 「剩余额度」塞进 reserved_credit，语义是错的）。
            remaining_credit = max(0, qc - uc) / ledger.SCALE
            if remaining_credit <= 0:
                raise BudgetRejected('积分预算已用尽', 'credit_quota_exhausted')
        now = int(time.time())
        db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (rid,key['id'],now,'reserved',tokens,credit,now,budgetowner.identity(),1,now+60))
    state.budget_reserved = True
    state.budget_fence = 1

def forwarded():
    state = observability.current.get()
    if state and state.budget_reserved:
        with db.transaction() as conn:
            changed = conn.execute("UPDATE budget_reservations SET state='forwarded',updated_at=? WHERE request_id=? AND state='reserved' AND owner=? AND fence=?", (int(time.time()),state.request_id,budgetowner.identity(),state.budget_fence)).rowcount
            if changed != 1:
                raise BudgetRejected('预算预留归属已变化，禁止转发', 'budget_fenced')

def settle(usage, pt, ct, credit):
    """Runs in the same transaction as request log and key usage updates."""
    state = observability.current.get()
    if state is None or not state.budget_reserved: return
    row = db.query_one('SELECT state,owner,fence FROM budget_reservations WHERE request_id=?', (state.request_id,))
    if row is None or row['state'] in ('settled','released'): return
    if not _owned(row, state):
        raise BudgetRejected('预算预留已被恢复进程隔离', 'budget_fenced')
    fresh = db.query_one('SELECT quota_credit,daily_credit_quota FROM api_keys WHERE id=?', (state.key_id,))
    known = state.terminal_state == 'completed' and isinstance(usage,dict) and all(k in usage for k in ('prompt_tokens','completion_tokens'))
    if fresh and (fresh['quota_credit'] or fresh['daily_credit_quota']) and credit is None: known = False
    next_state = 'settled' if known else 'released' if row['state']=='reserved' else 'unknown'
    db.execute('UPDATE budget_reservations SET state=?,charged_tokens=?,charged_credit=?,updated_at=? WHERE request_id=?',
               (next_state,pt+ct,credit or 0,int(time.time()),state.request_id))

def finish(state):
    if not state.budget_reserved: return
    db.execute("UPDATE budget_reservations SET state=CASE WHEN state='reserved' THEN 'released' ELSE 'unknown' END,updated_at=? "
               "WHERE request_id=? AND owner=? AND fence=? AND state IN ('reserved','forwarded')", (int(time.time()),state.request_id,budgetowner.identity(),state.budget_fence))

def unresolved(key_id=None):
    args = () if key_id is None else (key_id,)
    clause = '' if key_id is None else ' AND key_id=?'
    return [dict(r) for r in db.query("SELECT * FROM budget_reservations WHERE state IN ('reserved','forwarded','unknown')" + clause + ' ORDER BY created_at,request_id LIMIT 1000', args)]


def unresolved_batches(key_id=None, batch_size=500):
    """Scan all unresolved rows without OFFSET skips or an unbounded result list."""
    batch_size = max(1, min(500, int(batch_size)))
    base = "state IN ('reserved','forwarded','unknown')"
    args = () if key_id is None else (key_id,)
    scope = '' if key_id is None else ' AND key_id=?'
    last = db.query_one('SELECT created_at,request_id FROM budget_reservations WHERE '
                        + base + scope + ' ORDER BY created_at DESC,request_id DESC LIMIT 1', args)
    if last is None:
        return
    ceiling = (last['created_at'], last['request_id'])
    cursor = None
    while True:
        clauses = [base, '(created_at,request_id) <= (?,?)']
        args = list(ceiling)
        if key_id is not None:
            clauses.append('key_id=?')
            args.append(key_id)
        if cursor is not None:
            clauses.append('(created_at,request_id) > (?,?)')
            args.extend(cursor)
        rows = [dict(r) for r in db.query(
            'SELECT * FROM budget_reservations WHERE ' + ' AND '.join(clauses) +
            ' ORDER BY created_at,request_id LIMIT ?', (*args, batch_size))]
        if not rows:
            return
        cursor = (rows[-1]['created_at'], rows[-1]['request_id'])
        yield rows


def iter_unresolved(key_id=None):
    for rows in unresolved_batches(key_id):
        yield from rows


def _unresolved_with_logs(key_id=None):
    for rows in unresolved_batches(key_id):
        logs = _logs_for([row['request_id'] for row in rows])
        for row in rows:
            yield row, logs.get(row['request_id'])


def usage_delta(tokens, credit, charged_tokens=0, charged_credit=0, *, allow_negative=False):
    """Compute integer deltas against durable charged evidence, never a scan snapshot."""
    td = tokens - int(charged_tokens or 0)
    cd_units = ledger.units(credit) - (ledger.units(charged_credit or 0) or 0)
    if not allow_negative and (td < 0 or cd_units < 0):
        raise ValueError(
            '该请求已有计费用量（charged_tokens=%d / charged_credit=%s），'
            '不能用更小的数值覆盖；确需下调请显式声明 allow_negative'
            % (charged_tokens or 0, charged_credit or 0))
    return td, cd_units


def charge_delta(key_id, token_delta, credit_units_delta, ts, *, allow_negative=False):
    """Apply one reconciled delta inside the caller's ledger transaction."""
    if not allow_negative and (token_delta < 0 or credit_units_delta < 0):
        raise ValueError('negative accounting delta requires explicit approval')
    from . import dailyquota
    dailyquota.charge(key_id, token_delta, str(Decimal(credit_units_delta) / ledger.SCALE), ts)
    db.execute('UPDATE api_keys SET used_tokens=max(0,used_tokens+?),used_credit_units=max(0,coalesce(used_credit_units,round(used_credit*1000000))+?) WHERE id=?',
               (token_delta, credit_units_delta, key_id))
    db.execute('UPDATE api_keys SET used_credit=used_credit_units*1.0/? WHERE id=?',
               (ledger.SCALE, key_id))


def _confirmed_zero_usage(request_id, key_id) -> bool:
    """Require complete request-level provider evidence, not missing charges."""
    return _zero_usage_proof(_logs_for([request_id]).get(request_id), key_id)


def _zero_usage_proof(row, key_id) -> bool:
    if (row is None or row['key_id'] != key_id or row['usage_source'] != 'upstream'
            or row['usage_complete'] != 1 or row['prompt_tokens'] != 0
            or row['completion_tokens'] != 0 or row['credit'] is None
            or Decimal(str(row['credit'])) != 0):
        return False
    return row['usage_event_count'] == 1 and row['zero_event_count'] == 1


def resolve(request_id, tokens, credit, actor, reason, *, zero_only=False,
            expect_charged_zero=False, allow_negative=False):
    """把一条 unknown 预留核对结清（写 charged_* + 增量调整 + 审计）。

    ## N05（2026-10-28，P1）：扫描与结清之间必须重新核对

    调用方通常是「先扫描、再结清」的两段式（自动对账、定时脚本、面板端点）。
    扫描读到 `charged_tokens=0 / charged_credit=0` 之后、真正进到事务之前，
    **可能有晚到的部分用量落库**（回执重放 / 迟到的结算 / 另一实例的写入）。
    旧实现用 `zero_only=False` 时**不检查 charged**，于是：

    ```
    td = 0 - 50 = -50            # 用"实际 0"覆盖已记的 50
    cd_units = 0 - 1_000_000     # 金额同样被抹掉
    ```

    再写进 `budget_adjustments` / `adjustment_events` —— 那不是"结清"，而是
    **伪造账并把已消耗的配额退回去**。

    两道闸（都在 `BEGIN IMMEDIATE` 之后、任何写入之前）：

    * `expect_charged_zero=True`：调用方声明"我读到 charged 为 0"。事务内重读后
      若 charged 已非 0 → **返回 False**（跳过，保留 unknown 给人看），不写任何东西；
    * `allow_negative`（**默认 False**）：算出来的 `td` / `cd_units` 为负 → 抛
      `ValueError`。默认拒绝，意味着**现有调用方不改也不会再抹掉用量**：
      面板端点把它转成 409（带原因），定时脚本按失败计数并保留该行。
      确实需要"下调"的核对（管理员判定实际用量更小）必须显式
      `allow_negative=True` —— 把"覆盖已记用量"变成一次**主动声明**而不是默认行为。

    自动对账（含旧定时脚本）和 zero_only 还必须在事务内重读完整的零用量回执。
    零预扣、超期、拒绝错误字符串或未采集到 attempt 都不能证明没有计费。
    """
    if tokens < 0 or credit < 0 or not math.isfinite(credit) or not reason.strip():
        raise ValueError('需要非负实际用量和核对原因')
    with db.transaction():
        row = db.query_one('SELECT * FROM budget_reservations WHERE request_id=?', (request_id,))
        if row is None: raise ValueError('预留不存在')
        if row['state']=='settled': return False
        if row['state']!='unknown': raise ValueError('仅允许核对 unknown 请求，进行中请求不可手动释放')
        # Recheck under BEGIN IMMEDIATE: a stale scan must never erase uncertain usage.
        if zero_only and any(row[field] or 0 for field in ('reserved_tokens', 'reserved_credit', 'charged_tokens', 'charged_credit')):
            return False
        # N05 闸 1：事务内重读「已记用量」，与调用方的假设不符就跳过。
        charged_tokens = int(row['charged_tokens'] or 0)
        charged_credit_units = ledger.units(row['charged_credit'] or 0) or 0
        if expect_charged_zero and (charged_tokens or charged_credit_units):
            _log.warning('budget resolve skipped: late usage appeared under the lock '
                         'request_id=%s charged_tokens=%d charged_credit=%s',
                         request_id, charged_tokens, row['charged_credit'])
            return False
        td, cd_units = usage_delta(tokens, credit, charged_tokens, row['charged_credit'],
                                   allow_negative=allow_negative)
        if actor == 'auto-reconcile' or zero_only:
            if tokens != 0 or ledger.units(credit) != 0 or not _confirmed_zero_usage(request_id, row['key_id']):
                return False
        cd = cd_units / ledger.SCALE
        usage_log = db.query_one('SELECT ts FROM request_logs WHERE request_id=?', (request_id,))
        charge_delta(row['key_id'], td, cd_units, usage_log['ts'] if usage_log else row['created_at'],
                     allow_negative=allow_negative)
        db.execute('INSERT INTO budget_adjustments VALUES(?,?,?,?,?,?,?)', (request_id,row['key_id'],int(time.time()),td,cd,actor[:128],reason[:500]))
        ledger.adjust(request_id, row['key_id'], td, cd, actor, reason)
        db.execute("UPDATE budget_reservations SET state='settled',charged_tokens=?,charged_credit=?,resolution=?,updated_at=? WHERE request_id=?", (tokens,credit,reason[:500],int(time.time()),request_id))
        db.add_audit_log(actor,'budget.resolve',request_id,reason[:500])
    return True

# ── U02（2026-10-28）：unknown 的分类、下界展示与「有证据才结清」 ────────────
# 错误分类只用于诊断，不能充当计费结论。自动结清另须完整零用量回执。
#
# 分类口径（互斥，按优先级）：
#   charged_partial   已有 charged_tokens/credit > 0 —— 有部分计费，保留
#   usage_present     request_logs.usage_source=upstream 或 credit 非 NULL —— 有用量，保留
#   client_cancelled  499 / client_cancelled —— 可能已部分计费，保留
#   upstream_rejected 上游拒绝且未回 usage —— 仍待核对，不能猜作零用量
#   no_request_log    没有请求日志 —— 证据不足，保留
#   unexplained       其他 —— 保留
UNKNOWN_CLASSES = ('charged_partial', 'usage_present', 'client_cancelled',
                   'upstream_rejected', 'no_request_log', 'unexplained')

#: 只认「请求没被上游处理」这一类标识；超时/断流不在其中。
_REJECTED_MARKERS = ('no_healthy_account', '"code":11102', '"code": 11102',
                     'model_not_found', 'service info not found')

_log = logging.getLogger('workbuddy.budget')


def _classify(row, log) -> str:
    if any(row.get(field) or 0 for field in ('charged_tokens', 'charged_credit')):
        return 'charged_partial'
    if log is None:
        return 'no_request_log'
    if (log['usage_source'] or '') == 'upstream' or log['credit'] is not None:
        return 'usage_present'
    status = int(log['status'] or 0)
    terminal = log['terminal_state'] or ''
    if terminal == 'client_cancelled' or status == 499:
        return 'client_cancelled'
    error = str(log['error'] or '')
    if status >= 500 and any(marker in error for marker in _REJECTED_MARKERS):
        return 'upstream_rejected'
    return 'unexplained'


def _logs_for(request_ids) -> dict:
    ids = [rid for rid in request_ids if rid]
    if not ids:
        return {}
    placeholders = ','.join('?' * len(ids))
    rows = db.query('SELECT l.request_id,l.status,l.terminal_state,l.usage_source,l.credit,l.error,'
                    'l.key_id,l.prompt_tokens,l.completion_tokens,l.usage_complete,'
                    'count(e.event_id) usage_event_count,'
                    'sum(CASE WHEN e.key_id=l.key_id AND e.prompt_tokens=0 AND e.completion_tokens=0 '
                    "AND e.credit_units=0 AND e.provenance='provider-reported' THEN 1 ELSE 0 END) zero_event_count "
                    'FROM request_logs l LEFT JOIN usage_events e ON e.request_id=l.request_id '
                    "AND e.event_type='usage' WHERE l.request_id IN (" + placeholders + ') GROUP BY l.request_id', tuple(ids))
    return {r['request_id']: r for r in rows}


# ── U02 收尾（2026-10-28）：待核对终态的**处理期限**与「已归档待核对」──────
# 现场：生产只剩 1 条 unknown（client_cancelled，可能已部分计费），它**不能**
# 自动结清；但它会让 budget_unknown 告警长期 firing，把真正需要人看的信号稀释。
# 审计验收要求「unknown 无上游证据时允许保留在待核对终态，并建立处理期限」。
#
# 期限语义：创建超过 WB_BUDGET_REVIEW_DEADLINE_DAYS（默认 7 天）仍**没有上游
# 证据**的 unknown → 标记「已归档待核对」：
#   · 不改 state（仍是 unknown）、不删除 —— 随时可人工 resolve；
#   · 不再计入 needs_human_old（告警只看真正待处理的），单独计入
#     archived_pending / archived_pending_tokens（下界）；
#   · 归档幂等（按 resolution 前缀判定），留痕（resolution + audit_logs），
#     **不改用量**、不动 updated_at（避免把旧行重新算成「在途」）。
#
# 只归档「无上游证据」的三类：charged_partial / usage_present 有用量证据、
# upstream_rejected 仍需核对 —— 三者都不归档。
ARCHIVE_RESOLUTION_PREFIX = '已归档待核对：'
#: 没有任何上游用量证据的分类（可归档）。有证据的一律留在 needs_human。
EVIDENCELESS_CLASSES = ('client_cancelled', 'no_request_log', 'unexplained')
REVIEW_DEADLINE_DAYS_DEFAULT = 7


def review_deadline_days() -> int:
    """处理期限（天）。C-004：阈值源码直读 env，不藏在带外补丁里。"""
    return _env_int('WB_BUDGET_REVIEW_DEADLINE_DAYS', REVIEW_DEADLINE_DAYS_DEFAULT)


def _is_archived(row) -> bool:
    """是否已标记「已归档待核对」（只看留痕字段，不看 state）。"""
    return str(row.get('resolution') or '').startswith(ARCHIVE_RESOLUTION_PREFIX)


def _mark_archived(request_id: str, reason: str) -> bool:
    """幂等写入归档留痕；只有「仍是 unknown 且尚未归档」时才算改动。

    刻意**不更新 updated_at**：`prepare()` 用 updated_at 判断 unknown 是否
    「15 分钟内仍在途」，把 7 天前的老行刷成现在会让它重新阻塞该密钥的新请求。
    """
    with db.transaction() as conn:
        changed = conn.execute(
            "UPDATE budget_reservations SET resolution=? "
            "WHERE request_id=? AND state='unknown' AND "
            "(resolution IS NULL OR resolution NOT LIKE ?)",
            (reason[:500], request_id, ARCHIVE_RESOLUTION_PREFIX + '%')).rowcount
    return changed == 1


def archive_overdue_unknowns(now: int, deadline_days: int | None = None) -> int:
    """把超过处理期限仍无上游证据的 unknown 标记「已归档待核对」；返回条数。

    幂等：已归档的行第二次进来直接跳过（返回 0），不会重复留痕。
    """
    now = int(now)
    days = max(1, int(deadline_days) if deadline_days is not None
               else review_deadline_days())
    horizon = now - days * 86400
    archived = 0
    for row, log in _unresolved_with_logs():
        if row.get('state') != 'unknown' or _is_archived(row):
            continue
        created = int(row.get('created_at') or now)
        if created > horizon:
            continue                      # 未到处理期限：继续留在待处理
        cls = _classify(row, log)
        if cls not in EVIDENCELESS_CLASSES:
            continue                      # 有用量证据 / 可自动结清：不归档
        reason = ('%s超过 %d 天处理期限仍无上游证据（分类 %s，创建于 %s）；'
                  '保留 state=unknown 供人工核对，未改用量、未删除'
                  % (ARCHIVE_RESOLUTION_PREFIX, days, cls,
                     time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created))))
        try:
            if not _mark_archived(row['request_id'], reason):
                continue
        except Exception:  # noqa: BLE001 - 单条失败不中断本轮
            _log.exception('archive overdue unknown failed request_id=%s',
                           row.get('request_id'))
            continue
        archived += 1
        try:
            db.add_audit_log('auto-archive', 'budget.archive', row['request_id'], reason)
        except Exception:  # noqa: BLE001 - 留痕失败不能撤销归档事实
            _log.exception('archive audit log failed request_id=%s', row.get('request_id'))
    if archived:
        _log.warning('archived %d overdue unknowns as pending-review (deadline %d days)',
                     archived, days)
    return archived


def unresolved_summary(key_id=None, now=None, stale_after=3600) -> dict:
    """unknown 的分类汇总与下界（U02）。

    下界语义：`reserved_tokens` 是**预留估算**（输入字节 + max_tokens），不是实际
    用量；`credit` 没有任何本地定价表，金额下界只能是 0 —— 不编造。两个数分开
    标注，避免把估算当成账单。

    U02 收尾：`archived_pending` 是「超过处理期限仍无上游证据、已标记归档待核对」
    的条数 —— 它们仍是 unknown、仍可人工 resolve，但**不再计入 needs_human_old**，
    因此不再触发 budget_unknown 告警。归档行若后来拿到用量证据（迟到日志），
    分类会变成 charged_partial/usage_present，自动重新计入 needs_human（宁可重报）。
    """
    now = int(time.time()) if now is None else int(now)
    days = review_deadline_days()
    items = []
    by_class = {name: 0 for name in UNKNOWN_CLASSES}
    by_state = {state: 0 for state in ('reserved', 'forwarded', 'unknown')}
    total = unknown_total = needs_human = needs_human_old = auto_reconcilable = 0
    archived_pending = archived_pending_tokens = token_lower_bound = 0
    for row, log in _unresolved_with_logs(key_id):
        cls = _classify(row, log)
        by_class[cls] = by_class.get(cls, 0) + 1
        total += 1
        by_state[row['state']] += 1
        archived = bool(row['state'] == 'unknown' and cls in EVIDENCELESS_CLASSES
                        and _is_archived(row))
        if len(items) < 1000:
            items.append({**row, 'class': cls, 'archived': archived})
        if row['state'] != 'unknown':
            continue
        unknown_total += 1
        confirmed_zero = (not any(row.get(field) or 0 for field in ('charged_tokens', 'charged_credit'))
                          and _zero_usage_proof(log, row['key_id']))
        auto_reconcilable += int(confirmed_zero)
        reserved = int(row.get('reserved_tokens') or 0)
        token_lower_bound += reserved
        if archived:
            archived_pending += 1
            archived_pending_tokens += reserved
        elif not confirmed_zero:
            needs_human += 1
            needs_human_old += int(int(row.get('created_at') or now) < now - stale_after)
    return {
        'items': items,
        'items_truncated': total > len(items),
        'items_limit': 1000,
        'summary': {
            'total': total,
            'unknown_total': unknown_total,
            'by_state': by_state,
            'by_class': by_class,
            'auto_reconcilable': auto_reconcilable,
            'needs_human': needs_human,
            'needs_human_old': needs_human_old,
            'archived_pending': archived_pending,
            'archived_pending_tokens': archived_pending_tokens,
            'review_deadline_days': days,
            'token_lower_bound': token_lower_bound,
            'credit_lower_bound': 0.0,
            'bounds_note': ('token_lower_bound 是预留估算（输入字节+max_tokens），不是实际用量；'
                            'credit_lower_bound 恒为 0 —— 本地没有定价表，不编造金额。'
                            '真实值只有上游回 usage 才知道。'),
            'archive_note': ('archived_pending 是超过 review_deadline_days 天仍无上游证据、'
                             '已标记「已归档待核对」的 unknown：state 未改、未删除、'
                             '仍可人工 resolve，只是不再计入 needs_human_old 告警；'
                             'archived_pending_tokens 同样是预留估算下界，不是账单。'),
        },
    }


#: 「attempt 到达过上游」的阶段集合（N05）。
#:
#: **与 `ledger.py::_REACHED_PHASES` 同源** —— 那张表是 `ledger.attempt()` 写的，
#: 阶段取值由它定义。这里 import 私有名而不是抄一份字面量，避免两处漂移
#: （`test_late_usage_race.py` 里有一条断言两者相等）。
_UPSTREAM_PHASES = tuple(getattr(ledger, '_REACHED_PHASES', ()) or ())


def _reached_upstream(request_id: str) -> bool:
    """该请求是否有 attempt 到达过上游（N05 的证据加强）。

    为什么错误字符串不够：`error` 里出现 `no_healthy_account` 只证明**至少有一次**
    上游拒绝（或选号失败），不证明"所有 attempt 都没被处理"。池侧有重试/换号时，
    attempt 2 可能已经打到上游并产生部分计费，而最终错误仍然是 attempt 3 的
    `no_healthy_account`。所以结清前必须再确认：`attempt_events` 里**没有任何**
    到达上游的阶段。

    局限（写清楚，不夸大）：`attempt_events` 由上游日志轮询异步采集（约 15 秒一轮）。
    **没有事件**只是"没有证据表明到达过上游"，不是"证明没到达"。
    此函数仅供诊断，自动结清不再把事件缺失当作零用量证据。
    """
    if not _UPSTREAM_PHASES:
        return False
    placeholders = ','.join('?' * len(_UPSTREAM_PHASES))
    row = db.query_one(
        'SELECT 1 FROM attempt_events WHERE request_id=? AND phase IN (%s) LIMIT 1'
        % placeholders, (request_id, *_UPSTREAM_PHASES))
    return row is not None


def reconcile_evidenced_unknowns(now: int, min_age: int = 3600) -> int:
    """Settle only complete, immutable request-level zero-usage evidence."""
    now = int(now)
    settled = 0
    for row, log in _unresolved_with_logs():
        if row.get('state') != 'unknown':
            continue
        if int(row.get('created_at') or now) > now - min_age:
            continue
        if (any(row.get(field) or 0 for field in ('charged_tokens', 'charged_credit'))
                or not _zero_usage_proof(log, row['key_id'])):
            continue
        try:
            if resolve(row['request_id'], 0, 0.0, 'auto-reconcile',
                       '完整上游回执与不可变用量事件均确认零用量，自动核对结清',
                       zero_only=False, expect_charged_zero=True, allow_negative=False):
                settled += 1
        except Exception:  # noqa: BLE001 - 单条失败不中断本轮
            _log.exception('evidenced unknown reconcile failed request_id=%s',
                           row.get('request_id'))
    return settled
