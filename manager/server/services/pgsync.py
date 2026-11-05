"""PostgreSQL main-database mirror, with isolated recovery artifacts.

SQLite remains authoritative. Export takes one consistent main-DB snapshot and
copies it to the explicitly configured PG destination. PG does not contain a
paired user wallet, credentials, process ownership or a complete recovery set.
Import therefore writes a new, disabled, startup-blocked artifact; it never
replaces the online database. Missing tables, column or row receipts fail closed.
Activating recovery requires the separate paired offline recovery controller.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import quote

from .. import config, db
from .errtext import err_text


logger = logging.getLogger('workbuddy.pgsync')

STATUS_FILE = config.DATA_DIR / 'pgsync-status.json'
CONFIG_KEY = 'pg_sync_config'
LAST_EXPORT_KEY = 'pg_sync_last_export_at'
LAST_IMPORT_KEY = 'pg_sync_last_import_at'

# PG 镜像里的元信息表：记录这次导出的表清单、行数、面板版本与结构指纹。
# 以 `_wb_` 开头是为了和业务表分开 —— 它不属于面板的 19 张表，
# 导出/恢复时都要显式排除（见 _is_mirror_table）。
META_TABLE = '_wb_mirror_meta'
_MIRROR_PREFIX = '_wb_'

# 每批搬运的行数。太小则往返次数多（每次 round-trip 有固定开销），太大则
# 单事务内存与体积上去、进度更新也变粗。1000 行 × 十几列对本项目约几百 KB。
BATCH = 1000

# 连接超时（秒）：见导出/恢复里的说明。探测接口用的是同一个值。
CONNECT_TIMEOUT = 8

# 状态文件里的日志上限：备份会逐表写日志，大库几十条就够，不必无限增长。
MAX_LOGS = 200

# 前端回填密码时用的占位。保存时见到它就**保持原值**——把掩码当新密码存回去
# 是「改了下配置，备份就连不上了」的经典事故。
_MASK = '********'

# 只允许在这里出现的字段被写入配置：patch 里多出来的键一律忽略，
# 免得界面（或手工调接口）塞进什么奇怪的东西。
DEFAULT_CONFIG: dict[str, Any] = {
    'enabled': False,           # 是否开启定时自动备份
    'host': '',
    'port': 5432,
    'dbname': '',
    'user': '',
    'password': '',
    'sslmode': 'prefer',
    'interval_minutes': 0,      # 0 = 只手动（即使 enabled 为真）
    'keep_local_backup': True,  # 恢复前是否自动备份本地库
}

_SECRET_FIELDS = ('password',)

# libpq 支持的 sslmode。只接受这些值：它是**写进连接串的配置**，填错时
# libpq 只会回一句难以定位的 "invalid sslmode value"，界面看不出该改成什么。
# 空值按 prefer（libpq 默认）处理。
SSLMODES = ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full')

# 导出/恢复互斥：同时只能有一个在跑（手动与定时共用同一把锁）。
_job_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


# ── 配置 ─────────────────────────────────────────────────
def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def _normalize_sslmode(value: object) -> str:
    mode = str(value or '').strip().lower() or 'prefer'
    if mode not in SSLMODES:
        raise ValueError(
            'SSL 模式必须是以下之一：' + ' / '.join(SSLMODES))
    return mode


def get_config() -> dict:
    raw = db.get_setting(CONFIG_KEY, None)
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for key in DEFAULT_CONFIG:
            if key in raw:
                cfg[key] = raw[key]
    cfg['port'] = _as_int(cfg['port'], 5432)
    cfg['interval_minutes'] = max(0, _as_int(cfg['interval_minutes'], 0))
    cfg['enabled'] = bool(cfg['enabled'])
    cfg['keep_local_backup'] = bool(cfg['keep_local_backup'])
    return cfg


def merge_form(cfg: dict, patch: dict) -> dict:
    """把界面表单的值并进一份配置里。

    密码的掩码（与空串）表示「沿用原值」：把掩码当新密码存回去是「改了下配置，
    备份就连不上了」的经典事故；空串同样保留 —— 真要把密码清空的场景（PG 免密）
    远少于「界面上没填密码就点了保存」。

    「保存」与「测试连接」都要做同一套判断（后者测的是表单里刚填、还没保存的
    值），两处各写一遍必然漂移，所以只留这一份。
    """
    out = dict(cfg)
    for key in DEFAULT_CONFIG:
        if key not in patch:
            continue
        value = patch[key]
        if key in _SECRET_FIELDS and str(value) in (_MASK, ''):
            continue
        out[key] = value
    return out


def save_config(patch: dict) -> dict:
    cfg = merge_form(get_config(), patch or {})
    cfg['port'] = _as_int(cfg['port'], 5432)
    cfg['interval_minutes'] = max(0, _as_int(cfg['interval_minutes'], 0))
    cfg['enabled'] = bool(cfg['enabled'])
    cfg['keep_local_backup'] = bool(cfg['keep_local_backup'])
    # 非法值在这里就拒掉（路由层转 400），而不是等到导出时才在任务日志里
    # 看到一句 libpq 的原文。读路径不做校验：历史脏值不该把配置接口弄挂。
    cfg['sslmode'] = _normalize_sslmode(cfg.get('sslmode'))
    db.set_setting(CONFIG_KEY, cfg)
    return cfg


def public_config() -> dict:
    """给界面的配置：密码换成掩码，附带最近两次的完成时刻。"""
    cfg = get_config()
    out = dict(cfg)
    out['password'] = _MASK if cfg['password'] else ''
    out['last_export_at'] = _as_int(db.get_setting(LAST_EXPORT_KEY, 0), 0)
    out['last_import_at'] = _as_int(db.get_setting(LAST_IMPORT_KEY, 0), 0)
    return out


def build_dsn(cfg: dict) -> str:
    """把结构化字段拼成 libpq 连接串。

    用结构化字段而不是「让用户粘一整串 DSN」：一是界面上密码要能单独掩码，
    二是 libpq 的连接串在密码含 `@` / `/` / `:` 时会被解析错位 ——
    这类故障表现为「连不上」，而错误信息里看不出是密码被截断了。
    quote(..., safe='') 把密码里的这些字符转义掉，用户不必自己操心。
    """
    host = str(cfg.get('host') or '').strip()
    if not host:
        raise ValueError('PostgreSQL 地址不能为空')
    user = quote(str(cfg.get('user') or ''), safe='')
    pwd = quote(str(cfg.get('password') or ''), safe='')
    dbname = quote(str(cfg.get('dbname') or '').strip(), safe='')
    auth = f'{user}:{pwd}@' if user else ''
    dsn = f'postgresql://{auth}{host}:{_as_int(cfg.get("port"), 5432)}/{dbname}'
    sslmode = _normalize_sslmode(cfg.get('sslmode'))
    # prefer 是 libpq 默认，写不写一样；不写能让连接串更短、日志更好看。
    if sslmode != 'prefer':
        dsn += f'?sslmode={quote(sslmode, safe="")}'
    return dsn


def _require_psycopg():
    """按需导入 PG 驱动。

    放在函数里而不是模块顶层：没配 PG 的部署不该因为「驱动没装」而起不来 ——
    这个模块会被 main.py 在启动时 import（为了挂定时任务），顶层 import 失败
    就会把整个面板拖down。
    """
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise RuntimeError(
            '未安装 PostgreSQL 驱动。请在面板所在环境执行：'
            'pip install "psycopg[binary]"'
        ) from exc
    return psycopg


# ── 类型映射与建表 ────────────────────────────────────────
# SQLite 是动态类型（列上的类型名只是「亲和性」提示），PG 是强类型。
# 本项目建表语句里只用了 INTEGER / REAL / TEXT 三种，映射如下；
# 没列出来的（NUMERIC / BLOB 等）按最保守的方式处理：数值给 NUMERIC、
# 其余给 TEXT —— 宁可宽一点，也不要因为类型猜错让恢复直接失败。
_PG_TYPE = {
    'INTEGER': 'BIGINT',
    'INT': 'BIGINT',
    'BIGINT': 'BIGINT',
    'SMALLINT': 'SMALLINT',
    'REAL': 'DOUBLE PRECISION',
    'DOUBLE': 'DOUBLE PRECISION',
    'DOUBLE PRECISION': 'DOUBLE PRECISION',
    'FLOAT': 'DOUBLE PRECISION',
    'NUMERIC': 'NUMERIC',
    'DECIMAL': 'NUMERIC',
    'TEXT': 'TEXT',
    'VARCHAR': 'TEXT',
    'CHAR': 'TEXT',
    'BLOB': 'BYTEA',
}


def pg_type(declared: object) -> str:
    base = str(declared or '').strip().upper()
    # PRAGMA 给的是 `TEXT` / `INTEGER` 这类裸类型名，但也可能带 `(11)`；
    # 去掉长度后再查表，查不到就退回 TEXT。
    base = base.split('(')[0].strip()
    return _PG_TYPE.get(base, 'TEXT')


def pg_ddl(table: str, columns: list[dict]) -> str:
    """按源库的列定义生成 PG 的 CREATE TABLE。

    NOT NULL 跟着源库走：源库那一列非空，说明写入侧一直在保证它非空，
    镜像表照抄不会出问题；反过来漏掉 NOT NULL 只是少一层校验，不影响搬运。
    主键同样照抄 —— 镜像表不是给业务查询用的，主键的意义在于「恢复时若
    数据有重复能早点发现」，而不是性能。
    """
    defs: list[str] = []
    for col in columns:
        piece = f'"{col["name"]}" {pg_type(col.get("type"))}'
        if col.get('notnull'):
            piece += ' NOT NULL'
        defs.append(piece)
    pks = [c['name'] for c in columns if c.get('pk')]
    if pks:
        defs.append('PRIMARY KEY (' + ', '.join(f'"{p}"' for p in pks) + ')')
    body = ',\n  '.join(defs)
    return f'CREATE TABLE "{table}" (\n  {body}\n)'


def _is_mirror_table(name: str) -> bool:
    return str(name).startswith(_MIRROR_PREFIX)


# 整表不随备份搬迁的控制面表。
#
# 备份的语义是「异地存一份数据」；但恢复是「把镜像灌回本地」，而镜像可能躺在
# 别人的数据库里（云 PG、共享实例、或一份被改过的备份）。`api_tokens` 一旦能被
# 镜像写回，就等于往镜像里塞一行攻击者自己算好的 sha256，恢复后直接以 admin
# 范围调用管理面。因此整表排除。
_CONTROL_TABLES = frozenset({'api_tokens'})


def _is_control_table(name: str) -> bool:
    return str(name) in _CONTROL_TABLES


# `settings` 是混合表：既有业务配置，也有凭据和更新链路。整表排除会让
# model_map / 每日任务 / 代理池 / IP 规则也一起丢掉，所以改成**按 key 白名单**
# 同步：
#   · 导出只带白名单里的 key，GitHub token、代理密码、镜像地址不会进 PG；
#   · 恢复只删/写白名单里的 key，镜像改不了本机的更新链路与凭据；
#   · 以后新增的 key 默认不同步，需要显式加进来，避免静默泄露。
_SETTING_ALLOWLIST = frozenset({
    'model_map',
    'daily_tasks_plan',
    'task_claim_schedule',
    'token_refresh_plan',
    'proxy_pool',
    'proxy_sticky',
    'global_proxy_enabled',
    'security',
})


def _filter_settings_rows(table: str, columns: list[str], rows: list) -> list:
    """只保留 settings 白名单里的行；其它表原样返回。

    拿不到 `key` 列时返回空：结构不认识就不搬，避免把整表（可能含凭据）
    当成普通数据写进镜像。
    """
    if table != 'settings':
        return rows
    try:
        index = columns.index('key')
    except ValueError:
        return []
    return [row for row in rows
            if str(row[index]) in _SETTING_ALLOWLIST]


# ── 数据源 / 目标（搬运的两端）──────────────────────────
#
# 抽出这一层是为了**可测**：导出与恢复的差别只是「谁当源、谁当目标」，
# 而搬运逻辑（分批、列投影、进度）完全一样。抽出之后，整套搬运可以用
# 「SQLite 文件 → SQLite 文件」在本地跑通，不需要真的起一个 PG。
class Source(Protocol):
    def tables(self) -> list[str]: ...
    def columns(self, table: str) -> list[dict]: ...
    def rows(self, table: str, columns: list[str], batch: int) -> Iterator[list]: ...
    def close(self) -> None: ...


class _SqliteSource:
    """本地 SQLite（导出时指向 `VACUUM INTO` 出来的快照文件）。"""

    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row

    def tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [str(r[0]) for r in rows
                if not _is_control_table(r[0])]

    def columns(self, table: str) -> list[dict]:
        rows = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        # PRAGMA table_info 的列序：cid, name, type, notnull, dflt_value, pk
        return [{'name': str(r[1]), 'type': str(r[2]),
                 'notnull': bool(r[3]), 'pk': int(r[5] or 0)} for r in rows]

    def rows(self, table: str, columns: list[str], batch: int = BATCH) -> Iterator[list]:
        collist = ', '.join(f'"{c}"' for c in columns)
        if table == 'settings' and 'key' not in columns:
            return
        predicate = (' WHERE "key" IN (' + ','.join('?' for _ in _SETTING_ALLOWLIST) + ')'
                     if table == 'settings' else '')
        cur = self._conn.execute(f'SELECT {collist} FROM "{table}"' + predicate,
                                 tuple(sorted(_SETTING_ALLOWLIST)) if table == 'settings' else ())
        while True:
            chunk = cur.fetchmany(batch)
            if not chunk:
                break
            filtered = _filter_settings_rows(
                table, columns, [tuple(r) for r in chunk])
            if filtered:
                yield filtered

    def close(self) -> None:
        self._conn.close()


class _PgSource:
    """PG 镜像（恢复时作为数据源）。"""

    def __init__(self, conn):
        self._conn = conn

    def tables(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
            return [str(r[0]) for r in cur.fetchall()
                    if not _is_mirror_table(r[0]) and not _is_control_table(r[0])]

    def columns(self, table: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "ORDER BY ordinal_position",
                (table,),
            )
            rows = cur.fetchall()
        return [{'name': str(r[0]), 'type': str(r[1]),
                 'notnull': str(r[2]).upper() == 'NO', 'pk': 0} for r in rows]

    def rows(self, table: str, columns: list[str], batch: int = BATCH) -> Iterator[list]:
        collist = ', '.join(f'"{c}"' for c in columns)
        if table == 'settings' and 'key' not in columns:
            return
        cur = self._conn.cursor()
        try:
            predicate = (' WHERE "key" IN (' + ','.join('%s' for _ in _SETTING_ALLOWLIST) + ')'
                         if table == 'settings' else '')
            if table == 'settings':
                cur.execute(f'SELECT {collist} FROM "{table}"' + predicate, tuple(sorted(_SETTING_ALLOWLIST)))
            else:
                cur.execute(f'SELECT {collist} FROM "{table}"')
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk:
                    break
                filtered = _filter_settings_rows(
                    table, columns, [tuple(r) for r in chunk])
                if filtered:
                    yield filtered
        finally:
            cur.close()

    def close(self) -> None:
        pass  # 连接由调用方管理


class _PgSink:
    """PG 镜像（导出时作为写入端）。

    每次导出都 **DROP + CREATE** 而不是 TRUNCATE：全量覆盖的语义下两者等价，
    但源侧加了列时 TRUNCATE 会留下旧结构的表，紧接着的 INSERT 直接失败。
    重建则天然跟上源库结构 —— 这正是「表结构不写死」的另一半。
    """

    def __init__(self, conn):
        self._conn = conn

    def accepts(self, table: str, columns: list[str]) -> list[str]:
        return list(columns)  # 表会被重建，来什么收什么

    def prepare(self, table: str, columns: list[dict]) -> None:
        with self._conn.cursor() as cur:
            # CASCADE：万一镜像里被别的东西（视图/外键）引用了，别让导出卡在
            # 「cannot drop table ... because other objects depend on it」。
            # 镜像表是我们自己建的，级联删掉依赖是预期行为。
            cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
            cur.execute(pg_ddl(table, columns))

    def write(self, table: str, columns: list[str], rows: list) -> int:
        if not rows:
            return 0
        collist = ', '.join(f'"{c}"' for c in columns)
        placeholders = ', '.join(['%s'] * len(columns))
        sql = f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})'
        with self._conn.cursor() as cur:
            cur.executemany(sql, rows)
        return len(rows)

    def drop(self, table: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


class _SqliteSink:
    """本地 SQLite（恢复时作为写入端）。

    只写**两边都有**的列：PG 镜像是按当时的源库结构建的，而本地库可能已经
    升过级（多了列）或退过版（少了列）。取交集能让「用一份稍旧的镜像恢复」
    仍然可用 —— 少的那几列保持默认值，而不是整场恢复失败。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def accepts(self, table: str, columns: list[str]) -> list[str]:
        local = {str(r[1]) for r in self._conn.execute(f'PRAGMA table_info("{table}")')}
        if not local:
            return []  # 本地根本没有这张表（镜像来自更新的版本）→ 整表跳过
        return [c for c in columns if c in local]

    def prepare(self, table: str, columns: list[dict]) -> None:
        pass  # 表已存在（accepts 已经确认过），结构不动

    def write(self, table: str, columns: list[str], rows: list) -> int:
        if not rows:
            return 0
        collist = ', '.join(f'"{c}"' for c in columns)
        placeholders = ', '.join(['?'] * len(columns))
        self._conn.executemany(
            f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})', rows)
        return len(rows)


def copy_data(src: Source, sink, *, on_table=None, on_batch=None) -> dict:
    """把 src 的每一张表整表搬进 sink。返回 {表名: 行数} 与合计。"""
    per_table: dict[str, int] = {}
    tables = src.tables()
    for index, table in enumerate(tables, start=1):
        src_cols = src.columns(table)
        names = [c['name'] for c in src_cols]
        wanted = sink.accepts(table, names)
        if not wanted:
            per_table[table] = 0
            if on_table:
                on_table(table, index, len(tables), 0, 'skip')
            continue
        sink.prepare(table, [c for c in src_cols if c['name'] in wanted])
        moved = 0
        for chunk in src.rows(table, wanted, BATCH):
            moved += sink.write(table, wanted, chunk)
            if on_batch:
                on_batch(table, moved)
        per_table[table] = moved
        if on_table:
            on_table(table, index, len(tables), moved, 'ok')
    return {'tables': per_table, 'rows': sum(per_table.values())}


# ── 状态文件（与 updater 的 update-status.json 同构，前端复用一套渲染）──
def read_status() -> dict:
    data: dict = {}
    try:
        if STATUS_FILE.is_file():
            data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001 —— 状态文件坏了不该让接口 500
        data = {}
    if not isinstance(data, dict) or not data:
        return {
            'running': False, 'kind': '', 'ok': None, 'step': '', 'percent': 0,
            'tables_total': 0, 'tables_done': 0, 'rows': 0,
            'logs': [], 'started_at': 0, 'finished_at': 0,
        }
    data.setdefault('logs', [])
    # 进程重启后残留的 running=true 会让界面永远转圈 —— 这里只能靠「本次进程
    # 没在跑任何任务」来判断。_job_lock 是进程内的，未持锁即视为没有任务。
    if data.get('running') and not _job_lock.locked():
        data['running'] = False
        if not data.get('finished_at'):
            data['finished_at'] = int(STATUS_FILE.stat().st_mtime)
        if data.get('ok') is None:
            data['ok'] = False
            data['step'] = '任务被中断（面板重启过）'
    return data


def _write_status(data: dict) -> None:
    try:
        config.ensure_dirs()
        tmp = STATUS_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, STATUS_FILE)
    except OSError:
        logger.exception('写 pgsync 状态文件失败')


class _Reporter:
    """边跑边写状态文件，供前端轮询。写盘做了节流（见 flush）。"""

    def __init__(self, kind: str):
        self.data: dict = {
            'running': True, 'kind': kind, 'ok': None, 'step': '准备中',
            'percent': 0, 'tables_total': 0, 'tables_done': 0, 'rows': 0,
            'logs': [], 'started_at': int(time.time()), 'finished_at': 0,
        }
        self._last = 0.0
        _write_status(self.data)

    def log(self, text: str, level: str = 'info') -> None:
        self.data['logs'].append(
            {'ts': int(time.time()), 'level': level, 'text': str(text)[:500]})
        if len(self.data['logs']) > MAX_LOGS:
            del self.data['logs'][:-MAX_LOGS]
        self.flush(force=True)

    def step(self, text: str, percent: int | None = None) -> None:
        self.data['step'] = str(text)[:200]
        if percent is not None:
            self.data['percent'] = max(0, min(100, int(percent)))
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last < 0.4:
            return
        self._last = now
        _write_status(self.data)

    def finish(self, ok: bool, message: str) -> None:
        self.data['running'] = False
        self.data['ok'] = bool(ok)
        self.data['step'] = str(message)[:200]
        if ok:
            self.data['percent'] = 100
        self.data['finished_at'] = int(time.time())
        _write_status(self.data)


# ── 导出（SQLite → PG）──────────────────────────────────
def _snapshot_path() -> Path:
    config.ensure_dirs()
    return config.DATA_DIR / f'.pgsync-snapshot-{os.getpid()}.db'



def _restore_staging_path() -> Path:
    # This is a quarantined artifact, never a live-database replacement.
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / f'pg-restore-quarantined-{secrets.token_hex(12)}.db'


def export_to_pg(cfg: dict, reporter: _Reporter) -> dict:
    psycopg = _require_psycopg()
    dsn = build_dsn(cfg)
    snapshot = _snapshot_path()
    if snapshot.exists():
        snapshot.unlink()
    with db._lock:
        # VACUUM INTO 必须在没有活动事务时执行；db.execute 每次都 commit，
        # 正常路径下连接是空闲的。这里加锁是为了挡住并发的写入。
        # 取连接也放在锁内：恢复换库时会在锁内关掉并重开连接，锁外拿到的
        # 可能是即将被关闭的旧句柄。
        db.connect().execute('VACUUM INTO ?', (str(snapshot),))
    reporter.log(f'已生成一致性快照（{snapshot.stat().st_size // 1024} KB）')
    src = _SqliteSource(snapshot)
    try:
        # connect_timeout：黑洞地址（SYN 被丢）下 libpq 会挂到 OS 默认超时（分钟级），
        # 期间任务锁一直占着、界面「进行中」不动。8 秒足够建立内网/公网连接。
        with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT) as pg:
            sink = _PgSink(pg)
            tables = src.tables()
            reporter.data['tables_total'] = len(tables)
            # 上一轮导出过、这一轮已经不在源库里的表（版本回退或表被删）：
            # 留在 PG 里会变成一份「看起来有数据、其实没人再写」的僵尸表。
            # 只删我们自己在元信息里登记过的，不碰用户库里别的东西。
            for stale in _prev_tables(pg) - set(tables):
                sink.drop(stale)
                reporter.log(f'清理镜像里已不存在的表：{stale}', 'warn')

            def on_table(table, index, total, moved, status):
                reporter.data['tables_done'] = index
                if status == 'skip':
                    reporter.log(f'跳过 {table}（源库没有这张表）', 'warn')
                else:
                    reporter.data['rows'] = int(reporter.data.get('rows') or 0) + moved
                    reporter.log(f'{table}：{moved} 行')
                reporter.step(f'导出 {table}（{index}/{total}）',
                              percent=int(index * 100 / max(1, total)))

            result = copy_data(src, sink, on_table=on_table)
            _write_meta(pg, result['tables'])
            pg.commit()
        db.set_setting(LAST_EXPORT_KEY, int(time.time()))
        return result
    finally:
        src.close()
        try:
            snapshot.unlink()
        except OSError:
            pass


def _prev_tables(pg) -> set[str]:
    """上一轮导出登记过的表清单；没有（首次导出）就返回空集。

    **必须放在 SAVEPOINT 里**，而且要用 `to_regclass` 先探存在性。
    理由是 PG 与 SQLite 在这一点上语义不同：SQLite 里一条语句失败只是那条
    语句失败，而 PG 会把**整个事务**置为 aborted —— 之后任何语句都直接报
    「current transaction is aborted」，直到 rollback。

    首次导出时元信息表根本不存在，`SELECT ... FROM _wb_mirror_meta` 必然失败；
    若只用 try/except 吞掉异常，紧接着的建表与 COPY 全部会被拒 ——
    表现为**第一次备份必定失败**，而且报错指向后面那条无辜的语句。
    """
    try:
        with pg.cursor() as cur:
            cur.execute('SAVEPOINT pgsync_prev')
            try:
                cur.execute('SELECT to_regclass(%s)', (META_TABLE,))
                found = cur.fetchone()
                if not found or found[0] is None:
                    cur.execute('RELEASE SAVEPOINT pgsync_prev')
                    return set()
                cur.execute(f'SELECT value FROM "{META_TABLE}" WHERE key = %s', ('tables',))
                row = cur.fetchone()
                cur.execute('RELEASE SAVEPOINT pgsync_prev')
            except Exception:  # noqa: BLE001 —— 表在但结构不对/已损坏
                cur.execute('ROLLBACK TO SAVEPOINT pgsync_prev')
                return set()
        if row:
            return set(json.loads(row[0]) or [])
    except Exception:  # noqa: BLE001 —— 连 SAVEPOINT 都建不了（只读事务等）
        logger.warning('读取镜像元信息失败，按首次导出处理', exc_info=True)
    return set()


def _write_meta(pg, tables: dict) -> None:
    info = {
        'tables': sorted(tables),
        'rows': {k: int(v) for k, v in tables.items()},
        'exported_at': int(time.time()),
        'app_version': _app_version(),
    }
    with pg.cursor() as cur:
        cur.execute(
            f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" '
            '(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        cur.execute(f'DELETE FROM "{META_TABLE}"')
        for key, value in info.items():
            cur.execute(f'INSERT INTO "{META_TABLE}" (key, value) VALUES (%s, %s)',
                        (key, json.dumps(value, ensure_ascii=False)))


def _app_version() -> str:
    try:
        from . import updater
        return updater.current_version()
    except Exception:  # noqa: BLE001
        return ''


# ── 恢复（PG → SQLite）──────────────────────────────────

def import_from_pg(cfg: dict, reporter: _Reporter) -> dict:
    """Materialize an untrusted mirror into a NEW, quarantined SQLite artifact.

    A PG mirror contains only the main database, without a paired user wallet,
    process-liveness proofs or credential closure. It must never replace a live
    ledger. Existing audit rows/triggers are untouched. Offline activation uses
    the separate paired recovery controller, not this web endpoint.
    """
    psycopg = _require_psycopg()
    staging = _restore_staging_path()
    schema_snapshot = staging.with_suffix('.schema.db')
    success = False
    try:
        with db._lock:
            db.connect().execute('VACUUM INTO ?', (str(schema_snapshot),))
        template = sqlite3.connect(f'file:{schema_snapshot.as_posix()}?mode=ro', uri=True)
        try:
            objects = template.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 "
                "WHEN 'index' THEN 1 ELSE 2 END,name").fetchall()
            local = _local_tables(template)
        finally:
            template.close()
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with psycopg.connect(build_dsn(cfg), connect_timeout=CONNECT_TIMEOUT) as pg:
            with pg.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            src = _PgSource(pg)
            available = set(src.tables())
            missing = sorted(local - available)
            if missing:
                raise ValueError('镜像缺少当前数据表，拒绝生成可误用的部分恢复：' + ','.join(missing))
            with pg.cursor() as cur:
                cur.execute(f'SELECT value FROM "{META_TABLE}" WHERE key=%s', ('rows',))
                row = cur.fetchone()
            expected = json.loads(row[0]) if row else None
            if not isinstance(expected, dict):
                raise ValueError('镜像缺少行数回执')
            conn = sqlite3.connect(staging)
            try:
                conn.execute('PRAGMA synchronous=FULL')
                # Tables/indexes are trusted LOCAL schema. Immutable triggers
                # are installed on the new artifact after initial loading; no
                # trigger on an existing database is removed or disabled.
                for kind, name, table, sql in objects:
                    if kind in ('table', 'index'):
                        conn.execute(sql)
                sink = _SqliteSink(conn)
                reporter.data['tables_total'] = len(local)
                counts = {}
                with conn:
                    for index, table in enumerate(sorted(local), 1):
                        names = [c['name'] for c in src.columns(table)]
                        wanted = sink.accepts(table, names)
                        if wanted != names or not wanted:
                            raise ValueError('镜像列结构不匹配：' + table)
                        moved = sum(sink.write(table, wanted, chunk)
                                    for chunk in src.rows(table, wanted, BATCH))
                        if type(expected.get(table)) is not int or moved != expected[table]:
                            raise ValueError('镜像行数回执不匹配：' + table)
                        counts[table] = moved
                        reporter.data['tables_done'] = index
                    for kind, name, table, sql in objects:
                        if kind == 'trigger':
                            conn.execute(sql)
                    if 'api_keys' in local:
                        conn.execute('UPDATE api_keys SET enabled=0')
                    conn.execute('CREATE TABLE _wb_restore_quarantine(reason TEXT NOT NULL)')
                    conn.execute('INSERT INTO _wb_restore_quarantine VALUES(?)',
                                 ('Main-only PostgreSQL mirror; paired offline activation required',))
                if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('恢复产物完整性校验失败')
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('恢复产物外键不完整')
            finally:
                conn.close()
        with staging.open('r+b') as handle:
            os.fsync(handle.fileno())
        success = True
        reporter.log('已生成隔离恢复产物；现网主账、钱包和审计未替换。必须通过配对离线恢复流程激活。', 'warn')
        return {'tables': len(counts), 'rows': sum(counts.values()),
                'artifact': staging.name, 'activation_quarantined': True,
                'live_database_replaced': False, 'complete_restore_verified': False}
    finally:
        schema_snapshot.unlink(missing_ok=True)
        if not success:
            staging.unlink(missing_ok=True)


def _local_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return {str(r[0]) for r in rows
            if not _is_mirror_table(r[0]) and not _is_control_table(r[0])}


def _local_backup() -> Path | None:
    """恢复前把当前本地库整份快照出来，供「恢复错了」时退回。"""
    try:
        config.ensure_dirs()
        path = config.DATA_DIR / f'pre-restore-{time.strftime("%Y%m%d-%H%M%S")}.db'
        with db._lock:
            db.connect().execute('VACUUM INTO ?', (str(path),))
        return path
    except Exception:  # noqa: BLE001 —— 备份失败不该阻止恢复，但要留下痕迹
        logger.exception('恢复前备份本地库失败')
        return None


# ── 任务调度 ─────────────────────────────────────────────
def start_job(kind: str) -> tuple[bool, str]:
    """启动一次导出/恢复。返回 (是否启动, 说明)。"""
    if kind not in ('export', 'import'):
        return False, '未知的任务类型'
    if not _job_lock.acquire(blocking=False):
        return False, '已有备份或恢复任务在进行中，请等它结束'
    # 拿到锁之后的任何一条提前返回都必须先还锁 —— 漏掉一处，这个功能就永久
    # 卡在「已有任务在进行中」，而且只有重启面板才能恢复。
    try:
        cfg = get_config()
        if not str(cfg.get('host') or '').strip():
            _job_lock.release()
            return False, '请先填写 PostgreSQL 地址并保存'
        thread = threading.Thread(target=_run_job, args=(kind, cfg),
                                  name=f'pgsync-{kind}', daemon=True)
        thread.start()
    except Exception:
        _job_lock.release()
        raise
    return True, '已开始'


def _run_job(kind: str, cfg: dict) -> None:
    reporter = _Reporter(kind)
    label = '导出' if kind == 'export' else '恢复'
    try:
        if kind == 'export':
            result = export_to_pg(cfg, reporter)
            reporter.finish(True, f'导出完成：{len(result["tables"])} 张表、{result["rows"]} 行')
        else:
            result = import_from_pg(cfg, reporter)
            reporter.finish(True, f'隔离恢复产物已生成：{result["tables"]} 张表；未替换现网')
        logger.info('PG %s 完成', label)
    except Exception as exc:  # noqa: BLE001 —— 任务线程的异常只能记在这里
        logger.exception('PG %s 失败', label)
        reporter.log(f'{label}失败：{err_text(exc)}', 'error')
        reporter.finish(False, f'{label}失败：{err_text(exc)}')
    finally:
        _job_lock.release()


def test_connection(cfg: dict) -> dict:
    """探测连通性。不抛异常 —— 结果直接回给界面。

    **先验配置、再要驱动**：地址没填是用户当下要改的东西，而缺驱动是环境问题。
    反过来的话，没装驱动的机器上填了个空地址，用户看到的是「未安装驱动」——
    照着它去装驱动，回来还是连不上（因为地址仍然空着）。
    """
    try:
        dsn = build_dsn(cfg)
    except ValueError as exc:
        return {'ok': False, 'message': str(exc)}
    try:
        psycopg = _require_psycopg()
    except RuntimeError as exc:
        return {'ok': False, 'message': str(exc)}
    try:
        with psycopg.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT version()')
                row = cur.fetchone()
                cur.execute('SELECT current_database(), current_user')
                who = cur.fetchone()
        version = str(row[0]).split(',')[0] if row else ''
        return {
            'ok': True,
            'message': f'连接成功：{who[0]} / {who[1]}' if who else '连接成功',
            'server_version': version,
        }
    except Exception as exc:  # noqa: BLE001 —— 驱动异常类型很杂，统一转文案
        return {'ok': False, 'message': f'连接失败：{err_text(exc)}'}


def start_scheduler() -> None:
    """定时自动导出。与手动导出共用 _job_lock，不会并发。"""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop,
                                         name='pgsync-scheduler', daemon=True)
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _scheduler_stop.set()


def _scheduler_loop() -> None:
    # 一分钟巡检一次。间隔本身由 interval_minutes 决定，这里只是「多久看一眼
    # 到点没有」——比 60 秒更密没有意义（间隔配置的最小粒度就是分钟）。
    while not _scheduler_stop.wait(60):
        try:
            cfg = get_config()
            interval = int(cfg.get('interval_minutes') or 0)
            if not cfg.get('enabled') or interval <= 0:
                continue
            if _job_lock.locked():
                continue
            last = _as_int(db.get_setting(LAST_EXPORT_KEY, 0), 0)
            if time.time() - last < interval * 60:
                continue
            ok, message = start_job('export')
            if ok:
                logger.info('PG 定时备份已启动（间隔 %d 分钟）', interval)
            else:
                logger.warning('PG 定时备份未启动：%s', message)
        except Exception:  # noqa: BLE001 —— 巡检线程绝不能因为一次异常退出
            logger.exception('PG 定时备份巡检失败')
