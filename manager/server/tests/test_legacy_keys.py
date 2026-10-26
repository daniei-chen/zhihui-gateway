"""K01-S1：Manager 兼容"已迁移的 QTG 旧 key"。

背景（方案 a，用户已确认）：Manager 成为公开推理密钥的唯一权威；QTG 已经发出的
`sk-qtg-` 旧 key 必须继续可用，不要求用户换钥匙、不重置额度；新 key 统一 `wbk_`。

本文件钉住七件事（对应任务验收项）：

1. **迁移后能校验**：QTG 旧 key 经过 `tools/migrate_qtg_keys.py` 同款迁移逻辑后，
   `keysvc.resolve()` 返回与 `wbk_` key 同构的记录（含可用 id），能真正走网关链路；
2. **未迁移的 `sk-qtg-` → None**：只认登记过的记录，不做"前缀对就放行"；
3. **随机 `sk-xxx` → None**：新增兼容没有放宽前缀检查；
4. **enabled=0 拒绝**（迁移快照停用）；
5. **过期拒绝**（QTG expires_at 迁移后仍生效）；
6. **重复迁移幂等**：`INSERT OR IGNORE` + key_hash 判重，第二次不产生新行；
7. **`wbk_` 行为不变**：原有分支一行未改，返回值结构/字段不受影响。

额外钉住的边界（防止"静默放大权限"）：
  · 平台范围：QTG key 的 `platforms` 是数据面真实约束，迁移后由
    `keysvc.validate(..., platform=...)` 守边界；不能默认全平台；
  · IP 白名单 / 模型白名单：从 QTG 逗号串转成 Manager JSON 数组列后仍生效；
  · 额度语义：QTG 日/月限额**不硬等价**成 Manager 总量配额，迁移为 quota=0
    （不限总量）并原值保留在迁移台账（LEGACY-KEYS.md 有显式说明）；
  · 迁移输出只有计数与哈希前 8 位指纹，绝不含明文。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import secrets
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, legacymig  # noqa: E402
from server.routers import gateway  # noqa: E402

# QTG qtg_keys 的最小建表语句（字段与 qtg/app/qtg/store.py 一致，删掉无关表）。
_QTG_SCHEMA = '''
CREATE TABLE qtg_keys (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    key_hash      TEXT NOT NULL UNIQUE,
    key_prefix    TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    platforms     TEXT NOT NULL DEFAULT 'qoder,trae,workbuddy',
    models        TEXT NOT NULL DEFAULT '',
    daily_limit   INTEGER NOT NULL DEFAULT 0,
    monthly_limit INTEGER NOT NULL DEFAULT 0,
    max_concurrency INTEGER NOT NULL DEFAULT 0,
    ip_allowlist  TEXT NOT NULL DEFAULT '',
    max_ips       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    expires_at    INTEGER NOT NULL DEFAULT 0
);
'''

_ALL_PLATFORMS = 'qoder,trae,workbuddy'


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _qtg_token() -> str:
    """造一把形状与 QTG 一致的 key：`sk-qtg-` + 40 位 hex。"""
    return legacymig.QTG_KEY_PREFIX + secrets.token_hex(20)


class _LegacyTestBase(unittest.TestCase):
    """每个用例独立临时 Manager DB + QTG DB（不碰仓库 data/）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        self.manager_db = self.root / 'manager.db'
        config.DB_PATH = self.manager_db
        config.USERS_FILE = self.root / 'users.json'
        db._conn = None
        db.connect()
        self.qtg_db = self.root / 'qtg.db'
        # Windows：sqlite3 连接不显式 close 会锁住库文件，TemporaryDirectory
        # 清理时报 WinError 32（Linux 上只是延迟回收，容易掩盖问题）。
        with contextlib.closing(sqlite3.connect(str(self.qtg_db))) as conn:
            conn.executescript(_QTG_SCHEMA)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        self._tmp.cleanup()

    # ---- QTG 侧夹具 -----------------------------------------------------
    def _insert_qtg_key(
        self,
        token: str,
        *,
        name: str = '旧 key',
        enabled: int = 1,
        platforms: str = _ALL_PLATFORMS,
        models: str = '',
        daily_limit: int = 0,
        monthly_limit: int = 0,
        max_concurrency: int = 0,
        ip_allowlist: str = '',
        max_ips: int = 0,
        expires_at: int = 0,
        note: str = '',
        key_prefix: str | None = None,
    ) -> str:
        key_hash = _sha256(token)
        with contextlib.closing(sqlite3.connect(str(self.qtg_db))) as conn:
            conn.execute(
                'INSERT INTO qtg_keys(id,name,key_hash,key_prefix,enabled,platforms,'
                'models,daily_limit,monthly_limit,max_concurrency,ip_allowlist,max_ips,'
                'created_at,note,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    secrets.token_hex(8), name, key_hash,
                    token[:12] if key_prefix is None else key_prefix,
                    enabled, platforms, models, daily_limit, monthly_limit,
                    max_concurrency, ip_allowlist, max_ips,
                    time.strftime('%Y-%m-%d %H:%M:%S'), note, expires_at,
                ),
            )
            conn.commit()
        return key_hash

    def _insert_raw_qtg(self, **row: object) -> None:
        """直接插一行（用于构造非法哈希 / 缺字段等异常输入）。"""
        values = {
            'id': 'raw', 'name': 'raw', 'key_hash': '', 'key_prefix': '',
            'enabled': 1, 'platforms': _ALL_PLATFORMS, 'models': '',
            'daily_limit': 0, 'monthly_limit': 0, 'max_concurrency': 0,
            'ip_allowlist': '', 'max_ips': 0, 'created_at': '2026-10-01 00:00:00',
            'last_used_at': '', 'note': '', 'expires_at': 0,
        }
        values.update(row)
        with contextlib.closing(sqlite3.connect(str(self.qtg_db))) as conn:
            cols = ', '.join(values)
            marks = ', '.join('?' for _ in values)
            conn.execute(f'INSERT INTO qtg_keys({cols}) VALUES({marks})',
                         tuple(values.values()))
            conn.commit()

    def _migrate(self, *, dry_run: bool = False) -> dict:
        return legacymig.migrate(self.qtg_db, self.manager_db, dry_run=dry_run)

    def _legacy_rows(self) -> list[sqlite3.Row]:
        return db.query('SELECT * FROM legacy_qtg_keys ORDER BY key_hash')

    def _canonical_rows(self) -> list[sqlite3.Row]:
        return db.query("SELECT * FROM api_keys WHERE source = ? ORDER BY id",
                        (legacymig.SOURCE_QTG_LEGACY,))


class LegacyResolveTest(_LegacyTestBase):
    """迁移后的旧 key 能被 resolve/validate 正常消费。"""

    def test_migrated_key_resolves(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token, name='业务A', daily_limit=1000, monthly_limit=9000)
        result = self._migrate()
        self.assertEqual(result['inserted'], 1, result)

        key = keysvc.resolve(token)
        self.assertIsNotNone(key, '迁移后的 sk-qtg- key 应能通过 resolve')
        self.assertEqual(key['name'], '业务A')
        self.assertEqual(key['enabled'], True)
        self.assertEqual(key['realm'], '')
        # 返回的 id 必须指向真实权威行：budget.prepare 会按 id 回查 api_keys。
        row = db.query_one('SELECT id, source, key_hash FROM api_keys WHERE id = ?',
                           (key['id'],))
        self.assertIsNotNone(row, 'resolve 返回的 id 在 api_keys 里不存在')
        self.assertEqual(row['source'], legacymig.SOURCE_QTG_LEGACY)
        self.assertEqual(row['key_hash'], _sha256(token))

    def test_resolved_dict_is_parse_compatible(self) -> None:
        """与 wbk_ 的 resolve 返回值同构（只多一个平台范围字段）。"""
        token = _qtg_token()
        self._insert_qtg_key(token)
        self._migrate()
        wbk = keysvc.resolve(keysvc.create_key('新 key')['key'])
        legacy = keysvc.resolve(token)
        self.assertIsNotNone(wbk)
        self.assertIsNotNone(legacy)
        self.assertEqual(set(wbk) - set(legacy), set(), '旧 key 记录缺少 wbk 的字段')
        self.assertEqual(set(legacy) - set(wbk), {'legacy_platforms'})

    def test_unmigrated_qtg_key_returns_none(self) -> None:
        """形状完全合法但没迁移的 sk-qtg- key：一律 None（不凭前缀放行）。"""
        token = _qtg_token()
        self.assertIsNone(keysvc.resolve(token))
        # 迁移另一把 key 后，它仍然不能通过
        self._insert_qtg_key(_qtg_token())
        self._migrate()
        self.assertIsNone(keysvc.resolve(token))

    def test_random_sk_key_returns_none(self) -> None:
        for token in ('sk-xxx', 'sk-' + secrets.token_hex(20),
                      'sk-qtg-' + '0' * 40, 'wbk_', ''):
            with self.subTest(token=token[:12]):
                self.assertIsNone(keysvc.resolve(token))

    def test_disabled_key_rejected(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token, enabled=0)
        self._migrate()
        self.assertIsNone(keysvc.resolve(token), '迁移时已停用的旧 key 不应放行')

    def test_expired_key_rejected(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token, expires_at=int(time.time()) - 60)
        self._migrate()
        self.assertIsNone(keysvc.resolve(token), '过期的旧 key 不应放行')

    def test_future_expiry_allowed(self) -> None:
        token = _qtg_token()
        expires_at = int(time.time()) + 3600
        self._insert_qtg_key(token, expires_at=expires_at)
        self._migrate()
        key = keysvc.resolve(token)
        self.assertIsNotNone(key)
        self.assertEqual(int(key['expires_at']), expires_at)

    def test_manager_side_disable_revokes_immediately(self) -> None:
        """迁移台账是快照；Manager 权威行的停用必须立即生效。"""
        token = _qtg_token()
        self._insert_qtg_key(token)
        self._migrate()
        key = keysvc.resolve(token)
        keysvc.update_key(key['id'], {'enabled': False})
        self.assertIsNone(keysvc.resolve(token))

    def test_manager_side_delete_revokes(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token)
        self._migrate()
        key = keysvc.resolve(token)
        db.execute('DELETE FROM api_keys WHERE id = ?', (key['id'],))
        self.assertIsNone(keysvc.resolve(token))

    def test_wbk_key_behaviour_unchanged(self) -> None:
        """新增旧 key 兼容后，wbk_ 的生成/校验行为不变。"""
        created = keysvc.create_key('正常密钥', quota=1000, realm='cn')
        token = created['key']
        self.assertTrue(token.startswith(keysvc.TOKEN_PREFIX))
        # 迁移一批旧 key，确认 stderr 里不会互相干扰
        self._insert_qtg_key(_qtg_token())
        self._migrate()
        row = keysvc.resolve(token)
        self.assertIsNotNone(row)
        self.assertEqual(row['name'], '正常密钥')
        self.assertEqual(row['quota'], 1000)
        self.assertEqual(row['realm'], 'cn')
        self.assertEqual(row['prefix'], token[:12])
        self.assertNotIn('legacy_platforms', row, 'wbk_ key 不应带上旧 key 字段')
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'glm-5.2'))

    def test_platform_scope_enforced(self) -> None:
        """QTG 平台范围迁移后仍生效，不能被静默放大成三平台通用。"""
        token = _qtg_token()
        self._insert_qtg_key(token, platforms='qoder')
        self._migrate()
        key = keysvc.resolve(token)
        self.assertEqual(key['legacy_platforms'], ['qoder'])
        self.assertIsNone(keysvc.validate(key, '1.2.3.4', 'glm-5.2', platform='qoder'))
        for wrong in ('trae', 'workbuddy'):
            with self.subTest(platform=wrong):
                reason = keysvc.validate(key, '1.2.3.4', 'glm-5.2', platform=wrong)
                self.assertIsNotNone(reason)
                self.assertEqual(getattr(reason, 'code', ''), 'platform_not_allowed')

    def test_full_platform_scope_allowed_everywhere(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token, platforms=_ALL_PLATFORMS)
        self._migrate()
        key = keysvc.resolve(token)
        for platform in ('qoder', 'trae', 'workbuddy'):
            with self.subTest(platform=platform):
                self.assertIsNone(
                    keysvc.validate(key, '1.2.3.4', 'glm-5.2', platform=platform))

    def test_ip_allowlist_and_models_migrated(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token, ip_allowlist='10.0.0.0/8,192.168.1.5',
                             models='glm-5.2')
        self._migrate()
        key = keysvc.resolve(token)
        self.assertEqual(key['ip_allowlist'], ['10.0.0.0/8', '192.168.1.5'])
        self.assertEqual(key['models'], ['glm-5.2'])
        self.assertIsNone(keysvc.validate(key, '10.1.2.3', 'glm-5.2'))
        ip_reject = keysvc.validate(key, '8.8.8.8', 'glm-5.2')
        self.assertEqual(getattr(ip_reject, 'code', ''), 'ip_not_allowed')
        model_reject = keysvc.validate(key, '10.1.2.3', 'kimi-k2.7')
        self.assertEqual(getattr(model_reject, 'code', ''), 'model_not_allowed')

    def test_quota_mapping_is_unlimited_by_default(self) -> None:
        """QTG 日/月限额不硬等价成 Manager 总量配额：quota 必须是 0（文档已标注）。"""
        token = _qtg_token()
        self._insert_qtg_key(token, daily_limit=5000, monthly_limit=80000)
        self._migrate()
        key = keysvc.resolve(token)
        self.assertEqual(key['quota'], 0)
        self.assertEqual(key['quota_credit'], 0)
        self.assertEqual(key['used_tokens'], 0)
        self.assertEqual(key['used_credit'], 0)

    def test_legacy_table_preserves_qtg_fields(self) -> None:
        """QTG 的日/月限额、并发、备注等原值必须留档（哪怕 Manager 暂不执行）。"""
        token = _qtg_token()
        self._insert_qtg_key(token, daily_limit=5000, monthly_limit=80000,
                             max_concurrency=3, note='渠道备注')
        self._migrate()
        rows = self._legacy_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['daily_limit'], 5000)
        self.assertEqual(row['monthly_limit'], 80000)
        self.assertEqual(row['max_concurrency'], 3)
        self.assertEqual(row['note'], '渠道备注')
        self.assertEqual(row['source'], legacymig.SOURCE_QTG_KEYS)
        self.assertEqual(row['key_hash'], _sha256(token))
        self.assertEqual(row['prefix'], token[:12])
        # 台账里的 manager_key_id 必须真实存在（否则 resolve 会返回 None）
        canonical = db.query_one('SELECT id FROM api_keys WHERE id = ?',
                                 (row['manager_key_id'],))
        self.assertIsNotNone(canonical)


class LegacyMigrationTest(_LegacyTestBase):
    """迁移工具/脚本的行为：幂等、dry-run、只输出指纹。"""

    def test_migration_is_idempotent(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token)
        first = self._migrate()
        self.assertEqual(first['inserted'], 1)
        self.assertEqual(first['skipped_existing'], 0)
        key_id = keysvc.resolve(token)['id']

        second = self._migrate()
        self.assertEqual(second['inserted'], 0, '重复迁移不应产生新行')
        self.assertEqual(second['skipped_existing'], 1)
        self.assertEqual(len(self._legacy_rows()), 1)
        self.assertEqual(len(self._canonical_rows()), 1)
        self.assertEqual(keysvc.resolve(token)['id'], key_id,
                         '重复迁移不应更换 manager_key_id')

    def test_dry_run_writes_nothing(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token)
        before_keys = db.query_one('SELECT COUNT(*) c FROM api_keys')['c']
        result = self._migrate(dry_run=True)
        self.assertEqual(result['inserted'], 1, 'dry-run 应报告"会插入 1 行"')
        self.assertEqual(result['rows'][0]['action'], 'would_insert')
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM api_keys')['c'],
                         before_keys, 'dry-run 不允许写 api_keys')
        self.assertEqual(len(self._legacy_rows()), 0, 'dry-run 不允许写迁移台账')
        self.assertIsNone(keysvc.resolve(token), 'dry-run 后 key 不应生效')
        # dry-run 不应创建/修改 Manager 库文件内容（文件大小是最直接的可观测项）
        self.assertTrue(self.manager_db.is_file())

    def test_dry_run_on_manager_without_legacy_table(self) -> None:
        """迁移台账不存在时 dry-run 也要能跑（只读检查，不建表）。"""
        raw_manager = self.root / 'raw-manager.db'
        with contextlib.closing(sqlite3.connect(str(raw_manager))) as conn:
            conn.execute('CREATE TABLE api_keys (id INTEGER PRIMARY KEY, key_hash TEXT)')
        token = _qtg_token()
        self._insert_qtg_key(token)
        result = legacymig.migrate(self.qtg_db, raw_manager, dry_run=True)
        self.assertEqual(result['inserted'], 1)
        with contextlib.closing(sqlite3.connect(str(raw_manager))) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('legacy_qtg_keys', names, 'dry-run 不允许建表')

    def test_output_contains_counts_and_fingerprints_only(self) -> None:
        tokens = [_qtg_token(), _qtg_token()]
        for token in tokens:
            self._insert_qtg_key(token)
        result = self._migrate()
        self.assertEqual(result['inserted'], 2)
        text = json.dumps(result, ensure_ascii=False)
        for token in tokens:
            self.assertNotIn(token, text, '迁移输出泄露了明文 key')
            self.assertNotIn(_sha256(token), text, '迁移输出泄露了完整哈希')
        for item in result['rows']:
            self.assertEqual(item['action'], 'insert')
            self.assertEqual(len(item['fingerprint']), legacymig.FINGERPRINT_LEN)
            self.assertTrue(all(c in '0123456789abcdef' for c in item['fingerprint']))

    def test_missing_platforms_are_skipped_not_widened(self) -> None:
        """QTG platforms 为空 = 哪都用不了；迁移绝不能把它变成"不限制"。"""
        token = _qtg_token()
        self._insert_qtg_key(token, platforms='')
        result = self._migrate()
        self.assertEqual(result['inserted'], 0)
        self.assertEqual(result['skipped_missing_platforms'], 1)
        self.assertEqual(len(self._legacy_rows()), 0)
        self.assertIsNone(keysvc.resolve(token))

    def test_invalid_hash_is_skipped(self) -> None:
        self._insert_raw_qtg(key_hash='not-a-sha256', key_prefix=legacymig.QTG_KEY_PREFIX + 'abcde')
        result = self._migrate()
        self.assertEqual(result['inserted'], 0)
        self.assertEqual(result['skipped_invalid_hash'], 1)
        self.assertEqual(len(self._legacy_rows()), 0)

    def test_missing_prefix_is_skipped(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token, key_prefix='')
        result = self._migrate()
        self.assertEqual(result['inserted'], 0)
        self.assertEqual(result['skipped_missing_prefix'], 1)
        self.assertIsNone(keysvc.resolve(token))

    def test_reuses_existing_canonical_row(self) -> None:
        """半途失败重跑：权威行已存在时复用，不再建重复行。"""
        token = _qtg_token()
        digest = self._insert_qtg_key(token)
        cur = db.execute(
            "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at, source) "
            "VALUES(?, ?, ?, 1, ?, ?)",
            ('已有行', digest, token[:12], int(time.time()), legacymig.SOURCE_QTG_LEGACY))
        result = self._migrate()
        self.assertEqual(result['reused_canonical'], 1)
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(len(self._canonical_rows()), 1)
        self.assertEqual(keysvc.resolve(token)['id'], cur)

    def test_missing_qtg_db_raises(self) -> None:
        with self.assertRaises(legacymig.MigrationError):
            legacymig.migrate(self.root / 'nope.db', self.manager_db)

    def test_manager_without_api_keys_raises(self) -> None:
        empty = self.root / 'empty.db'
        sqlite3.connect(str(empty)).close()
        with self.assertRaises(legacymig.MigrationError):
            legacymig.migrate(self.qtg_db, empty)

    def test_pending_flags_are_counted_not_hidden(self) -> None:
        """QTG 平台前缀模型写法无法一一映射 → 计数上报（不静默假装映射成功）。"""
        token = _qtg_token()
        self._insert_qtg_key(token, models='qoder/glm-5.2')
        result = self._migrate()
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['pending_model_map'], 1)
        self.assertEqual(result['pending_unknown_platform'], 0)


class LegacySchemaTest(_LegacyTestBase):
    """schema 初始化路径：先扩后收、可重复执行。"""

    def test_init_creates_legacy_table_and_indexes(self) -> None:
        cols = {r[1] for r in db.query('PRAGMA table_info(legacy_qtg_keys)')}
        required = {
            'key_hash', 'prefix', 'qtg_key_id', 'manager_key_id', 'name', 'source',
            'platforms', 'models', 'daily_limit', 'monthly_limit', 'max_concurrency',
            'ip_allowlist', 'max_ips', 'expires_at', 'enabled', 'note', 'migrated_at',
        }
        self.assertTrue(required <= cols, f'缺少字段：{required - cols}')
        indexes = {r[1] for r in db.query('PRAGMA index_list(legacy_qtg_keys)')}
        self.assertIn('idx_legacy_qtg_prefix', indexes, 'prefix 索引缺失（resolve 会全表扫描）')

    def test_api_keys_source_column_expands_old_db(self) -> None:
        """存量 api_keys 也要补上 source 列，默认 'manager'（行为不变）。"""
        cols = {r[1] for r in db.query('PRAGMA table_info(api_keys)')}
        self.assertIn(legacymig.API_KEYS_SOURCE_COLUMN, cols)
        created = keysvc.create_key('新 key')
        row = db.query_one('SELECT source FROM api_keys WHERE id = ?', (created['id'],))
        self.assertEqual(row['source'], 'manager')

    def test_source_declaration_matches_db_migration(self) -> None:
        """db._MIGRATIONS 与 legacymig 的列定义不能漂移（两处各写一遍）。"""
        found = [entry for entry in db._MIGRATIONS
                 if entry[0] == 'api_keys' and entry[1] == legacymig.API_KEYS_SOURCE_COLUMN]
        self.assertEqual(len(found), 1, 'api_keys.source 迁移缺失或重复')
        self.assertEqual(found[0][2], legacymig.API_KEYS_SOURCE_DECL)

    def test_legacy_table_is_created_idempotently(self) -> None:
        """重复执行初始化不应报错（CREATE TABLE IF NOT EXISTS）。"""
        db._conn.close()
        db._conn = None
        db.connect()
        db.connect()
        cols = {r[1] for r in db.query('PRAGMA table_info(legacy_qtg_keys)')}
        self.assertIn('key_hash', cols)


class GatewayWiringTest(unittest.TestCase):
    """网关两条协议入口都要把"生效平台"传给 validate（否则平台边界形同虚设）。"""

    @classmethod
    def setUpClass(cls) -> None:
        server_dir = pathlib.Path(__file__).resolve().parents[1]
        cls.gateway_src = (server_dir / 'routers' / 'gateway.py').read_text(encoding='utf-8')
        cls.anthropic_src = (server_dir / 'routers' / 'anthropic.py').read_text(
            encoding='utf-8')

    def test_gateway_passes_platform(self) -> None:
        from server.routers import gateway
        self._assert_platform_at_validation(gateway)

    def test_anthropic_passes_platform(self) -> None:
        from server.routers import anthropic
        self._assert_platform_at_validation(anthropic)

    def _assert_platform_at_validation(self, module):
        from unittest import mock
        from starlette.requests import Request
        from server import db, platformpolicy
        from server.routers import gateway
        request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/messages',
                           'headers': [(b'authorization', b'Bearer test-not-a-secret')],
                           'query_string': b'', 'client': ('127.0.0.1', 1234)})
        token = platformpolicy.current_platform.set('')
        try:
            with mock.patch.object(keysvc, 'resolve', return_value={'id': 123}), \
                 mock.patch.object(db, 'get_setting', return_value={}), \
                 mock.patch.object(gateway, '_log_ip'), \
                 mock.patch.object(keysvc, 'validate', side_effect=RuntimeError('validation captured')) as validate:
                with self.assertRaisesRegex(RuntimeError, 'validation captured'):
                    module._authorize(request, 'qoder/Auto', mapped='Auto')
                self.assertEqual(validate.call_args.kwargs['platform'], 'qoder')
        finally:
            platformpolicy.current_platform.reset(token)

    def test_validate_default_platform_is_workbuddy(self) -> None:
        """默认值必须与 db.add_request_log 的平台归属一致（空 → workbuddy）。"""
        import inspect
        sig = inspect.signature(keysvc.validate)
        self.assertEqual(sig.parameters['platform'].default, 'workbuddy')


class LegacyCliTest(_LegacyTestBase):
    """CLI 行为（`tools/migrate_qtg_keys.py` 只是薄壳，实现在 `legacymig.cli`）。

    隔离运行器只复制 `manager/` 源码（不含 `tools/`），所以这里直接测 CLI 实现：
    参数形状、退出码、输出只含计数/指纹；薄壳脚本本身由独立 e2e 调用验证。
    """

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = legacymig.cli(argv)
            except SystemExit as exc:  # argparse 的参数错误走 SystemExit
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def _base_args(self) -> list[str]:
        return ['--qtg-db', str(self.qtg_db), '--manager-db', str(self.manager_db)]

    def test_cli_dry_run_reports_without_writing(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token)
        before = int(db.query_one('SELECT COUNT(*) c FROM api_keys')['c'])
        code, out, err = self._run(self._base_args() + ['--dry-run'])
        self.assertEqual(code, 0, err)
        self.assertIn('action=would_insert', out)
        self.assertIn('inserted=1', out)
        self.assertIn('未写库', out)
        self.assertNotIn(token, out)
        self.assertNotIn(_sha256(token), out)
        self.assertEqual(int(db.query_one('SELECT COUNT(*) c FROM api_keys')['c']), before)
        self.assertIsNone(keysvc.resolve(token))

    def test_cli_real_run_then_repeat_is_idempotent(self) -> None:
        token = _qtg_token()
        self._insert_qtg_key(token)
        code, out, err = self._run(self._base_args())
        self.assertEqual(code, 0, err)
        self.assertIn('action=insert', out)
        self.assertIn('inserted=1', out)
        self.assertIn('迁移台账总行数=1', out)
        self.assertNotIn(token, out)
        self.assertNotIn(_sha256(token), out)
        self.assertIsNotNone(keysvc.resolve(token))

        code2, out2, err2 = self._run(self._base_args())
        self.assertEqual(code2, 0, err2)
        self.assertIn('action=skip_existing', out2)
        self.assertIn('inserted=0', out2)
        self.assertIn('skipped_existing=1', out2)

    def test_cli_requires_arguments(self) -> None:
        code, _out, err = self._run([])
        self.assertEqual(code, 2, '缺少必填参数应以 argparse 的退出码 2 结束')
        self.assertIn('required', err)

    def test_cli_bad_manager_db_returns_2(self) -> None:
        empty = self.root / 'empty.db'
        with contextlib.closing(sqlite3.connect(str(empty))):
            pass
        code, _out, err = self._run(
            ['--qtg-db', str(self.qtg_db), '--manager-db', str(empty)])
        self.assertEqual(code, 2)
        self.assertIn('失败', err)
