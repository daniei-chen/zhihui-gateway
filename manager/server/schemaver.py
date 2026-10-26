"""K23 · 通用 schema 版本登记与 expand/contract 迁移引擎（纯 sqlite3，可复用到任意库）。

## 为什么需要它（现状证据）

* `manager/server/db.py:466-496` 的 `connect()` 只做两件事：`executescript(SCHEMA)`
  + `_migrate(conn)`。`_migrate`（`db.py:697-721`）是**逐列 PRAGMA 探测后补列**的
  幂等迁移，`_MIGRATIONS`（`db.py:599-694`、`db.py:1747-1759`）是它的声明表。
  这套机制本身是有效的（不是「零迁移」），但缺三样东西：
    1. 没有版本号 —— 两个库 `PRAGMA user_version` 都是 0（本轮核对 manager / qtg
       两个库文件均为 0），看不出这个库被迁到哪一步；
    2. 没有登记 —— 哪一步在什么时候、由哪个 release 执行过，事后无法追溯；
    3. 没有「先扩后收」的边界 —— 任何一步都可以是 `DROP`/`RENAME`，回退时旧代码
       直接读不了新 schema。
* `db.py:767-770` 的注释已经踩过一次坑：SQLite **不回滚 DDL**，所以「在事务里
  建表再失败回滚」并不成立，迁移必须**可重入**（重复执行无害）。本模块把这条
  变成机制：每一步都要求幂等，执行前先落 `started` 标记，执行后落 `applied`。

## 本模块给出的机制

| 机制 | 实现 | 对应任务要求 |
|---|---|---|
| 版本登记 | `schema_migrations` 表 + `PRAGMA user_version` 同步 | 迁移版本化 |
| 先扩后收 | `phase='expand'` 的步骤执行前后做 schema 指纹对比，**删列/删表/删索引一律抛 `SchemaDrift`** | 先扩后收 |
| 收缩闸门 | `phase='contract'` 的步骤默认**不执行**（记 `deferred`），必须显式 `allow_contract=True` 且自带 `contract_check` 前置校验 | 大回填/删除必须单独授权 |
| 大回填 | `backfill_batches()`：按 `schema_backfill_progress` 水位分批，每批独立事务，可断点续跑 | 大回填批处理 |
| 混合版本 | `assert_readable()`：库要求的最低读版本高于本代码支持版本时拒绝启动；旧代码读「只做过 expand」的新库仍然可用（由「先扩后收」保证） | 旧版能读新 schema 才可回退 |
| 幂等 | 每步可重复执行；执行前 `started`、执行后 `applied`；中断后重入会重跑该步 | 迁移中断重入 |
| 兼容旧机制 | 本模块**不替换** `_MIGRATIONS` 补列；`baseline` 步骤只校验既有形状后登记版本 | 不破坏现有幂等补列 |

## 明确不做

* 不执行任何 `contract` 步骤（本仓库本地候选阶段没有收缩授权）。
* 不自动回退：版本只前进。业务回退走「代码回退 + 旧代码读新 schema」，绝不
  还原旧库文件（会丢新增 key/用量/预算/审计，见审计包 §5 勘误表最后一行）。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

EXPAND = 'expand'
BACKFILL = 'backfill'
CONTRACT = 'contract'
PHASES = (EXPAND, BACKFILL, CONTRACT)

STATE_STARTED = 'started'
STATE_APPLIED = 'applied'
STATE_FAILED = 'failed'
STATE_DEFERRED = 'deferred'

REGISTRY_DDL = '''
CREATE TABLE IF NOT EXISTS schema_migrations (
  version         INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  phase           TEXT NOT NULL,
  state           TEXT NOT NULL,
  required_reader INTEGER NOT NULL DEFAULT 0,
  checksum        TEXT NOT NULL,
  app_version     TEXT NOT NULL DEFAULT '',
  started_at      INTEGER NOT NULL,
  applied_at      INTEGER,
  duration_ms     INTEGER,
  detail          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_schema_migrations_state ON schema_migrations(state);
'''

PROGRESS_DDL = '''
CREATE TABLE IF NOT EXISTS schema_backfill_progress (
  name       TEXT PRIMARY KEY,
  watermark  INTEGER NOT NULL DEFAULT 0,
  done       INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);
'''


class MigrationError(RuntimeError):
    """迁移/版本相关的可预期错误基类。"""


class MigrationFailed(MigrationError):
    """某一步执行抛异常；库内已留下 state='failed' 与 detail。"""


class SchemaDrift(MigrationError):
    """expand 步骤删除了既有对象 —— 违反「先扩后收」，必须停止并人工处理。"""


class ContractRefused(MigrationError):
    """contract 步骤缺少可证明的前置条件，拒绝执行。"""


class ReaderTooOld(MigrationError):
    """库的读版本要求高于本代码支持的版本（新库 + 旧代码）。"""


@dataclass(frozen=True)
class Step:
    """一次迁移。

    version       版本号（正整数，全局递增，也是 user_version 的值）
    name          稳定短名（出现于日志/登记表，改名会被校验为漂移）
    phase         expand / backfill / contract
    run           实际执行体；**必须幂等**（重复执行不产生错误或重复数据）
    required_reader 执行后，读这个库的代码至少要支持的版本（默认 = version）
    contract_check 仅 contract 用：返回 (ok, reason)。为 None 时拒绝执行
    detail        写进登记表的固定说明（便于事后审计）
    """

    version: int
    name: str
    run: Callable[[sqlite3.Connection], None]
    phase: str = EXPAND
    required_reader: int = 0
    contract_check: Callable[[sqlite3.Connection], tuple[bool, str]] | None = None
    detail: str = ''

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError('迁移版本必须是正整数')
        if self.phase not in PHASES:
            raise ValueError(f'未知的迁移阶段：{self.phase}')
        if not callable(self.run):
            raise ValueError('run 必须可调用')
        object.__setattr__(self, 'required_reader', self.required_reader or self.version)

    @property
    def checksum(self) -> str:
        """代码指纹：用于发现「同版本号但实现被改过」（改实现必须升版本）。"""
        code = getattr(self.run, '__code__', None)
        body = code.co_code if code is not None else b''
        raw = f'{self.version}|{self.name}|{self.phase}|{body.hex()}'.encode()
        return hashlib.sha256(raw).hexdigest()

    def row(self, app_version: str, state: str, now: int | None = None) -> tuple:
        now = int(time.time()) if now is None else now
        return (self.version, self.name, self.phase, state, self.required_reader,
                self.checksum, app_version, now)


@dataclass
class Result:
    """一次 `ensure()` 的结果，供启动日志与测试断言。"""

    applied: list[int] = field(default_factory=list)
    deferred: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)
    current_version: int = 0
    required_reader: int = 0

    def summary(self) -> str:
        return (f'applied={self.applied} deferred={self.deferred} '
                f'already={self.already} version={self.current_version} '
                f'required_reader={self.required_reader}')


# ── 基础查询 ────────────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone()
    return row is not None


def columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f'PRAGMA table_info({table})')}
    except sqlite3.Error:
        return set()


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute('PRAGMA user_version').fetchone()[0])


def ensure_registry(conn: sqlite3.Connection) -> None:
    """幂等建登记表。必须能在**任何**库（含空库）上执行。"""
    conn.executescript(REGISTRY_DDL)
    conn.executescript(PROGRESS_DDL)


def schema_fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    """当前库的「对象清单」：表 → 列，索引 → (表, 列写法)。

    只用于**比较删除**（先扩后收的守卫），不用于相等判断 —— 新增是被允许的。
    """
    tables: dict[str, list[str]] = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = str(row[0])
        tables[name] = sorted(columns_of(conn, name))
    indexes: dict[str, tuple[str, str]] = {}
    for row in conn.execute(
            "SELECT name, tbl_name, coalesce(sql, '') FROM sqlite_master WHERE type='index'"):
        indexes[str(row[0])] = (str(row[1]), str(row[2]))
    return {'tables': tables, 'indexes': indexes}


def _removed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    removed: list[str] = []
    for table, cols in before['tables'].items():
        after_cols = after['tables'].get(table)
        if after_cols is None:
            removed.append(f'表 {table} 被删除')
            continue
        gone = set(cols) - set(after_cols)
        if gone:
            removed.append(f'表 {table} 的列被删除：{sorted(gone)}')
    for index, spec in before['indexes'].items():
        if index not in after['indexes']:
            removed.append(f'索引 {index} 被删除（表 {spec[0]}）')
    return removed


def applied_versions(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    if not _table_exists(conn, 'schema_migrations'):
        return {}
    rows = conn.execute('SELECT * FROM schema_migrations').fetchall()
    # 用下标读取：调用方的 row_factory 可能是 sqlite3.Row 也可能没设，两边都能工作
    out: dict[int, tuple] = {}
    for row in rows:
        version = int(row[0])
        out[version] = tuple(row)
    return out  # type: ignore[return-value]


def _row_state(row: Sequence[Any]) -> str:
    return str(row[3])


def required_reader(conn: sqlite3.Connection) -> int:
    """库要求的**最低读代码版本** = 已应用步骤里最大的 required_reader。"""
    best = 0
    for row in applied_versions(conn).values():
        if _row_state(row) == STATE_APPLIED:
            best = max(best, int(row[4]))
    return best


def assert_readable(conn: sqlite3.Connection, code_version: int) -> None:
    """混合版本守卫：库比代码「新」到读不了的程度时拒绝。

    旧代码读「只做过 expand」的新库是安全的（只多列/多表、没有删改），所以这里
    只在**库的 required_reader 高于本代码版本**时才报错 —— 即有人给库加了旧代码
    不认识的新语义（例如收缩了列、改了列含义）。
    """
    need = required_reader(conn)
    if need > code_version:
        raise ReaderTooOld(
            f'库需要至少 v{need} 的读代码，当前代码只支持 v{code_version}；'
            '请升级代码或按发布流程回退代码（不要还原旧库文件）')


# ── 主入口 ──────────────────────────────────────────────────────────────


def _validate(steps: Sequence[Step]) -> list[Step]:
    ordered = sorted(steps, key=lambda s: s.version)
    seen: set[int] = set()
    names: set[str] = set()
    for step in ordered:
        if step.version in seen:
            raise ValueError(f'迁移版本重复：{step.version}')
        if step.name in names:
            raise ValueError(f'迁移名重复：{step.name}')
        seen.add(step.version)
        names.add(step.name)
    return ordered


def ensure(conn: sqlite3.Connection, steps: Sequence[Step], *,
           app_version: str = '', allow_contract: bool = False,
           audit_additive: bool = True) -> Result:
    """把 *steps* 应用到 *conn*；返回本次实际发生的事。

    * 幂等：已 `applied` 的步骤跳过（校验 checksum，不一致 → `SchemaDrift`）；
    * expand 步骤执行前后对比 schema 指纹，出现删除 → `SchemaDrift`（并保留证据）；
    * contract 步骤默认 `deferred`；`allow_contract=True` 时还必须有
      `contract_check` 且返回 ok，否则 `ContractRefused`；
    * 任何一步抛异常：登记 `failed` + detail 后原样抛出（不允许「静默降级启动」）。
    """
    ordered = _validate(steps)
    ensure_registry(conn)
    # N44（K23「旧代码读新 schema」）：库的 user_version 高于本代码已知的最高
    # 步骤时，说明这份库被**更新**的代码迁移过。此时继续启动等于用旧读法读新
    # 结构——列可能已改名/删除，读到的是缺列或少行，而不是报错。
    # `ReaderTooOld` 早就定义了这个语义，但 ensure() 从没抛过它。
    known_max = max((s.version for s in ordered), default=0)
    stored = user_version(conn)
    if stored > known_max > 0:
        raise ReaderTooOld(
            f'库 schema 版本 v{stored} 高于本代码已知最高 v{known_max}：'
            '这份库被更新的版本迁移过，旧代码不得继续读写。请回退到匹配的代码版本。')
    rows = applied_versions(conn)
    result = Result()

    for step in ordered:
        existing = rows.get(step.version)
        if existing is not None and _row_state(existing) == STATE_APPLIED:
            if str(existing[5]) != step.checksum:
                raise SchemaDrift(
                    f'v{step.version} {step.name} 已应用，但实现指纹变了；'
                    '改实现必须升版本号，不能原地改历史步骤')
            result.already.append(step.version)
            continue
        if step.phase == CONTRACT:
            if not allow_contract:
                conn.execute(
                    'INSERT OR REPLACE INTO schema_migrations '
                    '(version,name,phase,state,required_reader,checksum,app_version,'
                    'started_at,applied_at,duration_ms,detail) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?)',
                    (*step.row(app_version, STATE_DEFERRED), step.detail)
                )
                conn.commit()
                result.deferred.append(step.version)
                continue
            check = step.contract_check
            if check is None:
                raise ContractRefused(
                    f'v{step.version} {step.name} 是 contract 步骤但没有前置校验（contract_check）')
            ok, reason = check(conn)
            if not ok:
                raise ContractRefused(f'v{step.version} {step.name} 前置校验不通过：{reason}')

        started = step.row(app_version, STATE_STARTED)
        conn.execute(
            'INSERT OR REPLACE INTO schema_migrations '
            '(version,name,phase,state,required_reader,checksum,app_version,'
            'started_at,applied_at,duration_ms,detail) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?)',
            (*started, step.detail))
        conn.commit()

        t0 = time.perf_counter()
        before = schema_fingerprint(conn) if audit_additive and step.phase == EXPAND else None
        try:
            step.run(conn)
            conn.commit()
            after = schema_fingerprint(conn) if before is not None else None
            if before is not None:
                removed = _removed(before, after)
                if removed:
                    raise SchemaDrift(
                        f'v{step.version} {step.name} 是 expand 步骤但删除了既有对象：'
                        + '；'.join(removed))
        except Exception as exc:  # noqa: BLE001 —— 统一登记后原样抛
            msg = f'{type(exc).__name__}: {exc}'
            conn.execute(
                'UPDATE schema_migrations SET state=?,detail=?,duration_ms=? WHERE version=?',
                (STATE_FAILED, msg[:2000], int((time.perf_counter() - t0) * 1000),
                 step.version))
            conn.commit()
            if isinstance(exc, MigrationError):
                raise
            raise MigrationFailed(f'v{step.version} {step.name} 执行失败：{msg}') from exc

        conn.execute(
            'UPDATE schema_migrations SET state=?,applied_at=?,duration_ms=? WHERE version=?',
            (STATE_APPLIED, int(time.time()), int((time.perf_counter() - t0) * 1000),
             step.version))
        conn.commit()
        result.applied.append(step.version)

    # user_version 只前进，不回退（回退会掩盖「这个库曾经被迁到过更高版本」）
    highest = max([v for v, r in applied_versions(conn).items()
                   if _row_state(r) == STATE_APPLIED] or [0])
    if highest and user_version(conn) != highest:
        conn.execute(f'PRAGMA user_version={int(highest)}')
        conn.commit()
    result.current_version = highest
    result.required_reader = required_reader(conn)
    return result


def status(conn: sqlite3.Connection, steps: Sequence[Step] | None = None) -> dict[str, Any]:
    """只读的版本现状（给排障/健康检查用，不修改任何东西）。"""
    rows = applied_versions(conn)
    out: dict[str, Any] = {
        'user_version': user_version(conn),
        'required_reader': required_reader(conn),
        'applied': [], 'deferred': [], 'failed': [], 'started': [],
        'pending': [], 'drift': [],
    }
    for version in sorted(rows):
        row = rows[version]
        state = _row_state(row)
        if state == STATE_APPLIED:
            out['applied'].append({'version': version, 'name': row[1], 'phase': row[2],
                                   'app_version': row[6], 'applied_at': row[8]})
        elif state == STATE_DEFERRED:
            out['deferred'].append({'version': version, 'name': row[1]})
        elif state == STATE_FAILED:
            out['failed'].append({'version': version, 'name': row[1], 'detail': row[10]})
        elif state == STATE_STARTED:
            out['started'].append({'version': version, 'name': row[1]})
    if steps:
        for step in _validate(steps):
            if step.version not in rows:
                out['pending'].append({'version': step.version, 'name': step.name,
                                       'phase': step.phase})
            elif _row_state(rows[step.version]) == STATE_APPLIED and \
                    str(rows[step.version][5]) != step.checksum:
                out['drift'].append({'version': step.version, 'name': step.name})
    return out


# ── 大回填：分批 + 断点续跑 ──────────────────────────────────────────────


def backfill_batches(conn: sqlite3.Connection, name: str, select_sql: str,
                     apply_rows: Callable[[sqlite3.Connection, list[tuple]], int],
                     *, batch_size: int = 500, max_batches: int = 10_000,
                     app_version: str = '') -> dict[str, Any]:
    """按水位分批执行一次大回填，可中断后重入。

    `select_sql` 必须形如 `SELECT rowid, ... FROM t WHERE rowid > ? ORDER BY rowid LIMIT ?`，
    即**第一列是让水位前进的单调键**。`apply_rows(conn, rows)` 只做写入，返回影响行数。

    每批单独 `commit`，因此中断只会丢当前这一批（下一轮从水位续跑）；水位记录在
    `schema_backfill_progress`，重复调用不会重复处理已完成的批。
    """
    ensure_registry(conn)
    state = conn.execute('SELECT watermark, done FROM schema_backfill_progress WHERE name=?',
                         (name,)).fetchone()
    watermark = int(state[0]) if state else 0
    if state and int(state[1]):
        return {'name': name, 'batches': 0, 'rows': 0, 'watermark': watermark,
                'done': True, 'skipped': True}
    total_rows = 0
    batches = 0
    finished = False
    while batches < max_batches:
        rows = conn.execute(select_sql, (watermark, batch_size)).fetchall()
        if not rows:
            finished = True
            break
        payload = [tuple(r) for r in rows]
        watermark = int(payload[-1][0])
        affected = apply_rows(conn, payload) or 0
        conn.execute(
            'INSERT INTO schema_backfill_progress(name,watermark,done,updated_at) '
            'VALUES(?,?,0,?) ON CONFLICT(name) DO UPDATE SET watermark=excluded.watermark,'
            'updated_at=excluded.updated_at', (name, watermark, int(time.time())))
        conn.commit()
        total_rows += affected
        batches += 1
    # 只有真的读到空批才算做完；被 max_batches 截停时要保持 done=0，下次续跑
    conn.execute('UPDATE schema_backfill_progress SET done=?,updated_at=? WHERE name=?',
                 (1 if finished else 0, int(time.time()), name))
    conn.commit()
    return {'name': name, 'batches': batches, 'rows': total_rows, 'watermark': watermark,
            'done': finished, 'skipped': False}


# ── contract 步骤常用的前置校验 ─────────────────────────────────────────


def no_rows(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> tuple[bool, str]:
    row = conn.execute(sql, tuple(args)).fetchone()
    count = int(row[0] or 0) if row else 0
    return (count == 0, f'仍有 {count} 行不满足收缩前提')


def no_nulls(conn: sqlite3.Connection, table: str, column: str) -> tuple[bool, str]:
    row = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {column} IS NULL').fetchone()
    count = int(row[0] or 0) if row else 0
    return (count == 0, f'{table}.{column} 仍有 {count} 行 NULL')
