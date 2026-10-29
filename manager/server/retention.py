"""K24 · 账本与日志数据的保留 / 归档 / 对账分类（本地候选，不连生产）。

## 现状（证据）

* **五张不可变事件表**：`ledger.py:44-63` 给 `attempt_events` / `usage_events` /
  `adjustment_events` / `admin_events` / `forwarding_events` 各建了
  `*_no_update` 与 `*_no_delete` 触发器（`RAISE(ABORT, '… are immutable')`）。
  这些触发器**一个都不能删** —— 它们是「账本权威」的实现，不是可优化项。
* **已有保留期**（`db.py::_prune_side_tables` / `_prune_request_logs`）：
  `request_logs.ts` 90 天滚动删除；`request_outcomes.ts` 90 天；`usage_hourly`
  90 天；`task_logs` 90 天；`audit_logs` 只留最新 20000 行；
  `ip_access_logs` 有行数上限（`db.py::add_ip_access_log` 里按 `id` 截断）。
  `observability.py` 的中间件每 2000 次写也会顺手删 90 天前的 `request_outcomes`。
* **缺的是「归档出口」**：上面的清理都是 `DELETE`，删掉就没了。而 K24 要求
  「归档完整性、对账、取回检验后才清理」「未知预算不可归档」。所以本模块把
  每张表**分类**，并给 `tools/archive_ledgers.py` 提供筛选/对账口径。
* **未知预算**：`budget.py::recover` 只释放「确认已死的本机 owner + 尚未转发」的
  预留，其余一律置 `unknown`；`budget.py::resolve` 只允许人工核对 `unknown`。
  **任何自动流程（包括归档）都不得把 `unknown` 释放或归档掉。**

## 分类口径

| 类 | 含义 | 允许删除？ |
|---|---|---|
| `ledger-event` | 权威事件（不可变触发器） | 否，只能导出 |
| `projection` | 聚合投影，可从权威数据/日志重建 | 是（滚动或按上限） |
| `derived-log` | 派生日志（请求日志、终态、入站访问） | 归档 + 校验后才可以 |
| `state` | 状态机（预算预留） | 未决/unknown 不可，已结算可归档 |
| `config` | 配置/身份/密钥 | 否（不进保留策略） |
| `archive-ledger` | 归档台账本身 | 否 |

「保留期」是**最低保留**：短于它不得删除；长于它可以继续留着。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field

CLASS_LEDGER_EVENT = 'ledger-event'
CLASS_PROJECTION = 'projection'
CLASS_DERIVED_LOG = 'derived-log'
CLASS_STATE = 'state'
CLASS_CONFIG = 'config'
CLASS_ARCHIVE_LEDGER = 'archive-ledger'

ACTION_NEVER = 'never'                      # 只导出，不删除
ACTION_NO_DELETE = 'no-delete'              # 触发器禁止删除，删除即缺陷
ACTION_ARCHIVE_THEN_PRUNE = 'archive-then-prune'
ACTION_PRUNE = 'prune'
ACTION_CAP_ROWS = 'cap-rows'
ACTION_KEEP = 'keep'

#: 不可变事件表 → 触发器名（K24 要求「不删 no-delete 触发器」）
IMMUTABLE_TABLES = {
    'attempt_events': ('attempt_events_no_update', 'attempt_events_no_delete'),
    'usage_events': ('usage_events_no_update', 'usage_events_no_delete'),
    'adjustment_events': ('adjustment_events_no_update', 'adjustment_events_no_delete'),
    'admin_events': ('admin_events_no_update', 'admin_events_no_delete'),
    'forwarding_events': ('forwarding_events_no_update', 'forwarding_events_no_delete'),
}


@dataclass(frozen=True)
class Dataset:
    name: str
    klass: str
    action: str
    retention_days: int | None
    time_column: str | None
    watermark: str
    reconcile: str
    evidence: str
    note: str = ''
    archive_filter: str = ''       # 归档/清理时附加的 WHERE（含 NOT NULL），空 = 全部
    delete_filter: str = ''        # 删除时附加的 WHERE（保守：通常比 archive_filter 更严）
    state_column: str | None = None

    @property
    def watermark_key(self) -> str:
        """归档 JSONL 里代表水位的键名。

        `rowid` 不是 `SELECT *` 的列，需要显式别名；其余数据集的水位就是真实列。
        """
        return self.watermark if self.watermark != 'rowid' else '_watermark'

    def select_sql(self, before_ts: int, watermark: int, limit: int) -> tuple[str, tuple]:
        """按水位 + 时间窗选一批行（水位列必须单调）。"""
        columns = '*' if self.watermark != 'rowid' else 'rowid AS _watermark, *'
        where = [f'{self.watermark} > ?']
        args: list[object] = [watermark]
        if self.time_column:
            where.append(f'{self.time_column} < ?')
            args.append(before_ts)
        if self.archive_filter:
            where.append(self.archive_filter)
        sql = (f'SELECT {columns} FROM {self.name} WHERE ' + ' AND '.join(where)
               + f' ORDER BY {self.watermark} LIMIT ?')
        args.append(limit)
        return sql, tuple(args)

    def delete_sql(self, upto_watermark: int) -> tuple[str, tuple]:
        """只删除「已归档且已校验」的水位行；附加 delete_filter 保守收缩。"""
        where = [f'{self.watermark} <= ?']
        args: list[object] = [upto_watermark]
        if self.delete_filter:
            where.append(self.delete_filter)
        return (f'DELETE FROM {self.name} WHERE ' + ' AND '.join(where), tuple(args))

    def delete_batch_sql(self, upto_watermark: int, limit: int) -> tuple[str, tuple]:
        """分批删除（SQLite 默认编译选项没有 `DELETE ... LIMIT`，用子查询限行）。"""
        where = [f'{self.watermark} <= ?']
        args: list[object] = [upto_watermark]
        if self.delete_filter:
            where.append(self.delete_filter)
        condition = ' AND '.join(where)
        sql = (f'DELETE FROM {self.name} WHERE {self.watermark} IN ('
               f'SELECT {self.watermark} FROM {self.name} WHERE {condition} LIMIT ?)')
        return sql, tuple(args) + (int(limit),)


#: 全量分类表。注意：每一项都要能落到真实代码，不允许「看起来合理」。
DATASETS: tuple[Dataset, ...] = (
    Dataset(
        name='request_logs', klass=CLASS_DERIVED_LOG, action=ACTION_ARCHIVE_THEN_PRUNE,
        retention_days=90, time_column='ts', watermark='id',
        reconcile='按 request_id 与 usage_events / request_outcomes 一一对齐；credit 对账须区分 NULL（上游未回）与 0',
        evidence='db.py::_prune_request_logs（90 天 DELETE）；routers/logs.py 列表/清除端点',
        note=('**现状是「到点直接删」（db._prune_request_logs），没有归档出口** —— '
              '本分类给出目标策略：先导出 manifest + 对账 + 取回检验，再删。'
              '`request_id` 为 NULL 的历史行无法参与归因对账，删除前要单独计数。'),
        archive_filter='request_id IS NOT NULL',
    ),
    Dataset(
        name='request_outcomes', klass=CLASS_DERIVED_LOG, action=ACTION_ARCHIVE_THEN_PRUNE,
        retention_days=90, time_column='ts', watermark='rowid',
        reconcile='每个 request_id 应能在 request_logs 或「无花费路径」（容量/预算拒绝）里找到解释',
        evidence='db.py::_prune_side_tables；observability.py 中间件每 2000 次写清理',
        note='现状同样直接删；目标策略是先归档校验再删。表无自增 id，水位用 rowid。',
    ),
    Dataset(
        name='usage_events', klass=CLASS_LEDGER_EVENT, action=ACTION_NO_DELETE,
        retention_days=None, time_column='ts', watermark='rowid',
        reconcile='credit_units 权威值；与 usage_daily/usage_hourly 投影逐日逐 key 对账',
        evidence='ledger.py:44-63 触发器；ledger.record() 写 counted/terminal_state/provenance',
        note=('K24 明确：冻结数据不是死数据。只允许导出快照，**不得** DELETE/UPDATE。'
              'unknown 用量（credit_units IS NULL）必须原样保留，人工核对走 adjustment_events。'),
    ),
    Dataset(
        name='forwarding_events', klass=CLASS_LEDGER_EVENT, action=ACTION_NO_DELETE,
        retention_days=None, time_column='ts', watermark='rowid',
        reconcile='request_id/attempt/phase 三元组唯一；upstream_id 是「实际路由到的上游」的权威证据',
        evidence='ledger.forwarding()；ledger.py:60-63 触发器',
        note='K27 的「实际平台/上游」就靠它与 request_logs.actual_platform/upstream_ref 互证。',
    ),
    Dataset(
        name='attempt_events', klass=CLASS_LEDGER_EVENT, action=ACTION_NO_DELETE,
        retention_days=None, time_column='ts', watermark='rowid',
        reconcile='request_id/attempt/phase 唯一；account_uid + resolved_model + realm 可核对实际账号与模型',
        evidence='ledger.attempt()；ledger.py:44-47 触发器；routers/logs.py::/attempts 只读端点',
        note=('审计明确「冻结的 attempt 数据不是可立即删除的死数据」：停写不代表无审计价值。'
              '保留期满后也只能归档，不能 DELETE。'),
    ),
    Dataset(
        name='attempt_billing_facts', klass=CLASS_LEDGER_EVENT, action=ACTION_NO_DELETE,
        retention_days=None, time_column='ts', watermark='rowid',
        reconcile=('request_id/attempt 唯一；billing_fact ∈ {reported,unknown,not_applicable}，'
                   'billing_source ∈ {provider_receipt,absent}；与 usage_events 的请求级事实分开核对'),
        evidence=('ledger.derive_attempt_billing() / record_attempt_billing()；'
                  'ledger.py 的 no_update/no_delete 触发器'),
        note=('「这次 attempt 有没有计费证据」的不可变记录（U04）。多 attempt 绝不归属到'
              '单次计费；per-attempt 计费在缺少池侧回执前不做，报告里 per_attempt_billing '
              '恒为 not_derived。'),
    ),
    Dataset(
        name='adjustment_events', klass=CLASS_LEDGER_EVENT, action=ACTION_NO_DELETE,
        retention_days=None, time_column='ts', watermark='rowid',
        reconcile='每个 request_id 最多一条（UNIQUE）；与 budget_adjustments 双写一致',
        evidence='ledger.adjust()；budget.resolve() 同事务写 budget_adjustments + adjustment_events',
        note='人工核对记录是「为什么这个 unknown 变成了这个数」的唯一证据。',
    ),
    Dataset(
        name='admin_events', klass=CLASS_LEDGER_EVENT, action=ACTION_NEVER,
        retention_days=None, time_column='ts', watermark='id',
        reconcile='previous_hash/event_hash 链可校验（ledger.verify_chain）',
        evidence='ledger.admin() / verify_chain()；ledger.py:56-59 触发器',
        note=('哈希链必须原地可校验：导出快照不改变「链在原表里」的事实，'
              '但绝不能为了减小体积删除中间行（断链就无法自证）。'),
    ),
    Dataset(
        name='budget_reservations', klass=CLASS_STATE, action=ACTION_ARCHIVE_THEN_PRUNE,
        retention_days=180, time_column='updated_at', watermark='rowid',
        reconcile=('state ∈ {settled, released} 且 charged_* 与 usage_events 一致；'
                   'state=unknown 只能人工 resolve，永不自动释放/归档'),
        evidence='budget.py::prepare/forwarded/settle/finish/resolve/unresolved',
        note=('未决（reserved/forwarded）与 unknown **不可归档、不可释放、不可删除**。'
              '归档只针对已结算且保留期满的行。'),
        archive_filter="state IN ('settled','released')",
        delete_filter="state IN ('settled','released')",
        state_column='state',
    ),
    Dataset(
        name='budget_adjustments', klass=CLASS_LEDGER_EVENT, action=ACTION_NEVER,
        retention_days=None, time_column='ts', watermark='rowid',
        reconcile='与 adjustment_events 一一对应（同一 request_id）',
        evidence='budget.resolve() 同事务写入；budget.py:21-26 建表',
        note='核对台账，无触发器但同样不应删除；如需收缩必须先由 adjustment_events 证明。',
    ),
    Dataset(
        name='usage_daily', klass=CLASS_PROJECTION, action=ACTION_KEEP,
        retention_days=None, time_column='day', watermark='rowid',
        reconcile='credit_units/prompt_tokens/completion_tokens 与 usage_events 聚合对账；可 rebuild',
        evidence='db.bump_usage / backfill_usage_from_logs / rebuild_usage_from_logs',
        note='投影表：可重建，所以「保留期」不适用；被重建前不要当权威。',
    ),
    Dataset(
        name='usage_hourly', klass=CLASS_PROJECTION, action=ACTION_PRUNE,
        retention_days=90, time_column='day', watermark='rowid',
        reconcile='按 day/hour 与 usage_daily 对账（时区口径见 db.hour_sql）',
        evidence='db.py::_prune_side_tables（90 天）；db._backfill_hourly_from_logs',
        note='列是 day 文本，删除条件用 `day < 截止日期串`（现实现如此）。',
    ),
    Dataset(
        name='audit_logs', klass=CLASS_DERIVED_LOG, action=ACTION_CAP_ROWS,
        retention_days=None, time_column='ts', watermark='id',
        reconcile='保留最新 20000 行（db._AUDIT_LOG_MAX_ROWS）；被截断的行无法再取回 → 需要归档出口',
        evidence='db.py::_prune_side_tables；db.add_audit_log（未认证路径也会写）',
        note='按行数上限截断属于「无归档删除」，应当改为「先归档再截断」。',
    ),
    Dataset(
        name='ip_access_logs', klass=CLASS_DERIVED_LOG, action=ACTION_CAP_ROWS,
        retention_days=None, time_column='ts', watermark='id',
        reconcile='blocked 计数与 reason 分布；行数上限见 db.add_ip_access_log',
        evidence='db.py::add_ip_access_log（按 id 截断）',
        note='入站被拦证据，排障依赖它；截断前应先归档。',
    ),
    Dataset(
        name='task_logs', klass=CLASS_DERIVED_LOG, action=ACTION_PRUNE,
        retention_days=90, time_column='ts', watermark='id',
        reconcile='dedup_key 唯一性；与调度结果对账',
        evidence='db.py::_prune_side_tables（90 天）',
        note='任务日志，非账本。',
    ),
    Dataset(
        name='checkin_logs', klass=CLASS_DERIVED_LOG, action=ACTION_KEEP,
        retention_days=None, time_column='ts', watermark='id',
        reconcile='签到去重（uid+day）需要完整历史，不能按 90 天删',
        evidence='db.add_checkin_log / checkin_done_since / clear_checkin_logs',
        note='签到去重依赖历史；`clear_checkin_logs` 是管理动作，不属于保留策略。',
    ),
    Dataset(
        name='api_keys', klass=CLASS_CONFIG, action=ACTION_NEVER,
        retention_days=None, time_column=None, watermark='id',
        reconcile='used_tokens/used_credit_units 与 usage_events 对账（只对账，不改写）',
        evidence='keysvc / routers/keys.py',
        note='身份与额度权威，不进保留策略；迁移/回退见 K01/K23。',
    ),
    Dataset(
        name='archive_runs', klass=CLASS_ARCHIVE_LEDGER, action=ACTION_NEVER,
        retention_days=None, time_column='started_at', watermark='id',
        reconcile='每个归档文件都有 sha256 + 水位 + 条数 + 金额和，可回查',
        evidence='migrations.py v3（本任务新增）；tools/archive_ledgers.py',
        note='归档台账本身必须永久保留，否则「归档过什么」无法证明。',
    ),
)

_BY_NAME = {d.name: d for d in DATASETS}


def dataset(name: str) -> Dataset:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f'未登记的账本/日志数据集：{name}（先在本文件分类再归档）') from None


def policy() -> list[dict]:
    """给文档/界面/健康检查用的分类表（只读，不含 SQL）。"""
    return [{
        'dataset': d.name, 'class': d.klass, 'action': d.action,
        'retention_days': d.retention_days, 'time_column': d.time_column,
        'watermark': d.watermark, 'reconcile': d.reconcile,
        'evidence': d.evidence, 'note': d.note,
    } for d in DATASETS]


# ── 守卫 ────────────────────────────────────────────────────────────────


def immutable_triggers(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    names = {str(r[0]) for r in rows}
    return {table: [t for t in triggers if t in names]
            for table, triggers in IMMUTABLE_TABLES.items()}


def assert_immutable_triggers(conn: sqlite3.Connection) -> None:
    """K24 硬约束：五张不可变表的 no_update/no_delete 触发器必须全在。

    任何归档/清理脚本在动手前先调用它；缺一个就拒绝继续（说明有人为了让删除
    通过而拆了触发器）。
    """
    have = immutable_triggers(conn)
    missing = {table: [t for t in triggers if t not in have.get(table, [])]
               for table, triggers in IMMUTABLE_TABLES.items()}
    missing = {k: v for k, v in missing.items() if v}
    if missing:
        detail = '；'.join(f'{table}: {triggers}' for table, triggers in missing.items())
        raise RuntimeError(f'不可变事件表的触发器缺失（禁止归档/删除）：{detail}')


def unknown_budget(conn: sqlite3.Connection, *, limit: int = 500,
                   now: int | None = None) -> dict:
    """未决/未知预算：只报告，**不自动释放、不归档、不删除**。"""
    now = int(time.time()) if now is None else now
    rows = conn.execute(
        "SELECT request_id,key_id,state,created_at,updated_at,charged_tokens,"
        "charged_credit FROM budget_reservations "
        "WHERE state IN ('reserved','forwarded','unknown') ORDER BY created_at LIMIT ?",
        (limit,)).fetchall()
    out = []
    for row in rows:
        out.append({
            'request_id': row[0], 'key_id': row[1], 'state': row[2],
            'age_seconds': max(0, now - int(row[3] or now)),
            'charged_tokens': row[5], 'charged_credit': row[6],
        })
    counts = {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT state, COUNT(*) FROM budget_reservations "
        "WHERE state IN ('reserved','forwarded','unknown') GROUP BY state")}
    return {'counts': counts, 'rows': out, 'auto_released': False}


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return column in {str(r[1]) for r in conn.execute(f'PRAGMA table_info({table})')}
    except sqlite3.Error:
        return False


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def verify_admin_chain(conn: sqlite3.Connection) -> dict:
    """只读校验 admin_events 哈希链（与 ledger.verify_chain 同一算法）。"""
    prev = '0' * 64
    checked = 0
    broken_at = None
    if not _table_exists(conn, 'admin_events'):
        return {'ok': False, 'checked': 0, 'broken_at': None, 'reason': '表不存在'}
    for row in conn.execute('SELECT * FROM admin_events ORDER BY id'):
        fields = [row[i] for i in (1, 2, 3, 4, 5, 6, 7)]  # ts..previous_hash
        digest = hashlib.sha256(
            json.dumps(fields, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        checked += 1
        if str(row[7]) != prev or str(row[8]) != digest:
            broken_at = int(row[0])
            break
        prev = digest
    return {'ok': broken_at is None, 'checked': checked, 'broken_at': broken_at}


# ── 归档候选与对账 ──────────────────────────────────────────────────────


def archive_candidates(conn: sqlite3.Connection, *, now: int | None = None,
                       datasets: tuple[str, ...] | None = None) -> dict:
    """每个数据集可归档（且保留期满）的行数与金额和，供归档脚本与文档使用。"""
    now = int(time.time()) if now is None else now
    names = datasets or tuple(d.name for d in DATASETS)
    out: dict[str, dict] = {}
    for name in names:
        d = dataset(name)
        if not _table_exists(conn, d.name):
            out[name] = {'exists': False}
            continue
        cutoff = _cutoff(d, now)
        where, args = _retention_where(d, cutoff)
        if d.action == ACTION_ARCHIVE_THEN_PRUNE and d.archive_filter:
            # 只有「可归档后清理」的数据集才按 archive_filter 收缩统计口径
            # （预算预留必须排除 reserved/forwarded/unknown）
            where = f'({where}) AND {d.archive_filter}' if where else d.archive_filter
        row = conn.execute(f'SELECT COUNT(*) FROM {d.name} WHERE {where}', args).fetchone() \
            if where else conn.execute(f'SELECT COUNT(*) FROM {d.name}').fetchone()
        entry = {'exists': True, 'rows': int(row[0] or 0), 'cutoff': cutoff,
                 'deletable': d.action in (ACTION_ARCHIVE_THEN_PRUNE,),
                 'archive_only': d.action in (ACTION_NEVER, ACTION_NO_DELETE)}
        if d.action == ACTION_NO_DELETE:
            entry['rows'] = int(conn.execute(
                f'SELECT COUNT(*) FROM {d.name}').fetchone()[0] or 0)
            entry['note'] = '不可变事件：只允许导出快照，删除会被触发器拒绝'
        out[name] = entry
    return out


def _cutoff(d: Dataset, now: int) -> int:
    if not d.retention_days:
        return 0
    return now - int(d.retention_days) * 86400


def _retention_where(d: Dataset, cutoff: int) -> tuple[str, tuple]:
    """保留期满的 WHERE。文本日期列（usage_hourly.day）用字符串比较。"""
    if not d.time_column or not d.retention_days:
        return '', ()
    if d.time_column == 'day':
        return f'{d.time_column} < ?', (time.strftime('%Y-%m-%d', time.localtime(cutoff)),)
    return f'{d.time_column} < ?', (cutoff,)


def reconcile(conn: sqlite3.Connection, *, now: int | None = None) -> dict:
    """保留/归档视角的对账报告：不修改任何数据。

    覆盖 K24 的验收点：可用磁盘口径的行数分布、未知预算保留、不可变触发器、
    管理事件哈希链、以及「投影 vs 权威事件」的金额差。
    """
    now = int(time.time()) if now is None else now
    report: dict = {
        'generated_at': now,
        'retention': archive_candidates(conn, now=now),
        'unknown_budget': unknown_budget(conn, now=now),
        'immutable_triggers': {k: len(v) for k, v in immutable_triggers(conn).items()},
        'admin_chain': verify_admin_chain(conn),
    }
    try:
        assert_immutable_triggers(conn)
        report['immutable_triggers_ok'] = True
    except RuntimeError as exc:
        report['immutable_triggers_ok'] = False
        report['immutable_triggers_error'] = str(exc)
    ledger = _event_totals(conn)
    projection = _projection_totals(conn)
    report['ledger_totals'] = ledger
    report['projection_totals'] = projection
    report['diff'] = {k: (projection.get(k) or 0) - (ledger.get(k) or 0)
                      for k in ('credit_units', 'tokens')}
    return report


def _event_totals(conn: sqlite3.Connection) -> dict:
    if not _table_exists(conn, 'usage_events'):
        return {'credit_units': 0, 'tokens': 0}
    row = conn.execute(
        'SELECT COALESCE(SUM(credit_units),0), '
        'COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage_events').fetchone()
    return {'credit_units': int(row[0] or 0), 'tokens': int(row[1] or 0)}


def _projection_totals(conn: sqlite3.Connection) -> dict:
    if not _table_exists(conn, 'usage_daily'):
        return {'credit_units': 0, 'tokens': 0}
    units = 'credit_units' if _has_column(conn, 'usage_daily', 'credit_units') else '0'
    row = conn.execute(
        f'SELECT COALESCE(SUM({units}),0), '
        'COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage_daily').fetchone()
    return {'credit_units': int(row[0] or 0), 'tokens': int(row[1] or 0)}
