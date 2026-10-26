"""K23 · schema 版本登记与 expand/contract 迁移引擎的测试。

覆盖审计包 K23 的专项场景：空库、当前库、上一版库（模拟）、重复执行、
迁移中断重入、旧代码读新 schema、收缩步骤默认不执行。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, migrations, schemaver  # noqa: E402


class _TempDB(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k23.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    # ── 小工具 ──────────────────────────────────────────────────────
    def _close_conn(self) -> None:
        if db._conn is not None:
            db._conn.close()
            db._conn = None

    def _reopen(self) -> None:
        self._close_conn()
        db.connect()

    def _objects(self) -> set[str]:
        return {str(r[0]) for r in db.query(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}


class FreshDatabaseTest(_TempDB):
    def test_version_is_stamped_and_registry_filled(self) -> None:
        self.assertEqual(schemaver.user_version(db._conn), migrations.CODE_SCHEMA_VERSION)
        rows = {int(r['version']): r for r in db.query('SELECT * FROM schema_migrations')}
        self.assertEqual(sorted(rows), [1, 2, 3, 4, 5, 6, 7, 8, 9, 90])
        for version in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            self.assertEqual(rows[version]['state'], 'applied',
                             f'v{version} 应为 applied')
        # 收缩步骤默认不执行（K23：本地候选阶段没有收缩授权）
        self.assertEqual(rows[90]['state'], 'deferred')

    def test_expand_objects_exist(self) -> None:
        objects = self._objects()
        self.assertIn('idx_logs_key_id_id', objects)
        self.assertIn('idx_logs_unfilled_ts', objects)
        self.assertIn('archive_runs', objects)
        columns = {r[1] for r in db.query('PRAGMA table_info(request_logs)')}
        self.assertIn('actual_platform', columns)
        self.assertIn('upstream_ref', columns)
        # RV01（v6 request_identity）：关联 ID 是独立可空列，不参与主键。
        for table in ('request_logs', 'request_outcomes'):
            with self.subTest(table=table):
                columns = {r[1] for r in db.query(f'PRAGMA table_info({table})')}
                self.assertIn('client_request_id', columns)
        # K28（v7 task_leases）：跨进程任务租约表 + 查询索引。
        self.assertIn('task_leases', objects)
        self.assertIn('idx_task_leases_until', objects)
        lease_columns = {r[1] for r in db.query('PRAGMA table_info(task_leases)')}
        self.assertEqual(lease_columns,
                         {'task', 'owner', 'lease_until', 'fence', 'updated_at', 'detail'})
        # R3-L2（v8 instance_leases）：实例所有权租约表（与 task_leases 独立）。
        self.assertIn('instance_leases', objects)
        for index in ('idx_instance_leases_until', 'idx_instance_leases_role_until',
                      'idx_instance_leases_host'):
            self.assertIn(index, objects)
        instance_columns = {r[1] for r in db.query('PRAGMA table_info(instance_leases)')}
        self.assertEqual(instance_columns,
                         {'instance_id', 'owner', 'lease_until', 'fence', 'updated_at',
                          'source_sha', 'build_id', 'host', 'pid', 'start_ticks', 'role',
                          'phase', 'drain_snapshot', 'detail'})

    def test_second_ensure_is_a_noop(self) -> None:
        result = migrations.ensure(db._conn)
        self.assertEqual(result.applied, [])
        self.assertEqual(result.deferred, [90])

    def test_status_reports_plan_and_no_drift(self) -> None:
        status = migrations.status(db._conn)
        self.assertEqual(status['code_schema_version'], migrations.CODE_SCHEMA_VERSION)
        self.assertEqual(status['failed'], [])
        self.assertEqual(status['drift'], [])
        self.assertEqual(status['pending'], [])
        self.assertFalse(status['contract_approved'])
        self.assertTrue(any(p['version'] == 90 and p['requires_approval']
                            for p in migrations.plan()))


class UpgradeFromPreviousVersionTest(_TempDB):
    """模拟「上一版库」：把 v2..v4 的对象拆掉 + 版本归零，再升级。"""

    def _downgrade_to_v1(self) -> None:
        conn = db._conn
        conn.execute('DROP INDEX IF EXISTS idx_logs_key_id_id')
        conn.execute('DROP INDEX IF EXISTS idx_logs_unfilled_ts')
        conn.execute('DROP TABLE IF EXISTS archive_runs')
        conn.execute('ALTER TABLE request_logs DROP COLUMN actual_platform')
        conn.execute('ALTER TABLE request_logs DROP COLUMN upstream_ref')
        # RV01：模拟「没有 client_request_id」的旧库，验证 v6 真的会补列。
        conn.execute('ALTER TABLE request_logs DROP COLUMN client_request_id')
        conn.execute('ALTER TABLE request_outcomes DROP COLUMN client_request_id')
        # K28（v7）：旧库没有租约表，升级必须补建。
        conn.execute('DROP TABLE IF EXISTS task_leases')
        # R3-L2（v8）：实例租约表同样是新增，旧库没有。
        conn.execute('DROP TABLE IF EXISTS instance_leases')
        # The simulated historical database predates the v9 certificate.
        conn.execute('DROP TABLE IF EXISTS schema_reader_compatibility')
        conn.execute('DROP TABLE schema_migrations')
        conn.execute('DROP TABLE schema_backfill_progress')
        conn.execute('PRAGMA user_version=0')
        conn.commit()

    def test_upgrade_preserves_data_and_restores_expand_objects(self) -> None:
        import time
        now = int(time.time())
        db.execute("INSERT INTO api_keys(name,key_hash,prefix,created_at) VALUES('k','h','p',1)")
        db.execute("INSERT INTO request_logs(ts,key_id,model,request_id) "
                   "VALUES(?,?,?,?)", (now, 1, 'm', 'rid-1'))
        # ts 必须是「近期」：request_outcomes 有 90 天保留期，启动时的侧表清理
        # 会删掉过期行，用 1 会让这条断言变成在测清理逻辑而不是迁移。
        db.execute("INSERT INTO request_outcomes(request_id,ts) VALUES('rid-1',?)", (now,))
        self._downgrade_to_v1()
        self.assertEqual(schemaver.user_version(db._conn), 0)
        self._reopen()
        self.assertEqual(schemaver.user_version(db._conn), migrations.CODE_SCHEMA_VERSION)
        row = db.query_one('SELECT * FROM request_logs WHERE request_id=?', ('rid-1',))
        self.assertIsNotNone(row, '升级不能丢数据')
        self.assertIsNone(row['actual_platform'], '新列对历史行必须是 NULL，不能伪造')
        self.assertIsNone(row['client_request_id'],
                          'RV01：旧行没有关联 ID，NULL 就是「不知道」，不得回填猜测')
        outcome = db.query_one('SELECT * FROM request_outcomes WHERE request_id=?', ('rid-1',))
        self.assertIsNotNone(outcome, '升级不能丢 request_outcomes 行')
        self.assertIsNone(outcome['client_request_id'])
        self.assertIn('task_leases', self._objects(), 'v7 升级必须补建租约表')
        self.assertIn('instance_leases', self._objects(), 'v8 升级必须补建实例租约表')
        self.assertIn('idx_logs_key_id_id', self._objects())

    def test_interrupted_step_is_rerun(self) -> None:
        self._downgrade_to_v1()
        self._reopen()
        # 手工制造「中断」现场：登记表说 v2 正在跑，但索引其实不存在
        db.execute("DELETE FROM schema_migrations WHERE version=2")
        db.execute("DROP INDEX IF EXISTS idx_logs_key_id_id")
        db.execute("DROP INDEX IF EXISTS idx_logs_unfilled_ts")
        db.execute(
            "INSERT INTO schema_migrations(version,name,phase,state,required_reader,"
            "checksum,app_version,started_at) VALUES(2,'log_query_indexes','expand',"
            "'started',2,'deadbeef','test',1)")
        self._close_conn()
        db.connect()
        self.assertIn('idx_logs_key_id_id', self._objects())
        row = db.query_one('SELECT state FROM schema_migrations WHERE version=2')
        self.assertEqual(row['state'], 'applied')

    def test_failed_step_is_recorded_and_retried(self) -> None:
        db.execute("UPDATE schema_migrations SET state='failed', detail='simulated' "
                   "WHERE version=3")
        self._close_conn()
        db.connect()
        row = db.query_one('SELECT state FROM schema_migrations WHERE version=3')
        self.assertEqual(row['state'], 'applied')

    def test_old_reader_style_sql_still_works(self) -> None:
        """旧代码只认识旧列：升级后仍能读（先扩后收的直接证据）。"""
        db.execute("INSERT INTO api_keys(name,key_hash,prefix,created_at) VALUES('k','h','p',1)")
        db.execute("INSERT INTO request_logs(ts,key_id,model,status,prompt_tokens,"
                   "completion_tokens) VALUES(?,?,?,?,?,?)", (1, 1, 'm', 200, 10, 5))
        row = db.query_one('SELECT id,ts,key_id,model,status,prompt_tokens,'
                           'completion_tokens FROM request_logs WHERE key_id=1')
        self.assertEqual(row['prompt_tokens'], 10)
        # 旧插入路径（不写新列）也必须可用
        db.execute('INSERT INTO request_logs(ts,model) VALUES(?,?)', (2, 'm2'))
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM request_logs')['c'], 2)


class DriftAndGuardTest(_TempDB):
    def test_checksum_change_is_drift(self) -> None:
        db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=1")
        with self.assertRaises(schemaver.SchemaDrift):
            migrations.ensure(db._conn)

    def test_contract_without_approval_is_refused(self) -> None:
        with self.assertRaises(schemaver.ContractRefused):
            migrations.ensure(db._conn, allow_contract=True)

    def test_contract_with_approval_applies_then_reader_is_too_old(self) -> None:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                   (migrations.APPROVAL_KEY, '"1"'))
        migrations.ensure(db._conn, allow_contract=True)
        columns = {r[1] for r in db.query('PRAGMA table_info(usage_daily)')}
        self.assertNotIn('credit', columns)
        self.assertIn('credit_units', columns)
        # 收缩之后本代码（只支持读到 v4）必须拒绝启动，而不是带着误解读库
        self._close_conn()
        with self.assertRaises(RuntimeError):
            db.connect()

    def test_expand_step_cannot_drop_objects(self) -> None:
        """先扩后收守卫：expand 阶段删表 → SchemaDrift（且登记为 failed）。"""
        path = Path(self._tmp.name) / 'guard.db'
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript('CREATE TABLE keep_me(x INTEGER);'
                               'CREATE TABLE drop_me(y INTEGER);')
            steps = (
                schemaver.Step(version=1, name='drop_something', phase=schemaver.EXPAND,
                               run=lambda c: c.execute('DROP TABLE drop_me')),
            )
            with self.assertRaises(schemaver.SchemaDrift):
                schemaver.ensure(conn, steps, app_version='test')
            row = conn.execute(
                'SELECT state, detail FROM schema_migrations WHERE version=1').fetchone()
            self.assertEqual(row['state'], 'failed')
            self.assertIn('删除', row['detail'])
        finally:
            conn.close()


class BackfillTest(_TempDB):
    def test_batched_backfill_resumes_without_duplicates(self) -> None:
        db.execute('CREATE TABLE IF NOT EXISTS k23_backfill_demo('
                   'id INTEGER PRIMARY KEY, value INTEGER)')
        for i in range(1, 8):
            db.execute('INSERT INTO k23_backfill_demo(id,value) VALUES(?,0)', (i,))
        applied: list[int] = []

        def apply_rows(conn, rows):
            for row in rows:
                conn.execute('UPDATE k23_backfill_demo SET value=1 WHERE id=?', (row[0],))
                applied.append(int(row[0]))
            return len(rows)

        first = schemaver.backfill_batches(
            db._conn, 'demo', 'SELECT id FROM k23_backfill_demo WHERE id>? ORDER BY id LIMIT ?',
            apply_rows, batch_size=2, max_batches=1)
        self.assertEqual(first['rows'], 2)
        second = schemaver.backfill_batches(
            db._conn, 'demo', 'SELECT id FROM k23_backfill_demo WHERE id>? ORDER BY id LIMIT ?',
            apply_rows, batch_size=2)
        self.assertFalse(second['skipped'])
        third = schemaver.backfill_batches(
            db._conn, 'demo', 'SELECT id FROM k23_backfill_demo WHERE id>? ORDER BY id LIMIT ?',
            apply_rows, batch_size=2)
        self.assertTrue(third['skipped'], '完成后重复调用必须直接跳过')
        self.assertEqual(sorted(applied), list(range(1, 8)), '不能重复、不能漏')
        self.assertEqual(db.query_one('SELECT COUNT(*) c FROM k23_backfill_demo '
                                      'WHERE value=0')['c'], 0)


if __name__ == '__main__':
    unittest.main()


class MixedVersionMatrixTest(_TempDB):
    """K23 补全矩阵：QTG 只读兼容 + 迁移不破坏触发器/外部依赖。

    已有测试覆盖「空库 / 当前库 / 上一版模拟 / 重复执行 / 中断重入 / 旧读法」。
    任务卡还要求两条此前未实测的：
      · **QTG 只读兼容**：QTG 通过 `scope=read` 或直连 SQL 读同一份库时，
        迁移后仍能读到它依赖的列/表；
      · **不破坏数据/触发器/外部依赖**：迁移过程不得删掉不可变触发器。
    """

    @property
    def conn(self):
        return db._conn

    def test_readonly_consumer_survives_migration(self) -> None:
        """模拟 QTG 的只读查询：迁移前后都能读到它需要的列。"""
        # QTG 直连 SQL 时用的查询（见 qtg/app/qtg/adapters/workbuddy.py）
        read_queries = [
            'SELECT COUNT(*) FROM request_logs WHERE ts >= ?',
            'SELECT SUM(credit) FROM request_logs WHERE ts >= ?',
            'SELECT SUM(prompt_tokens), SUM(completion_tokens) FROM request_logs WHERE ts >= ?',
            'SELECT account, COUNT(*) FROM request_logs WHERE ts >= ? GROUP BY account',
            'SELECT DAY, credit FROM usage_daily',   # 注意：day 是列名，大小写不敏感
            'SELECT key_id, SUM(credit) FROM request_logs GROUP BY key_id',
            'SELECT * FROM api_keys',
            'SELECT * FROM checkin_logs',
            'SELECT * FROM task_logs',
            'SELECT * FROM audit_logs',
        ]
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at) '
                   "VALUES(1,'k','h','p',0)")
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,credit,'
                   'prompt_tokens,completion_tokens,request_id) '
                   "VALUES(?,1,'m',200,0.5,10,5,'r1')", (int(__import__('time').time()),))
        for sql in read_queries:
            with self.subTest(sql=sql):
                args = (0,) if '?' in sql else ()
                try:
                    db.query(sql, args)
                except sqlite3.Error as exc:
                    self.fail(f'QTG 只读查询在迁移后失败：{sql} -> {exc}')

    def test_migration_preserves_immutable_triggers(self) -> None:
        """迁移不得删掉 ledger 的不可变触发器（否则历史可被篡改）。"""
        from server import ledger, retention
        before = retention.immutable_triggers(self.conn) if hasattr(
            retention, 'immutable_triggers') else None
        # 触发一次完整迁移检查
        migrations.ensure(self.conn)
        after = retention.immutable_triggers(self.conn) if before is not None else None
        if before is not None:
            self.assertEqual(before, after, '迁移改变了不可变触发器集合')
        # 独立确认：每个 immutable 表的 UPDATE/DELETE 触发器都还在
        names = {str(r[0]) for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        for table in ('usage_events', 'forwarding_events', 'attempt_events',
                      'adjustment_events', 'admin_events'):
            with self.subTest(table=table):
                self.assertIn(f'{table}_no_update', names)
                self.assertIn(f'{table}_no_delete', names)

    def test_migration_preserves_row_data(self) -> None:
        """迁移前后行数不变（只加列/索引，不删数据）。"""
        import time
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at) '
                   "VALUES(1,'k','h','p',0)")
        for i in range(20):
            db.execute('INSERT INTO request_logs(ts,key_id,model,status,credit,'
                       'request_id) VALUES(?,1,?,200,?,?)',
                       (int(time.time()) + i, 'm', 0.1 * i, f'r{i}'))
        before = db.query_one('SELECT COUNT(*) c, SUM(credit) s FROM request_logs')
        migrations.ensure(self.conn)
        after = db.query_one('SELECT COUNT(*) c, SUM(credit) s FROM request_logs')
        self.assertEqual(after['c'], before['c'], '迁移丢了行')
        self.assertAlmostEqual(after['s'], before['s'], places=6, msg='迁移改了金额')

    def test_ensure_is_idempotent_across_reopen(self) -> None:
        """已迁移的库重开再 ensure 不应再产生变更。"""
        migrations.ensure(self.conn)
        v1 = schemaver.user_version(self.conn)
        plan1 = migrations.status(self.conn)
        self._reopen()
        migrations.ensure(self.conn)
        v2 = schemaver.user_version(self.conn)
        plan2 = migrations.status(self.conn)
        self.assertEqual(v1, v2, '重复 ensure 改变了版本号')
        self.assertEqual(plan1.get('pending'), plan2.get('pending'),
                         '重复 ensure 产生了新的待执行步骤')

    def test_unknown_future_version_is_refused(self) -> None:
        """库版本高于代码已知最高步骤时必须拒绝启动（N44）。

        修复前 `ensure()` 完全不看 `PRAGMA user_version`，只会"按步骤补齐"。
        一份被更新代码迁移过的库，拿回旧代码启动时不会报错，而是用旧读法
        去读可能已改名/删除的列 —— 表现为缺列或少行，而不是明确的版本错误。
        """
        migrations.ensure(self.conn)
        # 代码已知最高步骤是 v90（contract）；库被迁到 v91 就是"更新的版本"。
        known = max(s.version for s in migrations.MANAGER_STEPS)
        self.conn.execute(f'PRAGMA user_version = {known + 1}')
        self.conn.commit()
        self._close_conn()
        # connect 内部会 ensure → 必须在这里被拒（不是等到读数据时缺列）。
        # connect 把 MigrationError 包装成 RuntimeError 并附可读原因（启动日志要
        # 能直接看懂），所以这里断言包装层 + 原始异常链。
        with self.assertRaises(RuntimeError) as caught:
            db.connect()
        self.assertIsInstance(caught.exception.__cause__, schemaver.ReaderTooOld)
        self.assertIn('schema', str(caught.exception))
        db._conn = None
