"""QTG 旧 key → Manager 兼容迁移（K01-S1）。

## 背景

方案 (a) 确认 **Manager 是公开推理密钥的唯一权威**，QTG 只保留 UI / 平台适配。
但 QTG 已经发出去的 `sk-qtg-` key 不能要求用户整批换钥匙，也不能重置额度——
所以 Manager 必须能校验"已迁移的旧 key"。

技术前提：两边哈希算法完全相同（`sha256(plain).hexdigest()`），因此迁移**只搬哈希**，
不需要、也拿不到明文（QTG 本来也只在创建时返回一次明文）。

## 两张表各是什么角色

  · `legacy_qtg_keys`（本模块的 SCHEMA）：**迁移台账 / 来源记录**。
    保存 QTG 侧的原始语义字段（日/月限额、并发上限、平台范围、备注等），
    以及 `key_hash → api_keys.id` 的映射。`resolve()` 用它的 `prefix` 索引
    先定位，再用恒时比较校验哈希；**只有登记在这里的 key 才可能通过，
    不存在"凭前缀放行"**。
  · `api_keys`：**权威行**（canonical）。迁移时为每把旧 key 建一行
    （`source='qtg-legacy'`），目的是让 `resolve()` 返回的 dict 和普通
    `wbk_` key 完全同构、同一个 `id`：
      - `budget.prepare` 会按 `key['id']` 回查 `api_keys`，查不到直接报
        "密钥已被删除"（见 budget.py:77）；
      - `keysvc.touch` / `db.bump_usage` / `request_logs.key_id` /
        `api_key_ips` 全部按这个 id 记账；
      - 管理端列表、停用、删除、配额调整也因此对旧 key 立即生效。
    换句话说：**旧 key 在 Manager 里成为一等公民，而不是一个旁路特例。**

## 额度映射（不同单位不硬等价，规则见 LEGACY-KEYS.md）

  · QTG `daily_limit` / `monthly_limit` 是**按日/按月 token 窗口**，
    Manager 只有 `quota`（总量，无时间窗口）与 `quota_credit`（积分）。
    两者语义不同，不能把日限额塞进总量配额（那会让用户在一天内被"总量"
    永久锁死，或者反过来被放大）。因此：
      - `quota = 0`：不限总量（保守取值，含义见下）；
      - `quota_credit = 0`：QTG 没有积分概念，不臆造；
      - 日/月限额**原值保留在 `legacy_qtg_keys`**，当前 Manager 不执行
        （`LEGACY-KEYS.md` 明确标注为已知缺口 + 待决项）。
  · `max_concurrency` 同样原值保留，Manager 无 per-key 并发字段，不映射。

## 幂等与"先扩后收"

  · `legacy_qtg_keys` 用 `CREATE TABLE IF NOT EXISTS`（见 SCHEMA），
    在 `db.connect()` 的 SCHEMA 初始化路径里创建，可重复执行；
  · 迁移脚本以 `key_hash`（主键）判重：已登记的行一律跳过，
    重复执行不产生新行、不覆盖已有 `manager_key_id`；
  · 不触碰 QTG 库（只读打开），不改 QTG 代码；工具只写显式传入的 Manager 库。

## 安全约束

  · 迁移输出**只有计数与哈希前 8 位指纹**，绝不输出明文 key；
  · Manager 库里本来就没有明文，迁移不引入明文；
  · 旧 key 的"停用/过期/删除"以 Manager 侧权威行为准（旧 key 映射行的
    `enabled` 是迁移时快照，双保险：两边都启用才放行）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

# QTG 生成密钥的前缀（qtg/app/qtg/store.py: `_KEY_PREFIX = "sk-qtg-"`）。
# 迁移只认这一种前缀的已登记记录；其它前缀连查表都不查。
QTG_KEY_PREFIX = 'sk-qtg-'

# 迁移后在 api_keys.source 里打的标记，用于区分"Manager 自己创建的 key"与
# "从 QTG 迁移来的旧 key"（回退时可按它精确清理，不会误删新 key）。
SOURCE_QTG_LEGACY = 'qtg-legacy'
# legacy_qtg_keys.source 的取值：来源库类型（未来若有别的来源，可扩展）。
SOURCE_QTG_KEYS = 'qtg-keys'

# api_keys.source 列定义：**先扩后收**。存量行默认 'manager'，行为与升级前一致。
# 与 db._MIGRATIONS 里的同名列保持一致（test_legacy_keys 会校验两处一致）。
API_KEYS_SOURCE_COLUMN = 'source'
API_KEYS_SOURCE_DECL = "TEXT NOT NULL DEFAULT 'manager'"

# sha256 hex（两边算法相同）。迁移前先做形状校验：不合法哈希不写库，
# 否则会得到一条永远无法命中的垃圾记录（静默失效比报错更难查）。
_HASH_RE = re.compile(r'^[0-9a-f]{64}$')

# QTG key 的摘要指纹长度：迁移输出只打印哈希前 8 位。
FINGERPRINT_LEN = 8

# 合法平台名（QTG 侧语义）：qoder / trae / workbuddy。
# Manager 侧"生效平台"由入口决定：/qoder/v1 → qoder，/trae/v1 → trae，
# 其余 → workbuddy（见 gateway.forced_platform 与 db.add_request_log）。
QTG_PLATFORMS = ('qoder', 'trae', 'workbuddy')

# 迁移台账表。字段与任务要求一一对应，额外加了三列：
#   prefix          —— token[:12]（QTG key_prefix），resolve 的索引定位列；
#   manager_key_id  —— 对应的 api_keys.id（权威行），网关/预算/记账都按它走；
#   note            —— QTG 备注原值（Manager api_keys 没有备注列）。
SCHEMA = '''
CREATE TABLE IF NOT EXISTS legacy_qtg_keys (
  key_hash        TEXT    PRIMARY KEY,           -- sha256(明文)，与 Manager 同算法，迁移不需要明文
  prefix          TEXT    NOT NULL DEFAULT '',   -- token[:12]（QTG key_prefix），索引定位用
  qtg_key_id      TEXT    NOT NULL DEFAULT '',   -- QTG qtg_keys.id（TEXT，token_hex(8)）
  manager_key_id  INTEGER NOT NULL,              -- 对应 api_keys.id：网关键校验/预算/记账按它走
  name            TEXT    NOT NULL DEFAULT '',
  source          TEXT    NOT NULL DEFAULT 'qtg-keys',  -- 来源类型（保留出处）
  platforms       TEXT    NOT NULL DEFAULT '',   -- QTG 原值（逗号分隔），平台范围约束靠它
  models          TEXT    NOT NULL DEFAULT '',   -- QTG 原值（逗号分隔），映射规则见 LEGACY-KEYS.md
  daily_limit     INTEGER NOT NULL DEFAULT 0,    -- QTG 按日 token 上限；Manager 无对应字段，原值保留
  monthly_limit   INTEGER NOT NULL DEFAULT 0,    -- QTG 按月 token 上限；同上
  max_concurrency INTEGER NOT NULL DEFAULT 0,    -- QTG per-key 并发上限；同上
  ip_allowlist    TEXT    NOT NULL DEFAULT '',   -- QTG 原值（逗号分隔 CIDR）
  max_ips         INTEGER NOT NULL DEFAULT 0,
  expires_at      INTEGER NOT NULL DEFAULT 0,    -- unix 秒；0 = 永不过期（QTG 语义）
  enabled         INTEGER NOT NULL DEFAULT 1,    -- 迁移时的 QTG 启用状态（快照）
  note            TEXT    NOT NULL DEFAULT '',
  migrated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_legacy_qtg_prefix ON legacy_qtg_keys(prefix);
CREATE INDEX IF NOT EXISTS idx_legacy_qtg_manager ON legacy_qtg_keys(manager_key_id);
'''

# QTG qtg_keys 的字段与缺省值（旧库可能缺后加的列，缺失按默认值处理）。
_QTG_FIELDS = (
    'id', 'name', 'key_hash', 'key_prefix', 'enabled', 'platforms', 'models',
    'daily_limit', 'monthly_limit', 'max_concurrency', 'ip_allowlist',
    'max_ips', 'expires_at', 'created_at', 'last_used_at', 'note',
)
_QTG_DEFAULTS: dict[str, object] = {
    'id': '', 'name': '', 'key_hash': '', 'key_prefix': '', 'enabled': 1,
    'platforms': '', 'models': '', 'daily_limit': 0, 'monthly_limit': 0,
    'max_concurrency': 0, 'ip_allowlist': '', 'max_ips': 0, 'expires_at': 0,
    'created_at': '', 'last_used_at': '', 'note': '',
}


class MigrationError(RuntimeError):
    """迁移前置条件不满足（库不存在、不是 Manager/QTG 库、表缺失等）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def fingerprint(key_hash: object) -> str:
    """哈希前 8 位指纹：迁移输出用它定位记录，不能反推明文。"""
    return str(key_hash or '')[:FINGERPRINT_LEN]


def split_csv(raw: object) -> list[str]:
    """QTG 的逗号分隔列表（platforms/models/ip_allowlist）→ Python 列表。"""
    return [part.strip() for part in str(raw or '').split(',') if part.strip()]


def _int_or(value: object, default: int = 0) -> int:
    """把 SQLite 里可能为 None/空串/浮点的数值归一化成 int（脏值保守取默认）。"""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _text_or(value: object, default: str = '') -> str:
    return default if value is None else str(value)


def _parse_created_at(raw: object, fallback: int) -> int:
    """QTG created_at 是本地时间文本（`YYYY-MM-DD HH:MM:SS`）→ unix 秒。

    解析失败用迁移时刻兜底：created_at 只影响管理端列表排序，不值得让整批迁移失败。
    """
    text = str(raw or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    return fallback


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})')}


def _open_readonly(path: Path) -> sqlite3.Connection:
    """只读打开：迁移绝不写 QTG 库；dry-run 也绝不写 Manager 库。

    用 URI 的 `mode=ro`（而不是普通连接 + query_only）是为了连 WAL/SHM 伴生文件
    都不创建——dry-run 的语义必须是"看完就走，一个字节都不落"。
    """
    uri = path.resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 读 QTG 侧
# ---------------------------------------------------------------------------
def _read_qtg_rows(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, 'qtg_keys'):
        raise MigrationError('QTG 库里没有 qtg_keys 表，确认 --qtg-db 指向正确的库')
    columns = _table_columns(conn, 'qtg_keys')
    if 'id' not in columns or 'key_hash' not in columns:
        raise MigrationError('QTG qtg_keys 表缺少 id/key_hash，无法迁移')
    present = [name for name in _QTG_FIELDS if name in columns]
    rows: list[dict] = []
    # 按 rowid 排序保证多次运行处理顺序稳定（便于对数），不依赖 created_at 文本。
    for raw in conn.execute(f"SELECT {', '.join(present)} FROM qtg_keys ORDER BY rowid"):
        item = {name: raw[name] for name in present}
        for name, default in _QTG_DEFAULTS.items():
            item.setdefault(name, default)
        rows.append(item)
    return rows


# ---------------------------------------------------------------------------
# 迁移核心
# ---------------------------------------------------------------------------
def _build_api_keys_args(row: dict, key_hash: str, prefix: str, now: int) -> tuple:
    """构造权威行 api_keys 的插入参数。

    映射规则（与 LEGACY-KEYS.md 同步）：
      · name/prefix/expires_at/enabled/max_ips 直接对应；
      · realm 留空 = 不限制（QTG 没有版本概念，不能凭空限死；见文档"未决项"）；
      · ip_allowlist/models 从 QTG 逗号串转成 Manager 的 JSON 数组列；
      · quota = 0、quota_credit = 0：日/月限额语义不同，**不硬等价**（见模块头）；
      · used_tokens = 0：QTG 没有"总用量"概念（它按日/月窗口统计），
        总量限额既为 0，这里从 0 起算，不伪造历史用量；
      · source = 'qtg-legacy'：保留来源，回退时可按它精确清理。
    """
    expires_at = _int_or(row['expires_at'])
    return (
        _text_or(row['name']).strip()[:64] or 'qtg-legacy',
        key_hash,
        prefix,
        1 if _int_or(row['enabled'], 1) else 0,
        expires_at or None,
        _int_or(row['max_ips']),
        json.dumps(split_csv(row['ip_allowlist']), ensure_ascii=False),
        json.dumps(split_csv(row['models']), ensure_ascii=False),
        '',                       # realm：QTG 无此概念 → 不限制（保守不加限制）
        0,                        # quota：QTG 日/月限额 ≠ Manager 总量配额，不硬等价
        0,                        # used_tokens
        0.0,                      # quota_credit：QTG 无积分概念
        0.0,                      # used_credit
        _parse_created_at(row['created_at'], now),
        None,                     # upstream_id：未绑定 = 默认上游
        SOURCE_QTG_LEGACY,
    )


_API_KEYS_INSERT = (
    'INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, max_ips, '
    'ip_allowlist, models, realm, quota, used_tokens, quota_credit, used_credit, '
    'created_at, upstream_id, source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
)

_LEGACY_INSERT = (
    'INSERT OR IGNORE INTO legacy_qtg_keys(key_hash, prefix, qtg_key_id, '
    'manager_key_id, name, source, platforms, models, daily_limit, monthly_limit, '
    'max_concurrency, ip_allowlist, max_ips, expires_at, enabled, note, migrated_at) '
    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
)


def _row_status(row: dict) -> tuple[str, str]:
    """校验单行可否迁移，返回 (key_hash, prefix)；不可迁移时抛 _Skip 由调用方计数。"""
    key_hash = _text_or(row['key_hash']).strip().lower()
    if not _HASH_RE.match(key_hash):
        raise _Skip('skipped_invalid_hash')
    prefix = _text_or(row['key_prefix']).strip()
    if not prefix:
        raise _Skip('skipped_missing_prefix')
    # QTG 的 key_prefix 是 plain[:12]（store.create_key 写入）；不是 12 位就无法用
    # `token[:12]` 索引定位，迁进来也是一条永远不会被命中的记录 → 跳过并报数。
    if len(prefix) != 12 or not prefix.startswith(QTG_KEY_PREFIX):
        raise _Skip('skipped_bad_prefix')
    platforms = split_csv(row['platforms'])
    if not platforms:
        # QTG 里 platforms 为空 = 这把 key 在任何平台都用不了（proxy 会拒绝）。
        # Manager 侧 legacy_platforms=[] 会被理解成"不限制"，那是**放大权限**，
        # 所以宁可不迁移，登记为待人工确认，绝不静默放宽。
        raise _Skip('skipped_missing_platforms')
    return key_hash, prefix


class _Skip(Exception):
    def __init__(self, counter: str) -> None:
        super().__init__(counter)
        self.counter = counter


def _legacy_values(row: dict, key_hash: str, prefix: str,
                   manager_key_id: int, now: int) -> tuple:
    return (
        key_hash,
        prefix,
        _text_or(row['id'])[:64],
        manager_key_id,
        _text_or(row['name']).strip()[:64] or 'qtg-legacy',
        SOURCE_QTG_KEYS,
        _text_or(row['platforms']),
        _text_or(row['models']),
        _int_or(row['daily_limit']),
        _int_or(row['monthly_limit']),
        _int_or(row['max_concurrency']),
        _text_or(row['ip_allowlist']),
        _int_or(row['max_ips']),
        _int_or(row['expires_at']),
        1 if _int_or(row['enabled'], 1) else 0,
        _text_or(row['note'])[:200],
        now,
    )


def _pending_flags(row: dict, result: dict) -> None:
    """登记"已迁移但需要人工确认"的语义缺口（不阻断迁移，不静默）。"""
    models = split_csv(row['models'])
    if any('/' in item for item in models):
        # QTG 允许 `qoder/模型名` 这种平台前缀写法；Manager 的白名单不含平台维度，
        # 原样搬过去大概率匹配不上（表现为 400 model_not_allowed），需要 K02 定口径。
        result['pending_model_map'] += 1
    unknown = [p for p in split_csv(row['platforms']) if p not in QTG_PLATFORMS]
    if unknown or not split_csv(row['platforms']):
        result['pending_unknown_platform'] += 1


def _migrate_one(conn: sqlite3.Connection, row: dict, now: int, result: dict) -> None:
    try:
        key_hash, prefix = _row_status(row)
    except _Skip as skip:
        result[skip.counter] += 1
        result['rows'].append({'fingerprint': fingerprint(row.get('key_hash')),
                               'action': skip.counter})
        return

    fp = fingerprint(key_hash)
    existing = conn.execute(
        'SELECT manager_key_id FROM legacy_qtg_keys WHERE key_hash = ?', (key_hash,)
    ).fetchone()
    if existing is not None:
        # 幂等：已登记的行一律跳过，不覆盖 manager_key_id，也不改迁移时的快照。
        result['skipped_existing'] += 1
        result['rows'].append({'fingerprint': fp, 'action': 'skip_existing'})
        return

    canonical = conn.execute(
        'SELECT id FROM api_keys WHERE key_hash = ? ORDER BY id LIMIT 1', (key_hash,)
    ).fetchone()
    if canonical is not None:
        # 防御半途失败/重复执行：权威行已存在（例如上次插完 api_keys 后中断），
        # 复用它而不是再建一行，避免同一个哈希对应多个 id。
        manager_key_id = int(canonical['id'])
        result['reused_canonical'] += 1
    else:
        cur = conn.execute(_API_KEYS_INSERT, _build_api_keys_args(row, key_hash, prefix, now))
        manager_key_id = int(cur.lastrowid or 0)
        if manager_key_id <= 0:
            raise MigrationError(f'创建权威行失败（哈希指纹 {fp}）')

    cur = conn.execute(_LEGACY_INSERT,
                       _legacy_values(row, key_hash, prefix, manager_key_id, now))
    if int(cur.rowcount or 0) == 1:
        result['inserted'] += 1
        result['rows'].append({'fingerprint': fp, 'action': 'insert'})
        _pending_flags(row, result)
    else:
        # INSERT OR IGNORE 撞主键 = 并发/重复执行，按已存在处理。
        result['skipped_existing'] += 1
        result['rows'].append({'fingerprint': fp, 'action': 'skip_existing'})


def _dry_run_one(conn: sqlite3.Connection, row: dict, result: dict,
                 *, has_legacy: bool) -> None:
    """dry-run：只读判定"真实执行时会发生什么"，一个字节都不写。"""
    try:
        key_hash, _prefix = _row_status(row)
    except _Skip as skip:
        result[skip.counter] += 1
        result['rows'].append({'fingerprint': fingerprint(row.get('key_hash')),
                               'action': skip.counter})
        return
    fp = fingerprint(key_hash)
    if has_legacy:
        existing = conn.execute(
            'SELECT 1 FROM legacy_qtg_keys WHERE key_hash = ?', (key_hash,)
        ).fetchone()
        if existing is not None:
            result['skipped_existing'] += 1
            result['rows'].append({'fingerprint': fp, 'action': 'skip_existing'})
            return
    canonical = conn.execute(
        'SELECT id FROM api_keys WHERE key_hash = ? ORDER BY id LIMIT 1', (key_hash,)
    ).fetchone()
    if canonical is not None:
        result['reused_canonical'] += 1
    result['inserted'] += 1
    result['rows'].append({'fingerprint': fp, 'action': 'would_insert'})
    _pending_flags(row, result)


def _empty_result(dry_run: bool) -> dict:
    return {
        'dry_run': dry_run,
        'qtg_rows': 0,
        'inserted': 0,
        'skipped_existing': 0,
        'skipped_invalid_hash': 0,
        'skipped_missing_prefix': 0,
        'skipped_bad_prefix': 0,
        'skipped_missing_platforms': 0,
        'reused_canonical': 0,
        'pending_model_map': 0,
        'pending_unknown_platform': 0,
        # 预留：当前策略是"任一行出错即整批回滚"（不留下半批状态），
        # 所以 failed 恒为 0；将来若改为逐行容错，这里要真的累加。
        'failed': 0,
        'rows': [],
    }


def _ensure_manager_schema(conn: sqlite3.Connection) -> None:
    """真实执行前的"先扩"步：确认是 Manager 库，补 source 列与迁移台账表。

    与 `db.connect()` 里的 SCHEMA/_MIGRATIONS 等价（同一份 DDL 常量/字面量），
    只是让工具在 Manager 未启动时也能独立跑通。全部 CREATE IF NOT EXISTS /
    PRAGMA 判重，可重复执行。
    """
    if not _table_exists(conn, 'api_keys'):
        raise MigrationError('Manager 库里没有 api_keys 表，确认 --manager-db 指向正确的库')
    if API_KEYS_SOURCE_COLUMN not in _table_columns(conn, 'api_keys'):
        conn.execute(
            f'ALTER TABLE api_keys ADD COLUMN {API_KEYS_SOURCE_COLUMN} {API_KEYS_SOURCE_DECL}')
    conn.executescript(SCHEMA)


def migrate(qtg_db: str | Path, manager_db: str | Path, *, dry_run: bool = False) -> dict:
    """把 QTG 的 qtg_keys 迁进 Manager（幂等）。

    返回统计 dict：
      qtg_rows / inserted / skipped_existing / skipped_invalid_hash /
      skipped_missing_prefix / skipped_bad_prefix / skipped_missing_platforms /
      reused_canonical / pending_model_map / pending_unknown_platform / failed /
      rows（每行只有 {fingerprint, action} —— 不含明文、不含完整哈希）。

    dry_run=True 时只读打开两边库，`inserted` 表示"真实执行会新插入多少行"。

    调用约定：
      · 建议对 Manager 的**停机副本**执行（WAL 库边写边跑会与在线进程抢锁）；
      · 本函数只写 `manager_db`，QTG 库永远只读。
    """
    qtg_path = Path(qtg_db)
    manager_path = Path(manager_db)
    if not qtg_path.is_file():
        raise MigrationError(f'QTG 数据库不存在：{qtg_path}')
    if not manager_path.is_file():
        raise MigrationError(f'Manager 数据库不存在：{manager_path}')

    result = _empty_result(dry_run)
    now = int(time.time())

    qtg_conn = _open_readonly(qtg_path)
    try:
        rows = _read_qtg_rows(qtg_conn)
    finally:
        qtg_conn.close()
    result['qtg_rows'] = len(rows)

    if dry_run:
        manager_conn = _open_readonly(manager_path)
        try:
            if not _table_exists(manager_conn, 'api_keys'):
                raise MigrationError('Manager 库里没有 api_keys 表，确认 --manager-db 指向正确的库')
            has_legacy = _table_exists(manager_conn, 'legacy_qtg_keys')
            for row in rows:
                _dry_run_one(manager_conn, row, result, has_legacy=has_legacy)
        finally:
            manager_conn.close()
        return result

    conn = sqlite3.connect(str(manager_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_manager_schema(conn)
        # executescript 自带提交；DDL 先落盘，再开写事务搬数据。
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        try:
            for row in rows:
                _migrate_one(conn, row, now, result)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        result['legacy_total'] = int(
            conn.execute('SELECT COUNT(*) FROM legacy_qtg_keys').fetchone()[0])
        result['manager_keys_total'] = int(
            conn.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0])
        result['qtg_legacy_keys_total'] = int(conn.execute(
            'SELECT COUNT(*) FROM api_keys WHERE source = ?', (SOURCE_QTG_LEGACY,)
        ).fetchone()[0])
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# CLI（`tools/migrate_qtg_keys.py` 只是薄壳，参数与输出都在这里，便于单测）
# ---------------------------------------------------------------------------
_COUNT_FIELDS = (
    'qtg_rows', 'inserted', 'skipped_existing', 'skipped_invalid_hash',
    'skipped_missing_prefix', 'skipped_bad_prefix', 'skipped_missing_platforms',
    'reused_canonical', 'pending_model_map', 'pending_unknown_platform', 'failed',
)


def counts_line(tag: str, result: dict) -> str:
    """一行计数（顺序固定，便于日志比对）。"""
    pairs = ' '.join(f'{name}={result.get(name, 0)}' for name in _COUNT_FIELDS)
    return f'{tag} 计数: {pairs}'


def cli(argv: list[str] | None = None) -> int:
    """命令行入口：`--qtg-db --manager-db [--dry-run]`，返回进程退出码。

    输出约定：只有计数与哈希前 8 位指纹；绝不输出明文 key / 完整哈希。
    退出码：0 成功；2 前置条件不满足（库/表不对）；1 未预期错误。
    """
    parser = argparse.ArgumentParser(
        prog='migrate_qtg_keys.py',
        description='把 QTG 旧 key（sk-qtg-）迁移到 Manager，幂等、不输出明文。')
    parser.add_argument('--qtg-db', required=True, help='QTG 的 SQLite 库（只读打开）')
    parser.add_argument('--manager-db', required=True,
                        help='Manager 的 SQLite 库（只写入它）')
    parser.add_argument('--dry-run', action='store_true',
                        help='只读预演：不写 Manager 库，输出"会插入多少"')
    args = parser.parse_args(argv)

    tag = '[qtg-migrate][dry-run]' if args.dry_run else '[qtg-migrate]'
    try:
        result = migrate(args.qtg_db, args.manager_db, dry_run=args.dry_run)
    except MigrationError as exc:
        print(f'{tag} 失败（前置条件不满足）：{exc}', file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 要把意外错误变成可读输出
        print(f'{tag} 失败（未预期错误）：{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1

    for item in result['rows']:
        print(f"{tag} fp={item['fingerprint']} action={item['action']}")
    print(counts_line(tag, result))
    if args.dry_run:
        print(f'{tag} 未写库：去掉 --dry-run 才是真实迁移。')
    else:
        print(f"{tag} 迁移台账总行数={result.get('legacy_total', 0)} "
              f"Manager 密钥总行数={result.get('manager_keys_total', 0)} "
              f"其中 qtg-legacy={result.get('qtg_legacy_keys_total', 0)}")
    return 0
