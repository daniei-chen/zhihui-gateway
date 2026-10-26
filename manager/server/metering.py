"""K27 · 请求归因、用量三态与「日志 / 事件 / 预算 / 终态」四表对账。

## 现状（证据）

* 归因链本来就有：`observability.py` 给每个请求一个 `request_id`
  （`RequestAuditMiddleware`），`gateway._record()` 把它写进 `request_logs`、
  `usage_events`、`budget_reservations`、`forwarding_events` 与 `request_outcomes`。
  **不是「完全没有归因」**，所以本模块不重写链路，只补三件缺的东西。
* 缺 1 · **实际平台/上游没有落库**：`request_logs.platform` 记录的是入口/强制
  平台（`gateway.forced_platform` 或默认 `workbuddy`，见 `db.add_request_log`），
  而模型名前缀 `qoder/…`、密钥绑定上游、三平台上游都会改变**实际**路由结果
  （`gateway._upstream_for`）。这导致「日志说 workbuddy，实际打在 Qoder 上」
  无法事后核对。→ 新增 `request_logs.actual_platform` / `upstream_ref`
  （`migrations.py` v4），由本模块的 `route_attribution()` 计算并写入。
* 缺 2 · **用量三态没被显式区分**：`_usage_credit()` 已经把「上游没给 credit」
  记成 NULL（正确），但 token 列在写入前被 `observability.usage_int()` 归一成
  整数 0（`gateway._record`），于是「0 token」既可能是真的 0、也可能是上游没回
  usage。`request_logs.usage_source/usage_complete` 两列已经能区分，但没有
  查询/对账口径去用它。→ `parse_usage()` 用三态（present / absent / invalid）
  解析，`spend_class()` 把「成功已知 / 已取消 / 未知 / 拒绝无花费 / 不计费」
  分开；对账报告里**不把未知当 0**。
* 缺 3 · **没有对账出口**：四张表之间对不对得上，只能人工 SQL。→ `reconcile()`
  用 `request_id` 把 log / usage_event / budget_reservation / forwarding_event /
  outcome 串起来，输出分类计数 + 差异样本（不含 prompt/凭据）。

## 明确不做

* 不把未知用量填 0、不把 `unknown` 预算释放/归档（`budget.resolve` 是唯一出口，
  且必须人工带原因）。
* 不重算历史行：`reconcile()` 只读；历史缺 `actual_platform` 的行保持 NULL 并
  单独计数（NULL = 未记录，不等于 workbuddy）。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

# 花费分类（K27：成功 / 取消 / 未知必须分开）
SPEND_SETTLED = 'settled_known'          # 终态成功且用量已知，已结算
SPEND_CANCELLED = 'cancelled'            # 客户端取消（499）：可能有部分用量
SPEND_UNKNOWN = 'unknown_usage'          # 终态完成/中断但用量未知 —— 不可当 0
SPEND_REJECTED = 'rejected_no_spend'     # 请求在鉴权/预算/限流阶段被拒，无上游花费
SPEND_NOT_BILLABLE = 'not_billable'      # /v1/models 之类不计费的只读请求
SPEND_CLASSES = (SPEND_SETTLED, SPEND_CANCELLED, SPEND_UNKNOWN, SPEND_REJECTED,
                 SPEND_NOT_BILLABLE)

#: 不计费的端点（参考路由：`routers/models.py` 的 /v1/models 等）
NON_BILLABLE_PATHS = ('/v1/models', '/v2/models', '/models')


@dataclass(frozen=True)
class Usage:
    """上游 usage 的三态快照。

    `prompt_tokens=None` 表示**上游没给**（absent），与 `0`（给了但是 0）不同；
    credit 同理（`_usage_credit` 已经是这个语义，这里统一进结构体）。
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    credit: float | None
    observed: bool                 # 上游是否返回了 usage 对象/字段
    malformed: bool = False        # 收到了但不是合法数字

    @property
    def complete(self) -> bool:
        """两个 token 字段都拿到了 —— 只有这种才允许说「用量已知」。"""
        return self.prompt_tokens is not None and self.completion_tokens is not None

    @property
    def known_tokens(self) -> int | None:
        if not self.complete:
            return None
        return int(self.prompt_tokens) + int(self.completion_tokens)

    def storage_tokens(self) -> tuple[int, int]:
        """写 `request_logs.prompt_tokens/completion_tokens` 的兼容值。

        旧列是 `INTEGER DEFAULT 0`，历史上一直写整数；为了不改变既有读路径
        （统计、图表、导出），这里仍然返回 0 —— **但调用方必须同时写
        `usage_source`/`usage_complete`**，让「0 = 未知」这件事可判别。
        真正的未知语义保存在 `usage_events.credit_units=NULL` + `counted=0` 里。
        """
        return (int(self.prompt_tokens or 0), int(self.completion_tokens or 0))


def _int_or_none(raw: object) -> tuple[int | None, bool]:
    """把上游字段解析成 (值, 是否非法)。absent 与 invalid 分开。"""
    if raw is None:
        return None, False
    if isinstance(raw, bool):
        return None, True
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, True
    if value < 0:
        return None, True
    return value, False


def parse_usage(usage: object) -> Usage:
    """解析上游 usage；**绝不把 absent 变成 0**。"""
    if not isinstance(usage, dict) or not usage:
        return Usage(None, None, None, observed=False)
    pt, bad_pt = _int_or_none(usage.get('prompt_tokens'))
    ct, bad_ct = _int_or_none(usage.get('completion_tokens'))
    raw_credit = usage.get('credit')
    if raw_credit is None:
        raw_credit = usage.get('credits')
    credit: float | None = None
    bad_credit = False
    if raw_credit is not None:
        if isinstance(raw_credit, bool):
            bad_credit = True
        else:
            try:
                value = float(raw_credit)
                credit = value if value >= 0 else None
                bad_credit = credit is None
            except (TypeError, ValueError):
                bad_credit = True
    return Usage(pt, ct, credit, observed=True,
                 malformed=bad_pt or bad_ct or bad_credit)


def route_attribution(*, entry_platform: str = '', requested_model: str | None = None,
                      resolved_model: str | None = None,
                      upstream: dict | None = None) -> dict:
    """回答「这次到底路由到了哪」。

    返回：

    * `entry_platform`：入口/强制平台（写进既有 `platform` 列，口径不变）；
    * `actual_platform`：实际被路由到的平台；
    * `upstream_ref`：`id@host`（只有 id 与主机名，不含路径/凭据）；
    * `platform_source`：判定依据，便于事后复核（`forced` / `model_prefix` /
      `threeway` / `key_upstream` / `default`）。
    """
    entry = (entry_platform or '').strip()
    model = resolved_model or requested_model or ''
    prefix = model.split('/', 1)[0].lower() if '/' in model else ''
    actual = ''
    source = ''
    upstream = upstream or {}
    name = str(upstream.get('name') or '').strip().lower()
    if name in ('qoder', 'trae'):
        actual, source = name, 'threeway'
    elif entry:
        actual, source = entry, 'forced'
    elif prefix in ('qoder', 'trae'):
        actual, source = prefix, 'model_prefix'
    elif upstream.get('id'):
        actual, source = 'workbuddy', 'key_upstream'
    elif model:
        actual, source = 'workbuddy', 'default'
    upstream_ref = ''
    if upstream:
        upstream_id = upstream.get('id')
        label = str(upstream_id) if upstream_id not in (None, '') else (name or 'default')
        host = ''
        try:
            host = urlsplit(str(upstream.get('base_url') or '')).hostname or ''
        except ValueError:
            host = ''
        upstream_ref = f'{label}@{host}' if host else label
    return {
        'entry_platform': entry or None,
        'actual_platform': actual or None,
        'upstream_ref': upstream_ref or None,
        'platform_source': source or None,
    }


def spend_class(*, terminal_state: str | None, usage: Usage, credit: float | None,
                protocol: str | None = None, endpoint: str | None = None,
                budget_state: str | None = None) -> str:
    """把一次请求归到五类花费之一（互斥、可统计）。

    规则（顺序有意如此）：

    1. 终态 `client_cancelled` → 取消类（即使有部分 usage，也不并入「成功」）；
    2. 终态 `rejected` / 4xx 且没有观测到 usage → 拒绝无花费（不进成本分母）；
    3. 终态成功且 usage 完整 → 已结算已知；
    4. 非计费端点且没有 usage → 不计费；
    5. 其余（含 `unknown` 预算、usage 缺失的完成请求）→ 未知用量 —— **不是 0**。
    """
    terminal = (terminal_state or '').strip()
    path = (endpoint or '').split('?', 1)[0]
    if terminal == 'client_cancelled':
        return SPEND_CANCELLED
    if terminal in ('rejected', 'admission'):
        # 鉴权/预算/限流/准入拒绝：只要没有观测到 usage 就没有花费证据
        if not usage.observed and credit is None:
            return SPEND_REJECTED
        return SPEND_UNKNOWN
    if terminal and terminal != 'completed':
        # upstream_error / transport_error / protocol_incomplete / interrupted：
        # 可能已经产生了（部分）用量，但无法证明 —— 归未知，不归 0
        return SPEND_UNKNOWN
    if usage.complete and usage.observed:
        return SPEND_SETTLED
    if path in NON_BILLABLE_PATHS and not usage.observed:
        return SPEND_NOT_BILLABLE
    if budget_state == 'unknown':
        return SPEND_UNKNOWN
    return SPEND_UNKNOWN


# ── 对账 ────────────────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


CATEGORY_DESCRIPTIONS = {
    'missing_usage_event': 'request_logs 有记录，但 usage_events 没有对应事件（账本缺行）',
    'token_mismatch': '同一 request_id 的日志 token 与 usage_events 不一致',
    'platform_mismatch': '日志的入口平台与实际路由平台不一致',
    'attribution_missing': '推理请求缺少 actual_platform/upstream_ref（无法核对实际路由）',
    'budget_unknown': '预算预留停在 unknown —— 必须人工核对，不得自动释放',
    'budget_settled_without_event': '预算已 settled，但 usage_events 没有对应事件',
    'terminal_mismatch': 'request_logs 与 request_outcomes 的终态不一致',
    'zero_token_completed': '终态成功但 token 全 0 且 usage 不完整（未知用量，不是免费）',
    'log_without_request_id': '历史日志没有 request_id（无法参与归因），只能计数',
    'outcome_without_log': '有请求终态但没有请求日志（容量/预算拒绝等无花费路径）',
}


def reconcile(conn: sqlite3.Connection, *, since_ts: int | None = None,
              limit: int = 2000, samples: int = 5) -> dict:
    """只读对账：按 `request_id` 把五张表串起来，输出分类差异。

    `limit` 限制扫描的日志行数（默认最近 2000 条），保证在本地大库上也是秒级；
    全量对账应由归档脚本按水位分批调用。
    """
    findings: dict[str, dict] = {name: {'count': 0, 'samples': []}
                                 for name in CATEGORY_DESCRIPTIONS}
    totals = {name: 0 for name in SPEND_CLASSES}
    scanned = 0

    events: dict[str, dict] = {}
    budgets: dict[str, dict] = {}
    forwards: dict[str, dict] = {}
    outcomes: dict[str, dict] = {}

    if _table_exists(conn, 'usage_events'):
        for row in conn.execute(
                'SELECT request_id,prompt_tokens,completion_tokens,credit_units,'
                'counted,terminal_state FROM usage_events'):
            events[str(row[0])] = {'pt': row[1], 'ct': row[2], 'units': row[3],
                                   'counted': row[4], 'terminal': row[5]}
    if _table_exists(conn, 'budget_reservations'):
        for row in conn.execute(
                'SELECT request_id,state,charged_tokens,charged_credit FROM budget_reservations'):
            budgets[str(row[0])] = {'state': row[1], 'tokens': row[2], 'credit': row[3]}
    if _table_exists(conn, 'forwarding_events'):
        for row in conn.execute(
                'SELECT request_id,upstream_id,resolved_model,realm FROM forwarding_events '
                "WHERE attempt=1 AND phase='prepared'"):
            forwards[str(row[0])] = {'upstream_id': row[1], 'model': row[2], 'realm': row[3]}
    if _table_exists(conn, 'request_outcomes'):
        for row in conn.execute(
                'SELECT request_id,terminal_state,http_status FROM request_outcomes'):
            outcomes[str(row[0])] = {'terminal': row[1], 'status': row[2]}

    def add(name: str, sample: dict) -> None:
        bucket = findings[name]
        bucket['count'] += 1
        if len(bucket['samples']) < samples:
            bucket['samples'].append(sample)

    sql = ('SELECT request_id,ts,endpoint,protocol,platform,actual_platform,upstream_ref,'
           'status,http_status,terminal_state,prompt_tokens,completion_tokens,credit,'
           'usage_source,usage_complete FROM request_logs WHERE request_id IS NOT NULL')
    args: list[object] = []
    if since_ts is not None:
        sql += ' AND ts >= ?'
        args.append(int(since_ts))
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(int(limit))
    if _table_exists(conn, 'request_logs'):
        # actual_platform/upstream_ref 是 v4 迁移的列；旧库可能还没有，动态兜底
        columns = {str(r[1]) for r in conn.execute('PRAGMA table_info(request_logs)')}
        if 'actual_platform' not in columns:
            sql = sql.replace('actual_platform', 'NULL AS actual_platform')
        if 'upstream_ref' not in columns:
            sql = sql.replace('upstream_ref', 'NULL AS upstream_ref')
        rows = conn.execute(sql, tuple(args)).fetchall()
    else:
        rows = []

    for row in rows:
        scanned += 1
        rid = str(row[0])
        endpoint = row[2]
        platform = row[4]
        actual = row[5]
        upstream_ref = row[6]
        terminal = row[9]
        usage = Usage(
            prompt_tokens=row[10], completion_tokens=row[11],
            credit=None if row[12] is None else float(row[12]),
            observed=(row[13] == 'upstream'),
        )
        budget = budgets.get(rid)
        klass = spend_class(terminal_state=terminal, usage=usage, credit=usage.credit,
                            endpoint=endpoint,
                            budget_state=budget['state'] if budget else None)
        totals[klass] += 1
        sample = {'request_id': rid, 'endpoint': endpoint, 'terminal_state': terminal,
                  'platform': platform, 'actual_platform': actual,
                  'upstream_ref': upstream_ref, 'spend_class': klass}

        event = events.get(rid)
        if klass in (SPEND_SETTLED, SPEND_CANCELLED, SPEND_UNKNOWN) and event is None:
            add('missing_usage_event', sample)
        if event is not None and event['counted'] and usage.complete:
            if (event['pt'] or 0) != (usage.prompt_tokens or 0) or \
                    (event['ct'] or 0) != (usage.completion_tokens or 0):
                add('token_mismatch', {**sample, 'event_tokens': event['pt'] + event['ct']})
        if actual and platform and actual != platform:
            add('platform_mismatch', sample)
        if not actual and (endpoint or '') not in NON_BILLABLE_PATHS and \
                (row[3] or '') != 'http':
            add('attribution_missing', sample)
        if budget and budget['state'] == 'unknown':
            add('budget_unknown', {**sample, 'budget_state': 'unknown'})
        if budget and budget['state'] == 'settled' and event is None:
            add('budget_settled_without_event', sample)
        outcome = outcomes.get(rid)
        if outcome and outcome['terminal'] and terminal and outcome['terminal'] != terminal:
            add('terminal_mismatch', {**sample, 'outcome_terminal': outcome['terminal']})
        if terminal == 'completed' and usage.complete is False and \
                (row[14] or 0) == 0 and not usage.observed:
            add('zero_token_completed', sample)

    if _table_exists(conn, 'request_logs'):
        legacy = conn.execute(
            'SELECT COUNT(*) FROM request_logs WHERE request_id IS NULL').fetchone()
        findings['log_without_request_id']['count'] = int(legacy[0] or 0)
    if _table_exists(conn, 'request_logs') and outcomes:
        known = {str(r[0]) for r in conn.execute(
            'SELECT request_id FROM request_logs WHERE request_id IS NOT NULL')}
        for rid in list(outcomes)[:limit]:
            if rid not in known:
                add('outcome_without_log', {'request_id': rid,
                                            'outcome': outcomes[rid]})

    return {
        'generated_at': int(time.time()),
        'scanned_logs': scanned,
        'spend_classes': totals,
        'findings': {k: {'count': v['count'], 'samples': v['samples'],
                         'description': CATEGORY_DESCRIPTIONS[k]}
                     for k, v in findings.items()},
        'tables_seen': {'usage_events': len(events), 'budget_reservations': len(budgets),
                        'forwarding_events': len(forwards), 'request_outcomes': len(outcomes)},
        'notes': [
            '未知用量不会计入 0：spend_class=unknown_usage 的行需要人工/上游对账',
            'actual_platform 为 NULL 表示该行未记录实际路由（不是 workbuddy）',
        ],
    }
