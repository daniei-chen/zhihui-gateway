"""Manager 主库的迁移版本登记与迁移计划（K23 / K25 / K24 / K27 的 DDL 归属地）。

## 现状（证据）

* `db.py:466-496` `connect()`：`executescript(SCHEMA)` → `_migrate(conn)`。
  这里**不替换**这两步，只在其后追加一次版本登记（见 `docs` 里的接入点）。
* `db.py:599-694` / `db.py:1747-1759` `_MIGRATIONS`：逐列 PRAGMA 探测补列，
  幂等、可重复执行 —— 本模块把它登记成 **v1 baseline**，不重复实现。
* 两个库 `PRAGMA user_version` 都是 0（本轮只读核对）；`request_logs` 只有
  `idx_logs_ts`（`db.py:163`）与 `idx_request_logs_request_id`（`db.py:721`）。

## 版本计划

| 版本 | 名称 | 阶段 | 内容 | 回退 |
|---|---|---|---|---|
| v1 | `baseline` | expand | 校验既有形状（表/关键列都在）后登记；不执行任何 DDL | 删除 `schema_migrations` 表即可，业务零影响 |
| v2 | `log_query_indexes` | expand | `idx_logs_key_id_id`、`idx_logs_unfilled_ts`（K25 实测无回归的两个索引） | `DROP INDEX`（只影响查询速度） |
| v3 | `archive_runs` | expand | 归档运行台账表（K24）；只增不删 | `DROP TABLE archive_runs` |
| v4 | `attribution_columns` | expand | `request_logs.actual_platform` / `upstream_ref`（K27 归因） | `DROP COLUMN`（SQLite 3.35+）或保留空列 |
| v5 | `key_platforms` | expand | `api_keys.platforms`（K06 每密钥平台策略） | 保留空列 |
| v6 | `request_identity` | expand | `request_logs.client_request_id` / `request_outcomes.client_request_id`（RV01） | 保留空列 |
| v7 | `task_leases` | expand | 跨进程任务租约表（K28 唯一执行权 + 发布互斥） | `DROP TABLE task_leases`（只影响调度去重，不影响业务数据） |
| v8 | `instance_leases` | expand | Manager 实例所有权租约（R3-L2 双实例准入/owner 交接） | `DROP TABLE instance_leases`（只影响交接观测，不影响业务数据） |
| v9 | `instance_reader_compatibility` | expand | 已审计 v8 新增租约表兼容 v7 读者，保存不可变认证，不改业务数据 | 保留认证 |
| v90 | `contract_units_only` | contract | 候选收缩：`usage_daily.credit` 等 REAL 冗余列在 units 列全量就位后删除 | **默认不执行**，需授权 + 批准标记 |

v90 是**候选合同步骤**，用于证明「先扩后收」的收口路径真实存在：默认
`state=deferred`，只有 `allow_contract=True` 且库内存在人工批准标记
（`settings['schema.approve.contract.v90']='1'`）时才会执行；否则抛
`ContractRefused`。本地候选阶段不放行。

## 为什么 v2 只加两个索引

`tools/k25_log_index_bench.py` 在 12 万行、真实 SQL 形状上实测（见
`design/manager/K25-日志查询与索引.md`）：

* `idx_logs_key_id_id`：`key_id` 过滤的 COUNT 21.0ms → 0.01ms；同条件深分页
  OFFSET=50000 时 19.8ms → 0.06ms；且**不改变**列表页的 `ORDER BY id DESC`
  执行路径（索引第二列就是 id，顺序天然满足）。
* `idx_logs_unfilled_ts`（部分索引 `WHERE account IS NULL`）：账号回填查询
  473ms → 162ms。
* **负面实测**：`(realm, id)` / `(status, ts)` 这类只覆盖 WHERE、不满足 ORDER BY
  的索引，会让列表页从「反向 rowid 扫描 + LIMIT 提前退出」退化成
  `USE TEMP B-TREE FOR ORDER BY` —— `realm` 列表 0.15ms → 157ms，`status` 列表
  0.23ms → 700ms。**因此这两个索引没有加入 v2**；要做必须先按 K25 文档里的
  等价改写（`COALESCE(realm,'cn')` + 表达式索引）改查询文本，实测 0.15ms 且
  COUNT 24ms → 2.8ms，但那属于 `routers/logs.py` 的改动，不在本任务写入边界内。
* 写入代价：8.59 → 11.03 ms/千行（+28%）；建索引窗口在 12 万行库上
  62.8ms + 33.7ms。取舍与测量口径见 K25 文档。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from . import schemaver
from .schemaver import BACKFILL, CONTRACT, EXPAND, Step

logger = logging.getLogger(__name__)

#: 本代码能**安全读取**的最高 schema 版本（不是「已知的最高版本」）。
#: v90 是收缩步骤：执行后 `usage_daily.credit` / `usage_hourly.credit` 会被删除，
#: 而当前代码仍在读写这两列（`db.bump_usage`、`ledger` 的对账逻辑），所以本代码
#: 支持 v9（含已审计 v8→v7 读者兼容认证）。等读路径全部切到 `credit_units` 后再把它提到 90。
CODE_SCHEMA_VERSION = 9

#: baseline 必须存在的表（顺序无关）。缺任何一张说明这不是 Manager 主库，
#: 宁可拒绝启动也不要往一个陌生库里写版本号。
#:
#: **只列 `db.connect()` 自己能保证建出来的表**（`db.SCHEMA` + `ledger.SCHEMA`
#: + `request_outcomes` + `readiness_probe`）。`budget_reservations` /
#: `budget_adjustments` 由 `budget.initialize()` 在用到预算时才建，不在
#: `db.connect()` 的保证范围内 —— 把它们列进来会让「只初始化了主库」的调用方
#: （部分测试、只读工具）误判为损坏库。
BASELINE_TABLES = (
    'api_keys', 'request_logs', 'usage_daily', 'usage_hourly', 'audit_logs',
    'settings', 'api_tokens', 'ip_access_logs', 'task_logs', 'checkin_logs',
    'usage_events', 'forwarding_events', 'admin_events', 'attempt_events',
    'adjustment_events', 'request_outcomes', 'readiness_probe',
)

#: baseline 必须存在的列（`_migrate` 的产出；缺了说明升级未完成）。
BASELINE_COLUMNS = {
    'request_logs': ('request_id', 'endpoint', 'terminal_state', 'platform',
                     'usage_source', 'usage_complete', 'first_byte_ms', 'account'),
    'api_keys': ('realm', 'upstream_id', 'quota_credit', 'used_credit'),
    'usage_daily': ('realm', 'credit_units'),
}


def _app_version() -> str:
    """当前 release 标识；与 `observability._release_id` 读同一个文件。"""
    version = os.environ.get('WB_APP_VERSION', '').strip()
    if version:
        return version
    try:
        return Path('/opt/workbuddy-manager/.version').read_text(encoding='utf-8').strip() or 'unknown'
    except OSError:
        return 'unknown'


# ── v1 baseline ────────────────────────────────────────────────────────


def _baseline(conn: sqlite3.Connection) -> None:
    """只校验、不建表：`SCHEMA` + `_migrate` 已经把形状建好了。

    这一步的意义是**把既有的幂等补列机制纳入版本序列**：从此「库迁到第几步」
    有唯一答案，而不是靠逐列 PRAGMA 猜。任何一项缺失都说明这不是可服务的
    Manager 库（例如升级中断、或误指到别的库），直接失败比继续更安全。
    """
    existing = {str(r[0]) for r in
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in BASELINE_TABLES if t not in existing]
    if missing:
        raise schemaver.MigrationError(
            'baseline 校验失败：缺少表 ' + ', '.join(missing)
            + '（这不是可用的 Manager 主库，拒绝登记版本号）')
    for table, columns in BASELINE_COLUMNS.items():
        have = schemaver.columns_of(conn, table)
        gone = [c for c in columns if c not in have]
        if gone:
            raise schemaver.MigrationError(
                f'baseline 校验失败：{table} 缺少列 {gone}（升级未完成？）')


# ── v2 查询索引（K25 实测集合）─────────────────────────────────────────


def _log_query_indexes(conn: sqlite3.Connection) -> None:
    """K25：按真实查询形状补两个**无回归**索引。

    索引取舍的证据见模块 docstring 与 `design/manager/K25-日志查询与索引.md`；
    这里只强调一点：**不要**在这里顺手加 `(realm, id)` / `(status, ts)` ——
    实测会让列表页退化到临时 B 树排序（0.15ms → 157ms）。要加必须先改
    `routers/logs.py` 的谓词文本（等价改写），那是另一个变更。
    """
    conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_key_id_id '
                 'ON request_logs(key_id, id)')
    # 部分索引：账号回填只关心 account IS NULL 的行（db.attach_request_accounts）
    conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_unfilled_ts '
                 'ON request_logs(ts) WHERE account IS NULL')


# ── v3 归档台账（K24）──────────────────────────────────────────────────


def _archive_runs(conn: sqlite3.Connection) -> None:
    """归档/对账的运行台账：每次归档一行，记录水位、条数、校验和与状态。

    为什么要单独一张表：K24 要求「归档完整性、对账、取回检验后才清理」，
    这三件事必须有**可核对的记录**。归档文件本身带 sha256 清单，数据库这行
    指回那个文件 + 水位（watermark_id），断点续跑时据此判断「已归档到哪」。
    """
    conn.execute('''
        CREATE TABLE IF NOT EXISTS archive_runs (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          dataset      TEXT    NOT NULL,
          cutoff_ts    INTEGER NOT NULL,
          started_at   INTEGER NOT NULL,
          finished_at  INTEGER,
          watermark_id INTEGER,
          rows_archived INTEGER NOT NULL DEFAULT 0,
          sha256       TEXT,
          path         TEXT,
          credit_units_sum INTEGER,
          token_sum    INTEGER,
          state        TEXT    NOT NULL,
          verified_at  INTEGER,
          approved_by  TEXT,
          detail       TEXT    NOT NULL DEFAULT ''
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_archive_runs_dataset_started '
                 'ON archive_runs(dataset, started_at)')


# ── v4 归因列（K27）────────────────────────────────────────────────────


def _attribution_columns(conn: sqlite3.Connection) -> None:
    """K27：把「路由决定的实际平台/上游」落到可查询的列上。

    * `actual_platform`：实际被路由到的平台（workbuddy / qoder / trae / …）。
      既有的 `platform` 列记录的是**入口/强制平台**（`gateway.forced_platform`
      或默认 workbuddy），两者不是一回事：模型名前缀 `qoder/` 可以把请求路由到
      Qoder，而入口仍是 `/v1/chat/completions`。
    * `upstream_ref`：解析出来的上游标识，形如 `id@host`（只有 id 与主机名，
      不含 URL 路径、证书或任何凭据）。
    * 两列都**可空**：NULL = 未路由/未记录。不写默认值 —— 把未知写成 'workbuddy'
      正是 K27 要消除的「静默填零」。
    """
    columns = schemaver.columns_of(conn, 'request_logs')
    for name, decl in (('actual_platform', 'TEXT'), ('upstream_ref', 'TEXT')):
        if name not in columns:
            conn.execute(f'ALTER TABLE request_logs ADD COLUMN {name} {decl}')


def _key_platforms(conn: sqlite3.Connection) -> None:
    if 'platforms' not in schemaver.columns_of(conn, 'api_keys'):
        conn.execute("ALTER TABLE api_keys ADD COLUMN platforms TEXT NOT NULL "
                     "DEFAULT '[\"workbuddy\",\"qoder\",\"trae\"]'")


# ── v6 请求身份拆分（RV01）─────────────────────────────────────────────


def _request_identity(conn: sqlite3.Connection) -> None:
    """RV01：把「调用方关联 ID」从「服务端执行 ID」里拆出来，落成可空列。

    修复前外部 `X-Request-ID` 被直接当成执行 ID，同时是
    `request_logs.request_id` / `request_outcomes.request_id` /
    `budget_reservations.request_id`（主键）的来源，于是出现三类可复现故障：
    同 key 重发同一 ID → 第二个请求撞预算主键 500；不同 key 用同一 ID → 共用
    全局命名空间；匿名 401 先占 ID → 后续成功请求的终态被 `INSERT OR IGNORE`
    丢弃、停在 401。修复后执行 ID 一律由 `observability.new_request_id()` 生成，
    调用方的 ID 只作为相关性列保存（见 observability 模块 docstring）。

    为什么两列都**可空、不回填**：历史行（以及没有带头的请求）的
    `client_request_id` 未知，NULL 就是「不知道」。用执行 ID 或任何猜测值回填
    会把「未提供」伪造成「提供了某个 ID」，正好是 RV01 要消除的那类串号。
    这也满足 expand 阶段只增不删、旧读法不受影响。

    幂等：用 `schemaver.columns_of` 探测，列已存在就跳过（新库建表语句里就有
    `request_outcomes.client_request_id`，`request_logs` 则统一由本步骤补）。
    """
    for table in ('request_logs', 'request_outcomes'):
        if 'client_request_id' not in schemaver.columns_of(conn, table):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN client_request_id TEXT')


# ── v7 跨进程任务租约（K28）───────────────────────────────────────────


def _task_leases(conn: sqlite3.Connection) -> None:
    """K28：唯一执行权租约表。

    为什么需要单独一张表（而不是继续用 `settings.last_run` 戳）：`last_run`
    只能表达「这一小时跑过了」，无法表达「**谁**正在跑、跑到什么时候、能不能
    接手」。两个 Manager 进程（滚动升级的新旧进程、误起的第二个实例）会各自
    判定到点并各跑一遍；领奖/做任务不是幂等操作，重复上报就是真实重复行为。

    形状与语义（owner / fence / lease_until）见 `server/tasklease.py` 的模块
    docstring；本步骤只负责建表，DDL 与模块共用一份（`tasklease.initialize`），
    避免迁移里再抄一遍然后漂移。

    幂等：`CREATE TABLE/INDEX IF NOT EXISTS`；旧库升级只多一张表，不影响既有
    读法（v1 baseline 也不要求这张表存在）。
    """
    from . import tasklease
    tasklease.initialize(conn)


# ── v8 实例租约（R3-L2）───────────────────────────────────────────────


def _instance_leases(conn: sqlite3.Connection) -> None:
    """R3-L2：Manager 实例的所有权租约（双实例准入 / owner 交接）。

    为什么要独立一张表（方案 B，Lead 已批准）：`tasklease.active_leases()` 没有
    kind 过滤，`begin_publish()` 直接用它在发布前判断「是否有活跃任务」。
    把实例行塞进 `task_leases` 会让发布被自己的实例租约永久阻塞 —— 那段握手是
    K28 用 31/31 + 竞态 + 崩溃证据验证过的发布门禁路径，不值得为省一张表去改它。

    形状与状态机见 `server/instancelease.py` 的模块 docstring；本步骤只建表，
    DDL 与模块共用一份（`instancelease.initialize`）。

    幂等：`CREATE TABLE/INDEX IF NOT EXISTS`；旧库升级只多一张表、无回填，
    不影响既有读法（v1 baseline 也不要求这张表存在）。
    """
    from . import instancelease
    instancelease.initialize(conn)


# ── v90 候选收缩步骤（默认永远 deferred）──────────────────────────────


APPROVAL_KEY = 'schema.approve.contract.v90'


def _approved(conn: sqlite3.Connection, key: str) -> tuple[bool, str]:
    """contract 步骤的人工批准标记（写在 settings 里，只由授权发布步骤写入）。

    没有标记就必须拒绝 —— 收缩不可逆，不能因为「代码里写了 contract」就自动跑。
    """
    try:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    except sqlite3.Error:
        return False, 'settings 表不可读'
    if row is None:
        return False, f'缺少人工批准标记 {key}'
    value = row[0]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8', 'replace')
    text = str(value).strip().strip('"')
    return (text == '1', f'批准标记 {key}={text!r}（需要 "1"）')


def _contract_check_units_only(conn: sqlite3.Connection) -> tuple[bool, str]:
    approved, reason = _approved(conn, APPROVAL_KEY)
    if not approved:
        return approved, reason
    ok, reason = schemaver.no_nulls(conn, 'usage_daily', 'credit_units')
    if not ok:
        return ok, reason
    # 收缩前必须确认所有读路径已切到 units 列：这里用「旧列之和 = units 之和」对账，
    # 无法自动证明代码不再读旧列，所以仍然要求上面的显式批准标记。
    row = conn.execute(
        'SELECT COUNT(*) FROM usage_daily '
        'WHERE credit_units IS NOT NULL AND ABS(credit_units - CAST(ROUND(credit*1000000) AS INTEGER)) > 1'
    ).fetchone()
    mismatched = int(row[0] or 0) if row else 0
    return (mismatched == 0, f'{mismatched} 行的 credit 与 credit_units 不一致')


def _contract_units_only(conn: sqlite3.Connection) -> None:
    """删除 usage_daily/usage_hourly 的 REAL credit 冗余列（需批准）。"""
    for table in ('usage_hourly', 'usage_daily'):
        columns = schemaver.columns_of(conn, table)
        if 'credit' in columns and 'credit_units' in columns:
            conn.execute(f'ALTER TABLE {table} DROP COLUMN credit')


def _instance_reader_compatibility(conn: sqlite3.Connection) -> None:
    """v9: certify the exact v8 additive schema for the unmodified v7 reader."""
    from . import schema_compat
    v8 = next(step for step in MANAGER_STEPS if step.version == 8)
    row = conn.execute('SELECT app_version FROM schema_migrations WHERE version=9').fetchone()
    schema_compat.certify(conn, expected_checksum=v8.checksum,
                          release=str(row[0] or '') if row else '')


MANAGER_STEPS: tuple[Step, ...] = (
    Step(version=1, name='baseline', phase=EXPAND, run=_baseline,
         detail='登记既有 SCHEMA + _MIGRATIONS 幂等补列机制形成的形状'),
    Step(version=2, name='log_query_indexes', phase=EXPAND, run=_log_query_indexes,
         detail='K25：idx_logs_key_id_id + idx_logs_unfilled_ts（实测无回归）'),
    Step(version=3, name='archive_runs', phase=EXPAND, run=_archive_runs,
         detail='K24：归档运行台账'),
    Step(version=4, name='attribution_columns', phase=EXPAND, run=_attribution_columns,
         detail='K27：request_logs.actual_platform / upstream_ref'),
    Step(version=5, name='key_platforms', phase=EXPAND, run=_key_platforms,
         detail='K06: explicit per-key platform policy; legacy restrictions remain intersected'),
    Step(version=6, name='request_identity', phase=EXPAND, run=_request_identity,
         detail='RV01: client_request_id columns; external X-Request-ID is correlation only, never a primary key'),
    Step(version=7, name='task_leases', phase=EXPAND, run=_task_leases,
         detail='K28: cross-process task lease (owner/fence/lease_until) for unique execution + publish handshake'),
    Step(version=8, name='instance_leases', phase=EXPAND, run=_instance_leases,
         detail='R3-L2: Manager instance ownership lease (role/drain_snapshot/fence) for dual-instance handoff'),
    Step(version=9, name='instance_reader_compatibility', phase=EXPAND,
         run=_instance_reader_compatibility, required_reader=7,
         detail='Audited v8 additive lease layout is compatible with v7; no data restore or user_version rewind'),
    Step(version=90, name='contract_units_only', phase=CONTRACT, run=_contract_units_only,
         contract_check=_contract_check_units_only,
         detail='候选收缩：units 列全量就位后删除 REAL credit（需人工批准）'),
)


def ensure(conn: sqlite3.Connection, *, allow_contract: bool = False,
           app_version: str | None = None) -> schemaver.Result:
    """在 `db.connect()` 里调用：把 MANAGER_STEPS 应用到当前库。

    失败时抛 `schemaver.MigrationError`（含具体版本与原因），**不吞异常**：
    带着未知 schema 启动比启动失败更危险。
    """
    result = schemaver.ensure(conn, MANAGER_STEPS,
                              app_version=app_version or _app_version(),
                              allow_contract=allow_contract)
    if 9 in schemaver.applied_versions(conn) and schemaver.applied_versions(conn)[9][3] == 'applied':
        from . import schema_compat
        v8 = next(step for step in MANAGER_STEPS if step.version == 8)
        schema_compat.validate(conn, expected_checksum=v8.checksum)
    if result.applied:
        logger.info('schema 迁移已应用：%s（user_version=%s）',
                    result.applied, result.current_version)
    if result.deferred:
        logger.debug('schema 收缩步骤保持 deferred：%s', result.deferred)
    return result


def status(conn: sqlite3.Connection) -> dict:
    """只读版本现状（排障/健康检查用）；包含本代码的版本号与漂移检测。"""
    out = schemaver.status(conn, MANAGER_STEPS)
    out['code_schema_version'] = CODE_SCHEMA_VERSION
    has_settings = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone())
    out['contract_approved'] = _approved(conn, APPROVAL_KEY)[0] if has_settings else False
    return out


def plan() -> list[dict]:
    """迁移计划（给文档/健康检查/测试用，不含执行体）。"""
    return [{'version': s.version, 'name': s.name, 'phase': s.phase,
             'requires_approval': s.phase == CONTRACT, 'detail': s.detail}
            for s in MANAGER_STEPS]
