"""U27 回归：热路径治理（DDL 移出热路径、等待预算可观测、取消不提前释放）。

背景（台账 U27）：`budget.prepare()` 在**每个请求**的预留路径上都调
`initialize()`，而它每次要跑 3 条 DDL（各自独立提交）+ 1 个 `BEGIN IMMEDIATE`
事务 + `PRAGMA table_info`。生产库副本实测（69,647 行 / 207MB WAL）：
`prepare()` p50 0.296ms，把 DDL 换成一次性后 p50 0.175ms（−41%），
32 线程吞吐 1636 → 2360 req/s（+44%）。

顺带量出一个**比台账描述更大的**问题：`heartbeat()` 的
`UPDATE ... WHERE owner=? AND state IN (...)` 原先没有可用索引，是 69,647 行的
全表扫描（实测 p50 **8.08ms**，每 10 秒一次，且全程持有进程级写锁）。加
`(owner,state)` 索引后 p50 **0.0148ms**（546×）。

这里锁定：
  1. `initialize()` 每进程每库只真正执行一次 DDL；换库/fork 必须重建；
  2. `prepare()` 走热路径时**不再触发任何 DDL**（且 initialize 返回 False）；
  3. 32 并发下无锁超时（`busy_errors == 0`）、计数可读；
  4. 取消（转发后）**不提前释放**预留 —— 只转 unknown；
  5. `heartbeat()` 走索引（`EXPLAIN QUERY PLAN` 是 SEARCH 不是 SCAN）；
  6. `db.transaction()` 的语义未被仪表化改写（嵌套不提交、异常回滚、深度复位）。
"""
from __future__ import annotations

import contextlib
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import budget, budgetowner, config, db, instancelease, keysvc, observability  # noqa: E402


class _HotPathBase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        root = pathlib.Path(self._tmp.name)
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        db._conn = None
        db.connect()
        budget._SCHEMA_STATE['key'] = None          # 让每个用例从「未初始化」开始
        instancelease._SCHEMA_STATE['key'] = None
        for name in list(db._TXN_STATS):
            if isinstance(db._TXN_STATS[name], int):
                db._TXN_STATS[name] = 0
            elif isinstance(db._TXN_STATS[name], float):
                db._TXN_STATS[name] = 0.0
        budget.initialize(force=True)
        self.key = keysvc.resolve(keysvc.create_key(name='u27', realm='cn')['key'])

    def tearDown(self) -> None:
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        budget._SCHEMA_STATE['key'] = None
        instancelease._SCHEMA_STATE['key'] = None
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _state(self, request_id: str):
        state = observability.RequestAudit(request_id, '/v1/chat/completions', 'POST',
                                          'openai', time.monotonic())
        state.key_id = int(self.key['id'])
        return state

    @contextlib.contextmanager
    def _request(self, request_id: str):
        """把请求态放进 contextvar，供 prepare/forwarded/finish 共用。"""
        state = self._state(request_id)
        token = observability.current.set(state)
        try:
            yield state
        finally:
            observability.current.reset(token)

    def _prepare(self, request_id: str):
        state = self._state(request_id)
        token = observability.current.set(state)
        try:
            budget.prepare(self.key, {'model': 'm', 'messages': [], 'max_tokens': 64})
        finally:
            observability.current.reset(token)
        return state

    def _row_state(self, request_id: str) -> str:
        return db.query_one('SELECT state FROM budget_reservations WHERE request_id=?',
                            (request_id,))['state']


class InitializeOnceTest(_HotPathBase):

    def test_initialize_runs_ddl_only_once(self) -> None:
        budget._SCHEMA_STATE['key'] = None
        self.assertTrue(budget.initialize(), '首次必须真的执行 DDL')
        self.assertFalse(budget.initialize(), '第二次必须是空操作')
        self.assertFalse(budget.initialize())
        self.assertTrue(budget.initialize(force=True), 'force=True 必须强制重跑')

    def test_reinitialize_after_database_switch(self) -> None:
        """换库（测试/多实例）必须重建 —— 标志的键里必须含 DB 路径。"""
        self.assertFalse(budget.initialize())
        other = pathlib.Path(self._tmp.name) / 'other.db'
        original = config.DB_PATH
        try:
            config.DB_PATH = other
            db._conn.close()
            db._conn = None
            db.connect()
            self.assertTrue(budget.initialize(), '换库后必须重新建表')
        finally:
            config.DB_PATH = original
            db._conn.close()
            db._conn = None
            db.connect()
            budget._SCHEMA_STATE['key'] = None
            budget.initialize(force=True)

    def test_prepare_does_not_issue_ddl(self) -> None:
        """热路径不许出现 CREATE/ALTER/PRAGMA table_info。"""
        statements: list[str] = []
        original_execute = db.execute

        def spy(sql, args=()):  # noqa: ANN001
            head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ''
            statements.append(head)
            return original_execute(sql, args)

        init_results: list[bool] = []
        original_initialize = budget.initialize

        def init_spy(force: bool = False) -> bool:
            result = original_initialize(force=force)
            init_results.append(result)
            return result

        db.execute = spy
        budget.initialize = init_spy
        try:
            self._prepare('hot-1')
        finally:
            db.execute = original_execute
            budget.initialize = original_initialize
        self.assertNotIn('CREATE', statements, '热路径不许建表/建索引')
        self.assertNotIn('ALTER', statements, '热路径不许补列')
        self.assertEqual(init_results, [False], 'prepare 里的 initialize 必须走空操作分支')

    def test_instancelease_initialize_runs_once_but_conn_path_always_runs(self) -> None:
        instancelease._SCHEMA_STATE['key'] = None
        self.assertTrue(instancelease.initialize())
        self.assertFalse(instancelease.initialize())
        # 带 conn 的路径是迁移入口，必须无条件执行 DDL
        with db.transaction() as conn:
            self.assertTrue(instancelease.initialize(conn))
            self.assertTrue(instancelease.initialize(conn))


class WaitBudgetTest(_HotPathBase):

    def test_busy_timeout_is_explicit_and_reported(self) -> None:
        row = db.query_one('PRAGMA busy_timeout')
        self.assertEqual(int(row[0]), db._BUSY_TIMEOUT_MS)
        self.assertEqual(int(row[0]), 5000, '等待预算必须显式是 5000ms')
        stats = db.transaction_stats()
        self.assertEqual(stats['busy_timeout_ms'], 5000)
        self.assertIn('busy_errors', stats)
        self.assertIn('begin_max_ms', stats)
        self.assertIn('lock_wait_max_ms', stats)

    def test_transaction_stats_count_work(self) -> None:
        before = db.transaction_stats()['transactions']
        self._prepare('budget-stats')
        after = db.transaction_stats()
        self.assertGreater(after['transactions'], before)
        self.assertGreaterEqual(after['lock_wait_max_ms'], 0.0)
        self.assertGreaterEqual(after['begin_max_ms'], 0.0)

    def test_32_concurrent_prepares_have_no_lock_timeout(self) -> None:
        threads, per_thread = 32, 4
        errors: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(threads)

        def worker(index: int) -> None:
            barrier.wait()
            for seq in range(per_thread):
                try:
                    self._prepare('conc-%d-%d' % (index, seq))
                except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                    with lock:
                        errors.append('%s: %s' % (type(exc).__name__, exc))

        with mock.patch.dict('os.environ', {'WB_KEY_BUDGET_PENDING': '1000000',
                                            'WB_KEY_BUDGET_PENDING_QUOTA': '1000000'}):
            pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
            for thread in pool:
                thread.start()
            for thread in pool:
                thread.join()

        self.assertEqual(errors, [], '并发下不允许出现锁超时或其它异常')
        stats = db.transaction_stats()
        self.assertEqual(stats['busy_errors'], 0, 'busy_timeout 一次都不该被击穿')
        rows = db.query_one('SELECT COUNT(*) AS c FROM budget_reservations')['c']
        self.assertEqual(rows, threads * per_thread, '每个请求都应落一条预留')


class CancelSemanticsTest(_HotPathBase):

    def test_cancel_after_forward_keeps_reservation_as_unknown(self) -> None:
        """转发后被取消：上游可能已计费 → 只能转 unknown，**不能**释放。"""
        with self._request('cancel-forwarded') as state:
            budget.prepare(self.key, {'model': 'm', 'messages': [], 'max_tokens': 64})
            budget.forwarded()
            self.assertEqual(self._row_state('cancel-forwarded'), 'forwarded')
            budget.finish(state)          # 取消/异常收尾走这里
        self.assertEqual(self._row_state('cancel-forwarded'), 'unknown',
                         '转发后的取消必须保留为 unknown（可能已计费）')

    def test_cancel_before_forward_releases_only_unsent(self) -> None:
        with self._request('cancel-reserved') as state:
            budget.prepare(self.key, {'model': 'm', 'messages': [], 'max_tokens': 64})
            self.assertEqual(self._row_state('cancel-reserved'), 'reserved')
            budget.finish(state)
        self.assertEqual(self._row_state('cancel-reserved'), 'released',
                         '未转发的预留可以释放（上游没收到）')

    def test_finish_does_not_delete_rows(self) -> None:
        state = self._prepare('cancel-keep')
        budget.finish(state)
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM budget_reservations WHERE request_id=?', ('cancel-keep',)))


class HeartbeatIndexTest(_HotPathBase):

    def test_heartbeat_uses_owner_state_index(self) -> None:
        rows = db.query('EXPLAIN QUERY PLAN UPDATE budget_reservations SET lease_until=1 '
                        "WHERE owner='x' AND state IN ('reserved','forwarded')")
        plan = ' '.join(row['detail'] for row in rows)
        self.assertIn('idx_budget_owner_state', plan,
                      'heartbeat 的 UPDATE 必须走 (owner,state) 索引，否则是全表扫描')
        self.assertNotIn('SCAN budget_reservations', plan)

    def test_heartbeat_is_cheap_on_a_populated_table(self) -> None:
        """灌 5000 行后，心跳仍然不能是全表扫描量级的开销。"""
        now = int(time.time())
        with db.transaction() as conn:
            for index in range(5000):
                conn.execute(
                    'INSERT OR IGNORE INTO budget_reservations(request_id,key_id,created_at,'
                    'state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) '
                    "VALUES(?,?,?,'settled',0,0,?,'other-owner',1,0)",
                    ('bulk-%d' % index, self.key['id'], now, now))
        durations = []
        for _ in range(10):
            started = time.perf_counter()
            budget.heartbeat()
            durations.append((time.perf_counter() - started) * 1000)
        self.assertLess(max(durations), 50.0,
                        '5000 行上心跳仍应是亚毫秒到毫秒级；%.1fms 说明退回了扫描'
                        % max(durations))


class TransactionSemanticsTest(_HotPathBase):

    def test_nested_transaction_does_not_commit_early(self) -> None:
        with self.assertRaises(RuntimeError):
            with db.transaction():
                db.set_setting('u27-nested', 'outer')
                with db.transaction():
                    db.set_setting('u27-nested-inner', 'inner')
                raise RuntimeError('boom')
        self.assertIsNone(db.get_setting('u27-nested'))
        self.assertIsNone(db.get_setting('u27-nested-inner'))

    def test_depth_is_restored_after_failure(self) -> None:
        with self.assertRaises(RuntimeError):
            with db.transaction():
                raise RuntimeError('boom')
        self.assertEqual(getattr(db._atomic_state, 'depth', 0), 0)
        # 失败之后仍能正常开启新事务（深度没被污染）
        with db.transaction() as conn:
            conn.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
