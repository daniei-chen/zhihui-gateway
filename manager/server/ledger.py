"""Durable events are authoritative; legacy REAL summaries remain UI projections.

Amounts use millionths of a provider credit, rounded half-even. Unknown is NULL.
No prompt, credential, URL or provider error text is retained in these events.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import logging
import time

_log = logging.getLogger('workbuddy.ledger')

SCALE = 1_000_000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS usage_events (
 event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 event_type TEXT NOT NULL, ts INTEGER NOT NULL, key_id INTEGER, model TEXT,
 realm TEXT, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
 credit_units INTEGER, counted INTEGER NOT NULL, terminal_state TEXT,
 provenance TEXT NOT NULL, release_id TEXT,
 UNIQUE(request_id,attempt,event_type)
);
CREATE INDEX IF NOT EXISTS idx_usage_events_key_ts ON usage_events(key_id,ts);
CREATE TABLE IF NOT EXISTS forwarding_events (
 request_id TEXT NOT NULL, attempt INTEGER NOT NULL, phase TEXT NOT NULL,
 ts INTEGER NOT NULL, upstream_id TEXT, requested_model TEXT, resolved_model TEXT,
 realm TEXT, protocol TEXT, transform TEXT, config_hash TEXT, outcome TEXT,
 PRIMARY KEY(request_id,attempt,phase)
);
CREATE TABLE IF NOT EXISTS adjustment_events (
 event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, ts INTEGER NOT NULL,
 key_id INTEGER NOT NULL, token_delta INTEGER NOT NULL, credit_units_delta INTEGER NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, request_id TEXT,
 actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL,
 status INTEGER, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt_events (
 request_id TEXT NOT NULL, attempt INTEGER NOT NULL, phase TEXT NOT NULL,
 ts INTEGER NOT NULL, account_uid TEXT NOT NULL, resolved_model TEXT NOT NULL,
 realm TEXT NOT NULL, status INTEGER NOT NULL, config_hash TEXT NOT NULL,
 PRIMARY KEY(request_id,attempt,phase)
);
CREATE TRIGGER IF NOT EXISTS attempt_events_no_update BEFORE UPDATE ON attempt_events
 BEGIN SELECT RAISE(ABORT,'attempt events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attempt_events_no_delete BEFORE DELETE ON attempt_events
 BEGIN SELECT RAISE(ABORT,'attempt events are immutable'); END;
-- U04（2026-10-28）：attempt 级「有无计费事实」。三态 + 来源，**不含任何金额**。
-- 单独建表而不是给 attempt_events 加列：attempt_events 有 no_update/no_delete
-- 触发器，加列只能 ALTER（会重写 11 万行且不可回滚）；而且事实是**后到**的
-- （池侧 attempt 事件靠日志轮询采集），必须允许「同一 attempt 只写一次、写晚不写错」。
CREATE TABLE IF NOT EXISTS attempt_billing_facts (
 request_id TEXT NOT NULL, attempt INTEGER NOT NULL, ts INTEGER NOT NULL,
 billing_fact TEXT NOT NULL, billing_source TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(request_id, attempt)
);
CREATE TRIGGER IF NOT EXISTS attempt_billing_facts_no_update BEFORE UPDATE ON attempt_billing_facts
 BEGIN SELECT RAISE(ABORT,'attempt billing facts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attempt_billing_facts_no_delete BEFORE DELETE ON attempt_billing_facts
 BEGIN SELECT RAISE(ABORT,'attempt billing facts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS usage_events_no_update BEFORE UPDATE ON usage_events
 BEGIN SELECT RAISE(ABORT,'usage events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS usage_events_no_delete BEFORE DELETE ON usage_events
 BEGIN SELECT RAISE(ABORT,'usage events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjustment_events_no_update BEFORE UPDATE ON adjustment_events
 BEGIN SELECT RAISE(ABORT,'adjustment events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjustment_events_no_delete BEFORE DELETE ON adjustment_events
 BEGIN SELECT RAISE(ABORT,'adjustment events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS admin_events_no_update BEFORE UPDATE ON admin_events
 BEGIN SELECT RAISE(ABORT,'admin events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS admin_events_no_delete BEFORE DELETE ON admin_events
 BEGIN SELECT RAISE(ABORT,'admin events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forwarding_events_no_update BEFORE UPDATE ON forwarding_events
 BEGIN SELECT RAISE(ABORT,'forwarding events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forwarding_events_no_delete BEFORE DELETE ON forwarding_events
 BEGIN SELECT RAISE(ABORT,'forwarding events are immutable'); END;
-- U01（2026-10-28）：完成回执。request_logs 事务失败时事实先落 spool，重放时用
-- 这张表去重；receipt_id = request_id（无 request_id 的极少数路径用 spool 文件名）。
-- 它不属于不可变审计链，只是幂等闸门，因此不加 no_update/no_delete 触发器。
CREATE TABLE IF NOT EXISTS ledger_receipts (
 receipt_id TEXT PRIMARY KEY, request_id TEXT, attempt INTEGER NOT NULL DEFAULT 1,
 applied_at INTEGER NOT NULL, source TEXT NOT NULL, release_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_receipts_request ON ledger_receipts(request_id);
'''

def units(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('invalid credit')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) > Decimal('9000000000000'):
            raise ValueError('credit out of range')
        return int((amount * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('invalid credit') from exc

def record(request_id, key_id, model, realm, pt, ct, credit, counted, terminal, release):
    """写**请求级**用量事实（usage_events 一行）。

    U04（2026-10-28）：`attempt` 列恒为 `REQUEST_LEVEL_ATTEMPT`（1）是**有意**的
    —— usage_events 记的是「这次逻辑请求被上游计了多少」，不是某个 attempt 的
    账单。请求内部可能发生多次 provider attempt，但上游只回一份请求级 usage；
    按 attempt 拆分会把同一笔用量重复计费，也会编造出并不存在的 per-attempt 金额。
    attempt 级「有无计费证据」另存 `attempt_billing_facts`（见本模块 U04 段）。
    """
    from . import db
    if not request_id:
        return
    db.execute('INSERT OR IGNORE INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (f'{request_id}:{REQUEST_LEVEL_ATTEMPT}:usage', request_id,
                REQUEST_LEVEL_ATTEMPT, 'usage', int(time.time()), key_id,
                model, realm, pt, ct, units(credit), int(counted), terminal,
                'provider-reported' if credit is not None else 'unknown-credit', release))

def attempt(value):
    """Collect explicit gateway request IDs; never infer a correlation from time."""
    import re
    from . import db
    if not isinstance(value,dict) or value.get('event')!='workbuddy_attempt':return False
    rid=value.get('request_id');number=value.get('attempt');phase=value.get('phase')
    phases={'selected','capacity_lost','refresh_failed','refresh_write_failed','upstream_headers','stream_terminal','aggregate_terminal'}
    if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',rid):return False
    if isinstance(number,bool) or not isinstance(number,int) or not 1<=number<=32 or phase not in phases:return False
    uid=value.get('account_uid');model=value.get('resolved_model');realm=value.get('realm');status=value.get('status');config_hash=value.get('config_hash','')
    if not all(isinstance(v,str) and len(v)<=128 and not any(ord(c)<32 for c in v) for v in (uid,model)) or realm not in ('cn','global'):return False
    if isinstance(status,bool) or not isinstance(status,int) or not 0<=status<=599:return False
    if not isinstance(config_hash,str) or config_hash and not re.fullmatch(r'[a-f0-9]{64}',config_hash):return False
    with db.transaction():
        db.execute('INSERT OR IGNORE INTO attempt_events VALUES(?,?,?,?,?,?,?,?,?)',
                   (rid,number,phase,int(time.time()),uid,model,realm,status,config_hash))
        if uid:
            db.execute("UPDATE request_logs SET account=?,account_source='request_id',account_attempt=? WHERE request_id=? AND coalesce(account_attempt,0)<=?",
                       (uid,number,rid,number))
    return True

def forwarding(state, phase, outcome=None):
    from . import db
    db.execute('INSERT OR IGNORE INTO forwarding_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (state.request_id,1,phase,int(time.time()),getattr(state,'upstream_id','default'),
                getattr(state,'requested_model',None),getattr(state,'resolved_model',None),
                getattr(state,'realm',None),state.protocol,getattr(state,'transform',None),
                getattr(state,'config_hash',None),outcome))

def adjust(request_id, key_id, td, cd, actor, reason):
    from . import db
    db.execute('INSERT INTO adjustment_events VALUES(?,?,?,?,?,?,?,?)',
               (request_id + ':reconcile', request_id, int(time.time()), key_id, td,
                units(cd), str(actor)[:128], str(reason)[:500]))

def admin(actor, action, target='', outcome='completed', status=None, request_id=None):
    from . import db
    # Transaction serializes the hash chain across processes. This detects accidental
    # corruption; host administrators can rewrite SQLite and are outside this boundary.
    with db.transaction():
        previous = db.query_one('SELECT event_hash FROM admin_events ORDER BY id DESC LIMIT 1')
        prev = previous['event_hash'] if previous else '0' * 64
        fields = [int(time.time()), request_id, str(actor or 'anonymous')[:128],
                  str(action)[:128], str(target)[:256], outcome, status, prev]
        digest = hashlib.sha256(json.dumps(fields, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        db.execute('INSERT INTO admin_events(ts,request_id,actor,action,target,outcome,status,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)', (*fields, digest))

def verify_chain():
    from . import db
    prev = '0' * 64
    for row in db.query('SELECT * FROM admin_events ORDER BY id'):
        fields = [row[k] for k in ('ts','request_id','actor','action','target','outcome','status','previous_hash')]
        digest = hashlib.sha256(json.dumps(fields, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if row['previous_hash'] != prev or row['event_hash'] != digest:
            return False
        prev = digest
    return True


# ── U04（2026-10-28）：attempt 级「有无计费事实」────────────────────────────
#
# 审计口径：「区分逻辑请求与 provider attempts；取得池侧回执和关联 ID 后再计
# per-attempt；缺失证据单列，不合成虚假的 token/费用」。
#
# 现状（生产实测 2026-10-28）：`attempt_events` 有 111,724 行阶段事件
# （selected 39293 / upstream_headers 39273 / stream_terminal 32843 /
# aggregate_terminal 320），但**没有任何计费字段**；`usage_events` 是请求级；
# `record()` 的 attempt 列恒为 1。于是「这次请求用了几个 attempt、每个有没有
# 计费证据」在库里无法回答。
#
# 本段只做**可验证**的部分：
#   1. 三态 `reported` / `unknown` / `not_applicable` + 来源
#      `provider_receipt` / `absent`，落到不可变的 `attempt_billing_facts`；
#   2. 事实**只由既有不可变事实推导**（attempt_events 阶段 + 请求级 usage 证据），
#      缺证据一律记 unknown，绝不按比例/按次数估金额；
#   3. per-attempt **金额**不做：本地没有定价表，池侧也没有 per-attempt 回执与
#      关联 ID（见 PER_ATTEMPT_BILLING_REQUIREMENTS）。usage_events 仍只记请求级，
#      不会因为 attempt 变多而重复计费。
BILLING_REPORTED = 'reported'
BILLING_UNKNOWN = 'unknown'
BILLING_NOT_APPLICABLE = 'not_applicable'
#: 三态取值；顺序即「证据强度」由强到弱，报告里按此排列。
BILLING_FACTS = (BILLING_REPORTED, BILLING_UNKNOWN, BILLING_NOT_APPLICABLE)
BILLING_SOURCE_RECEIPT = 'provider_receipt'
BILLING_SOURCE_ABSENT = 'absent'
#: usage_events 是请求级事实，attempt 列恒为该值（不按 attempt 拆分，避免重复计费）。
REQUEST_LEVEL_ATTEMPT = 1
#: attempt 已经到达上游的阶段（出现过其一即「可能产生费用」）。
_REACHED_PHASES = ('upstream_headers', 'stream_terminal', 'aggregate_terminal')
#: attempt 明确结束的阶段。
_TERMINAL_PHASES = ('stream_terminal', 'aggregate_terminal')
#: 事实落库前的稳定窗口：池侧 attempt 事件靠日志轮询采集（15s 一轮、可能滞后），
#: 立刻落库会把「2 个 attempt」的请求记成「1 个 attempt 已上报」，而事实表不可变。
ATTEMPT_BILLING_MIN_AGE_SEC = 600
#: per-attempt 计费**缺什么**——要池侧补齐这些才能做，缺一条就不做。
PER_ATTEMPT_BILLING_REQUIREMENTS = (
    '每次 attempt 的上游回执：上游侧 request/completion id（用于与账单关联）',
    'attempt 编号与逻辑请求 request_id 的对应关系（同一请求内的 attempt 序号）',
    '按 attempt 拆分的 usage 明细（prompt/completion/cache 命中与写入 token）',
    '计费金额或可复算的定价依据（模型、倍率、缓存折扣），以及币种与精度',
    '失败/取消 attempt 的显式「未计费」回执（否则只能记 unknown，不能记 0）',
    '回执幂等键与发生时间（防止重放导致同一 attempt 被重复计费）',
)


def _attempt_phases(request_id: str) -> dict:
    """{attempt: [phase, ...]}；只读 attempt_events，不做时间推断。"""
    from . import db
    out: dict[int, list[str]] = {}
    for row in db.query('SELECT attempt,phase FROM attempt_events WHERE request_id=? '
                        'ORDER BY attempt,ts', (request_id,)):
        out.setdefault(int(row['attempt']), []).append(str(row['phase']))
    return out


def _request_usage_evidence(request_id: str) -> dict:
    """请求级用量证据：只认 request_logs.usage_source / usage_events 的既有事实。"""
    from . import db
    log = db.query_one('SELECT usage_source,usage_complete,terminal_state,status,credit '
                       'FROM request_logs WHERE request_id=?', (request_id,))
    events = db.query('SELECT attempt,provenance,counted,credit_units '
                      'FROM usage_events WHERE request_id=? ORDER BY attempt', (request_id,))
    reported = bool(log is not None
                    and (log['usage_source'] or '') == 'upstream'
                    and int(log['usage_complete'] or 0) == 1)
    return {
        'reported': reported,
        'usage_source': (log['usage_source'] if log is not None else None),
        'usage_complete': int(log['usage_complete'] or 0) if log is not None else 0,
        'terminal_state': log['terminal_state'] if log is not None else None,
        'status': int(log['status'] or 0) if log is not None else None,
        'credit': log['credit'] if log is not None else None,
        'usage_events': len(events),
        'provenance': sorted({str(e['provenance']) for e in events}),
        'credit_units': sum(int(e['credit_units'] or 0) for e in events
                            if e['credit_units'] is not None),
    }


def derive_attempt_billing(request_id: str) -> list[dict]:
    """按既有不可变事实推导每个 attempt 的计费事实三态（**只读**，不含金额）。

    规则（缺证据一律 unknown，不猜、不按比例摊）：
      · 该 attempt 没有任何到达上游的阶段 → `not_applicable` / `absent`：
        请求没被上游处理，本来就不该有计费事实；
      · 请求级拿到上游 usage，且**本请求只有一个 attempt 到达过上游** →
        该 attempt = `reported` / `provider_receipt`（金额仍是请求级，不拆分）；
      · 其余（到达上游但无终态事件、请求级无 usage、或同一请求有多个 attempt
        到达上游而无法归属）→ `unknown` / `absent`。
    """
    phases = _attempt_phases(request_id)
    if not phases:
        return []
    evidence = _request_usage_evidence(request_id)
    reached = [n for n, items in phases.items()
               if any(p in _REACHED_PHASES for p in items)]
    unambiguous = len(reached) == 1
    out = []
    for number in sorted(phases):
        seen = phases[number]
        reached_upstream = number in reached
        if not reached_upstream:
            fact, source = BILLING_NOT_APPLICABLE, BILLING_SOURCE_ABSENT
            why = '该 attempt 未到达上游（阶段：%s），本不该产生计费事实' % ','.join(seen)
        elif evidence['reported'] and unambiguous:
            fact, source = BILLING_REPORTED, BILLING_SOURCE_RECEIPT
            why = ('上游回了请求级 usage，且本请求只有这一个 attempt 到达过上游；'
                   '用量记在 usage_events（请求级），不按 attempt 拆分')
        elif evidence['reported']:
            fact, source = BILLING_UNKNOWN, BILLING_SOURCE_ABSENT
            why = ('请求级有上游 usage，但本请求有 %d 个 attempt 到达过上游，'
                   '缺池侧 per-attempt 回执无法归属' % len(reached))
        elif any(p in _TERMINAL_PHASES for p in seen):
            fact, source = BILLING_UNKNOWN, BILLING_SOURCE_ABSENT
            why = 'attempt 已结束，但请求级没有上游 usage 回执（usage_source=%s）' % (
                evidence['usage_source'],)
        else:
            fact, source = BILLING_UNKNOWN, BILLING_SOURCE_ABSENT
            why = 'attempt 到达上游但无终态事件，是否计费未知'
        out.append({'attempt': number, 'billing_fact': fact, 'billing_source': source,
                    'evidence': why, 'phases': seen,
                    'reached_upstream': reached_upstream})
    return out


def record_attempt_billing(request_id: str, now: int | None = None) -> int:
    """把推导出的三态事实**追加**进不可变表；幂等（INSERT OR IGNORE），返回新增条数。"""
    from . import db
    if not request_id:
        return 0
    stamp = int(time.time()) if now is None else int(now)
    written = 0
    with db.transaction() as conn:
        for item in derive_attempt_billing(request_id):
            written += conn.execute(
                'INSERT OR IGNORE INTO attempt_billing_facts'
                '(request_id,attempt,ts,billing_fact,billing_source,evidence) '
                'VALUES(?,?,?,?,?,?)',
                (request_id, item['attempt'], stamp, item['billing_fact'],
                 item['billing_source'], item['evidence'][:300])).rowcount
    return written


def backfill_attempt_billing(limit: int = 200, min_age: int = ATTEMPT_BILLING_MIN_AGE_SEC,
                             now: int | None = None) -> int:
    """后台补齐：给「已终结且过了稳定窗口」的请求落 attempt 计费事实。

    只挑**还没落过事实**的请求（LEFT JOIN 为空），按 request_logs.ts 的时间窗
    走索引，单轮有界。返回本轮写入的请求数。
    """
    from . import db
    stamp = int(time.time()) if now is None else int(now)
    horizon = stamp - max(0, int(min_age))
    rows = db.query(
        'SELECT DISTINCT l.request_id FROM request_logs l '
        'JOIN attempt_events a ON a.request_id=l.request_id '
        'LEFT JOIN attempt_billing_facts f ON f.request_id=l.request_id '
        'WHERE f.request_id IS NULL AND l.ts<=? AND l.ts>=? LIMIT ?',
        (horizon, horizon - 7 * 86400, int(limit)))
    filled = 0
    for row in rows:
        try:
            if record_attempt_billing(row['request_id'], stamp):
                filled += 1
        except Exception:  # noqa: BLE001 - 单条失败不中断本轮
            _log.exception('attempt billing backfill failed request_id=%s',
                           row['request_id'])
    return filled


def attempt_billing_report(request_id: str) -> dict:
    """回答「这次请求有几个 attempt、各自有无计费证据」。

    `per_attempt_billing` 恒为 `not_derived`：本地没有定价表，池侧也没有
    per-attempt 回执与关联 ID —— 任何 per-attempt 金额都是编造的。要拿到
    per-attempt 费用，先把 `PER_ATTEMPT_BILLING_REQUIREMENTS` 补齐。
    """
    from . import db
    attempts = derive_attempt_billing(request_id)
    recorded = {int(r['attempt']): r for r in db.query(
        'SELECT attempt,billing_fact,billing_source,evidence,ts '
        'FROM attempt_billing_facts WHERE request_id=?', (request_id,))}
    counts = {name: 0 for name in BILLING_FACTS}
    drift = {}
    for item in attempts:
        counts[item['billing_fact']] += 1
        stored = recorded.get(item['attempt'])
        if stored is not None and (stored['billing_fact'] != item['billing_fact']
                                   or stored['billing_source'] != item['billing_source']):
            drift[str(item['attempt'])] = {'recorded': stored['billing_fact'],
                                           'derived': item['billing_fact']}
    evidence = _request_usage_evidence(request_id)
    return {
        'request_id': request_id,
        'attempt_count': len(attempts),
        'attempt_count_source': 'pool_attempt_events' if attempts else 'absent',
        'attempts': [{k: item[k] for k in
                      ('attempt', 'billing_fact', 'billing_source', 'evidence',
                       'reached_upstream')} for item in attempts],
        'billing_fact_counts': counts,
        'recorded_facts': len(recorded),
        'recorded_drift': drift,
        'request_level': {
            'attempt': REQUEST_LEVEL_ATTEMPT,
            'usage_events': evidence['usage_events'],
            'provenance': evidence['provenance'],
            'credit_units': evidence['credit_units'],
            'usage_source': evidence['usage_source'],
            'usage_complete': evidence['usage_complete'],
            'terminal_state': evidence['terminal_state'],
            'note': ('usage_events 只记请求级用量（attempt 列恒为 %d），'
                     'attempt 变多不会重复计费' % REQUEST_LEVEL_ATTEMPT),
        },
        'per_attempt_billing': 'not_derived',
        'per_attempt_billing_note': (
            '不做 per-attempt 计费：本地没有定价表，池侧也没有 per-attempt 回执与'
            '关联 ID；缺证据时只记 unknown/not_applicable，绝不合成 token 或金额。'),
        'pool_side_requirements': list(PER_ATTEMPT_BILLING_REQUIREMENTS),
    }


# ── U22（2026-10-28）：attempt 轨迹时间线（**只读查询**）─────────────────────
#
# 审计要求：「先提供已有的 attempt/phase/结果查询；『为何选中』只在上游提供
# 候选/排除/权重/冷却快照后展示」。本段只做前半句 —— 把**已经记录下来的事实**
# 拼成时间线，缺证据一律 null / 'unknown'，不推断、不补 0。
#
# 「为什么选这个账号」**没有数据可拼**：上游 /v1 只回结果，不回候选集、排除原因、
# 权重或冷却快照。所以这里把边界写成常量，由路由原样写进响应，避免调用方误以为
# 没查到 = 没发生。

#: 要能回答「为什么选它而不是别的」，池侧必须补齐的证据（当前一条都没有）。
SELECTION_SNAPSHOT_REQUIREMENTS = (
    '本次选号的候选集（含被排除的账号及排除原因：冷却 / 禁用 / 无配额 / 权重为 0）',
    '参与排序的权重与随机种子（同一请求内的确定性来源）',
    '选号时刻的池快照（各账号 cooling / disabled / quota 状态 + 采样时间）',
    '换号与停止重试的决策依据（为什么换下一个、为什么不再试）',
)

#: 响应里固定的解释边界（字段名与取值由测试钉住）。
EXPLAIN_BOUNDARY = {
    'can_answer': (
        '这次请求经过几个 attempt',
        '每个 attempt 的 phase 序列、起止时间与各段耗时',
        '选中了哪个账号（attempt_events.account_uid）',
        '在哪个阶段结束/失败（最后一个 phase + request_outcomes.error_stage）',
        '该 attempt 是否到达上游、有无计费证据',
    ),
    'cannot_answer': (
        '为什么选中这个账号而不是别的（候选集 / 排除原因 / 权重 / 冷却快照）',
        '上游池内部的排序与重试决策',
    ),
    'missing_evidence': list(SELECTION_SNAPSHOT_REQUIREMENTS),
}

#: 「为何选中」这一层当前不可解释的机器可读原因（写进响应）。
EXPLAIN_REASON = 'upstream_does_not_expose_selection_snapshot'


def _request_outcome(request_id: str) -> dict:
    """请求级终态（request_outcomes）。缺行 = `absent`，各字段 null 而不是 0。"""
    from . import db
    row = db.query_one('SELECT * FROM request_outcomes WHERE request_id=?', (request_id,))
    if row is None:
        return {'source': 'absent', 'ts': None, 'terminal_state': None,
                'error_stage': None, 'error_code': None, 'http_status': None,
                'latency_ms': None, 'endpoint': None, 'protocol': None,
                'release_id': None}
    # error_code 是后加的列（U21）：旧库可能没有，读不到就报 null 而不是猜。
    keys = set(row.keys()) if hasattr(row, 'keys') else set(row)
    return {
        'source': 'request_outcomes',
        'ts': int(row['ts']) if row['ts'] is not None else None,
        'terminal_state': row['terminal_state'],
        'error_stage': row['error_stage'],
        'error_code': row['error_code'] if 'error_code' in keys else None,
        'http_status': int(row['http_status']) if row['http_status'] is not None else None,
        'latency_ms': int(row['latency_ms']) if row['latency_ms'] is not None else None,
        'endpoint': row['endpoint'],
        'protocol': row['protocol'],
        'release_id': row['release_id'],
    }


def attempt_timeline(request_id: str) -> dict:
    """把一次请求的 attempt 轨迹拼成时间线（**只读**，数据不编造）。

    数据**只**来自 `attempt_events` / `request_outcomes` / `attempt_billing_facts`：

      · `attempts[]`：每个 attempt 的 phase 序列、起止、总耗时、相邻 phase 间隔、
        是否到达上游、记录到的终态 phase、以及已落库的计费事实（没有就是 null）；
      · `timeline[]`：全部 phase 事件按 (ts, attempt, phase) 合并排序，
        带相对首个事件的 `elapsed_ms`；
      · `request`：请求级终态（缺行 → source='absent'，字段全 null）；
      · `explainable=False` + `reason`：**「为什么选它」这一层不可解释**，
        缺的是池侧快照（见 `SELECTION_SNAPSHOT_REQUIREMENTS`）。

    刻意**不做**的事：不把"最后一个 phase"说成"失败原因"（那只是记录到的最后一个
    事件，是否失败以 `request.terminal_state` 为准）；不补 0、不推算缺失的 attempt。
    """
    from . import db
    events = db.query(
        'SELECT attempt,phase,ts,account_uid,resolved_model,realm,status,config_hash '
        'FROM attempt_events WHERE request_id=? ORDER BY attempt,ts,phase',
        (request_id,))
    billing = {int(r['attempt']): {'billing_fact': r['billing_fact'],
                                   'billing_source': r['billing_source'],
                                   'evidence': r['evidence'],
                                   'ts': int(r['ts']) if r['ts'] is not None else None}
               for r in db.query(
                   'SELECT attempt,billing_fact,billing_source,evidence,ts '
                   'FROM attempt_billing_facts WHERE request_id=?', (request_id,))}
    outcome = _request_outcome(request_id)

    by_attempt: dict[int, list] = {}
    for row in events:
        by_attempt.setdefault(int(row['attempt']), []).append(row)

    attempts = []
    timeline = []
    first_ts = min((int(r['ts']) for r in events), default=None)
    for number in sorted(by_attempt):
        rows = by_attempt[number]
        phases = [{'phase': str(r['phase']),
                   'ts': int(r['ts']),
                   'status': int(r['status']) if r['status'] is not None else None}
                  for r in rows]
        names = [p['phase'] for p in phases]
        started, ended = phases[0]['ts'], phases[-1]['ts']
        gaps = [(phases[i + 1]['ts'] - phases[i]['ts']) * 1000
                for i in range(len(phases) - 1)]
        attempts.append({
            'attempt': number,
            'account_uid': str(rows[0]['account_uid'] or '') or None,
            'resolved_model': str(rows[0]['resolved_model'] or '') or None,
            'realm': str(rows[0]['realm'] or '') or None,
            'config_hash': str(rows[0]['config_hash'] or '') or None,
            'phase_sequence': names,
            'phases': phases,
            'started_at': started,
            'ended_at': ended,
            'duration_ms': max(0, ended - started) * 1000,
            'phase_gaps_ms': gaps,
            'reached_upstream': any(name in _REACHED_PHASES for name in names),
            # 记录到的终态 phase（只有 stream_terminal / aggregate_terminal 算）；
            # 没有就 null —— 不把"最后一个 phase"当成"失败原因"。
            'terminal_phase': (names[-1] if names and names[-1] in _TERMINAL_PHASES
                               else None),
            'last_phase': names[-1] if names else None,
            'billing': billing.get(number),
        })
        for p in phases:
            timeline.append({'ts': p['ts'], 'attempt': number, 'phase': p['phase'],
                             'status': p['status'],
                             'elapsed_ms': ((p['ts'] - first_ts) * 1000
                                            if first_ts is not None else None)})
    timeline.sort(key=lambda item: (item['ts'], item['attempt'], item['phase']))

    return {
        'request_id': request_id,
        'explainable': False,
        'reason': EXPLAIN_REASON,
        'boundary': {key: list(value) for key, value in EXPLAIN_BOUNDARY.items()},
        'attempt_count': len(attempts),
        'attempt_count_source': 'attempt_events' if attempts else 'absent',
        'attempts': attempts,
        'timeline': timeline,
        'request': outcome,
        'evidence': {
            'sources': ['attempt_events', 'request_outcomes', 'attempt_billing_facts'],
            'attempt_events': len(events),
            'attempt_billing_facts': len(billing),
            'request_outcomes': 1 if outcome['source'] == 'request_outcomes' else 0,
            'first_event_ts': first_ts,
            'note': ('时间线只拼已落库的事实；缺的字段一律 null / unknown，'
                     '不推断、不补 0'),
        },
    }
