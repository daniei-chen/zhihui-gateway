"""U01 回归：账本完成回执的落盘、幂等重放与健康状态机。

背景（2026-10-28 审计 U01）：`_record` 把「请求日志 + 预算结算 + 账本事件 +
密钥累计」放在一个事务里，事务失败时**只**记日志并置 `ledger_write_healthy=False`。
结果是：那一笔请求的实际用量彻底消失，而「健康」还能被后续任意一条不写账本的
成功请求翻回 true。

这里锁定四件事：
  1. 失败的事实必须原子落盘，且回执只含记账元数据（不含提示词/凭据）；
  2. 重放按 request_id 幂等——重复重放不重复累计；
  3. 请求日志已存在时只补不可变事件，不再累加用量；
  4. 就绪/健康由「积压归零 + 连续成功重放」决定，不是单条写成功。
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import budget, config, db, keysvc, ledger, ledgerreplay, observability  # noqa: E402
from server import operational  # noqa: E402


class _ReplayFixture(unittest.TestCase):
    """共享的临时库/spool/密钥夹具；不含测试方法，避免子类重复执行。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        self._orig_spool = os.environ.get('WB_LEDGER_SPOOL_DIR')
        self._orig_backpressure = os.environ.get('WB_LEDGER_BACKPRESSURE')
        self._orig_healthy = operational.ledger_write_healthy
        self._orig_spool_failed = operational.ledger_spool_failed
        self._orig_streak = operational._ledger_replay_streak
        root = pathlib.Path(self._tmp.name)
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        os.environ['WB_LEDGER_SPOOL_DIR'] = str(root / 'spool')
        db._conn = None
        db.connect()
        budget.initialize()
        operational.ledger_write_healthy = True
        operational.ledger_spool_failed = False
        operational._ledger_replay_streak = 0
        created = keysvc.create_key(name='u01', realm='cn')
        self.key = keysvc.resolve(created['key'])

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        if self._orig_spool is None:
            os.environ.pop('WB_LEDGER_SPOOL_DIR', None)
        else:
            os.environ['WB_LEDGER_SPOOL_DIR'] = self._orig_spool
        if self._orig_backpressure is None:
            os.environ.pop('WB_LEDGER_BACKPRESSURE', None)
        else:
            os.environ['WB_LEDGER_BACKPRESSURE'] = self._orig_backpressure
        operational.ledger_write_healthy = self._orig_healthy
        operational.ledger_spool_failed = self._orig_spool_failed
        operational._ledger_replay_streak = self._orig_streak
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _facts(self, request_id: str = 'req-1', **kw) -> dict:
        base = {
            'ts': int(time.time()), 'key_id': self.key['id'], 'ip': '127.0.0.1',
            'model': 'fake-model', 'mapped_model': 'fake-model', 'status': 200,
            'prompt_tokens': 10, 'completion_tokens': 5, 'latency_ms': 100,
            'first_token_ms': 20, 'ua': 'pytest', 'error': None, 'stream': 0,
            'credit': 0.5, 'realm': 'cn', 'account': None,
            'cache_hit_tokens': 0, 'cache_miss_tokens': 0, 'cache_write_tokens': 0,
            'request_id': request_id, 'endpoint': '/v1/chat/completions',
            'method': 'POST', 'protocol': 'openai', 'http_status': 200,
            'terminal_state': 'completed', 'error_stage': None,
            'release_id': 'test-release', 'usage_source': 'upstream',
            'usage_complete': 1, 'first_byte_ms': 30, 'platform': 'workbuddy',
            'actual_platform': 'workbuddy', 'upstream_ref': 'default',
            'client_request_id': None, 'effort': None, 'channel': 'api',
            'session_key': None,
        }
        base.update(kw)
        return base

    def _spill(self, facts: dict, **entry_kw) -> str:
        entry = ledgerreplay.build_entry(facts, release_id='test-release', **entry_kw)
        path = ledgerreplay.spill(entry)
        self.assertIsNotNone(path, 'spill 应写入成功')
        return path

    def _units(self) -> int:
        row = db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?', (self.key['id'],))
        return int(row['used_credit_units'] or 0)

    def _reserve(self, request_id: str) -> None:
        now = int(time.time())
        db.execute(
            'INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
            'reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (request_id, self.key['id'], now, 'forwarded', 1000, 0.0, now, 'test', 1, now + 60))

class LedgerReplayTest(_ReplayFixture):
    """回执落盘、幂等重放与健康状态机。"""

    # ── 1. 回执内容与落盘 ──────────────────────────────────────────────
    def test_receipt_keeps_only_accounting_metadata(self) -> None:
        facts = self._facts(messages=[{'role': 'user', 'content': 'secret prompt'}],
                            authorization='Bearer sk-should-not-persist')
        entry = ledgerreplay.build_entry(facts, config_hash='a' * 64)
        self.assertNotIn('messages', entry['facts'])
        self.assertNotIn('authorization', entry['facts'])
        path = ledgerreplay.spill(entry)
        self.assertIsNotNone(path)
        stored = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
        self.assertEqual(stored['schema'], ledgerreplay.SCHEMA_VERSION)
        self.assertNotIn('secret prompt', pathlib.Path(path).read_text(encoding='utf-8'))
        self.assertEqual(ledgerreplay.backlog(), 1)

    def test_spill_is_idempotent_per_request(self) -> None:
        self._spill(self._facts('req-dup'))
        self._spill(self._facts('req-dup'))
        self.assertEqual(ledgerreplay.backlog(), 1)

    # ── 2. 重放：应用、清空、幂等 ─────────────────────────────────────
    def test_replay_applies_once_and_clears_backlog(self) -> None:
        self._reserve('req-1')
        self._spill(self._facts('req-1'))
        before = self._units()
        result = ledgerreplay.replay_batch()
        self.assertEqual(result['applied'], 1)
        self.assertEqual(result['backlog'], 0)
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM request_logs WHERE request_id=?', ('req-1',)))
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM usage_events WHERE request_id=?', ('req-1',)))
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM ledger_receipts WHERE receipt_id=?', ('req-1',)))
        self.assertEqual(self._units() - before, 500000)
        row = db.query_one('SELECT state,charged_tokens FROM budget_reservations WHERE request_id=?',
                           ('req-1',))
        self.assertEqual(row['state'], 'settled')
        self.assertEqual(int(row['charged_tokens']), 15)

    def test_replay_twice_does_not_double_count(self) -> None:
        self._spill(self._facts('req-2'))
        ledgerreplay.replay_batch()
        after_first = self._units()
        # 同一请求再次失败：同一条回执可能被重新写回 spool
        self._spill(self._facts('req-2'))
        result = ledgerreplay.replay_batch()
        self.assertEqual(result['duplicate'], 1)
        self.assertEqual(self._units(), after_first)

    def test_existing_log_only_backfills_events(self) -> None:
        """事务已提交但回执残留时，只补不可变事件，不重复累计用量。"""
        facts = self._facts('req-3')
        db.add_request_log(**facts)
        before = self._units()
        self._spill(facts)
        ledgerreplay.replay_batch()
        self.assertEqual(self._units(), before)
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM usage_events WHERE request_id=?', ('req-3',)))
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM ledger_receipts WHERE receipt_id=?', ('req-3',)))

    # ── 3. spool 失败与背压 ───────────────────────────────────────────
    def test_spill_failure_returns_none_when_directory_unusable(self) -> None:
        blocker = pathlib.Path(self._tmp.name) / 'blocked'
        blocker.write_text('not a directory', encoding='utf-8')
        os.environ['WB_LEDGER_SPOOL_DIR'] = str(blocker)
        self.assertIsNone(ledgerreplay.spill(ledgerreplay.build_entry(self._facts('req-4'))))

    def test_backpressure_triggers_on_backlog(self) -> None:
        os.environ['WB_LEDGER_BACKPRESSURE'] = '2'
        self.assertFalse(ledgerreplay.should_backpressure())
        self._spill(self._facts('req-5'))
        self._spill(self._facts('req-6'))
        self.assertTrue(ledgerreplay.should_backpressure())
        token = observability.current.set(SimpleNamespace(
            key_id=None, request_id='req-7', budget_reserved=False))
        try:
            with self.assertRaises(budget.BudgetRejected) as ctx:
                budget.prepare(self.key, {'model': 'fake-model'})
            self.assertEqual(ctx.exception.code, 'ledger_backpressure')
        finally:
            observability.current.reset(token)

    # ── 4. 健康状态机 ─────────────────────────────────────────────────
    def test_readiness_fails_while_backlog_pending(self) -> None:
        self._spill(self._facts('req-8'))
        status = operational.readiness()
        self.assertFalse(status['ok'])
        self.assertFalse(status['ledger_write'])
        self.assertEqual(status['ledger_backlog'], 1)

    def test_health_recovers_only_after_streak(self) -> None:
        self._spill(self._facts('req-9'))
        operational.ledger_write_healthy = False
        # 第一轮重放：清空积压，但连续成功轮数还不够
        operational.replay_ledger_receipts(int(time.time()))
        self.assertFalse(operational.ledger_write_healthy)
        operational.replay_ledger_receipts(int(time.time()))
        self.assertFalse(operational.ledger_write_healthy)
        operational.replay_ledger_receipts(int(time.time()))
        self.assertTrue(operational.ledger_write_healthy)
        self.assertTrue(operational.readiness()['ok'])

    def test_unreadable_receipt_moves_to_dead_without_blocking_backlog(self) -> None:
        """坏回执立即进 dead-letter，不占 backlog（N-03 契约变更）。

        原契约是「任何失败都累加 attempts，5 次后进 dead」—— 但坏回执永远不会
        成功，却让 backlog>0 持续 5 个重放轮（约 2.5 分钟），而 readiness() 把
        「积压归零」当成就绪条件，等于一个 poison 文件能把就绪判定压住。
        """
        directory = pathlib.Path(os.environ['WB_LEDGER_SPOOL_DIR'])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'bad.json').write_text('{"schema": 99}', encoding='utf-8')
        result = ledgerreplay.replay_batch()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['dead'], 1)
        self.assertEqual(result['backlog'], 0)
        self.assertTrue((directory / 'dead' / 'bad.json').exists())
        self.assertEqual(operational._ledger_replay_streak, 0)

    def test_unparseable_receipt_also_moves_to_dead(self) -> None:
        """JSON 都解析不了（写半截/外部损坏）同样立即进 dead。"""
        directory = pathlib.Path(os.environ['WB_LEDGER_SPOOL_DIR'])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'broken.json').write_text('{"schema": 1, "facts"', encoding='utf-8')
        result = ledgerreplay.replay_batch()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['dead'], 1)
        self.assertEqual(result['backlog'], 0)
        self.assertTrue((directory / 'dead' / 'broken.json').exists())


CHILD_SCRIPT = '''
import os, pathlib, signal, sys
sys.path.insert(0, {root!r})
from server import config, db
config.DB_PATH = pathlib.Path({dbpath!r})
config.USERS_FILE = pathlib.Path({users!r})
db._conn = None
db.connect()
facts = {facts!r}
with db.transaction():
    db.add_request_log(**facts)
    db.execute('UPDATE api_keys SET used_tokens = used_tokens + 15 WHERE id = ?', ({keyid},))
{tail}
'''


class LedgerRecordIntegrationTest(_ReplayFixture):
    """`_record` 的接线：事务失败必须落回执并置降级，成功路径不留回执。"""

    def _state(self, request_id: str):
        return SimpleNamespace(
            request_id=request_id, http_status=200, terminal_state=None,
            endpoint='/v1/chat/completions', method='POST', protocol='openai',
            error_stage=None, account=None, first_byte_ms=12,
            client_request_id=None, config_hash='b' * 64,
            budget_reserved=True, budget_fence=1,
            actual_platform='workbuddy', upstream_ref='default', recorded=False)

    def test_record_success_leaves_no_receipt(self) -> None:
        from server.routers import gateway
        token = observability.current.set(self._state('req-ok'))
        try:
            gateway._record(self.key, '127.0.0.1', 'fake-model', 'fake-model', 200,
                            10, 5, 100, 'pytest', None, False, credit=0.5)
        finally:
            observability.current.reset(token)
        self.assertEqual(ledgerreplay.backlog(), 0)
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM request_logs WHERE request_id=?', ('req-ok',)))

    def test_record_failure_spills_and_degrades(self) -> None:
        from server.routers import gateway
        original = db.transaction

        def boom():
            raise sqlite3.OperationalError('database is locked')

        db.transaction = boom
        token = observability.current.set(self._state('req-locked'))
        try:
            gateway._record(self.key, '127.0.0.1', 'fake-model', 'fake-model', 200,
                            10, 5, 100, 'pytest', None, False, credit=0.5)
        finally:
            observability.current.reset(token)
            db.transaction = original
        self.assertEqual(ledgerreplay.backlog(), 1)
        self.assertFalse(operational.ledger_write_healthy)
        stored = json.loads((pathlib.Path(os.environ['WB_LEDGER_SPOOL_DIR'])
                             / 'req-locked.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['facts']['request_id'], 'req-locked')
        self.assertFalse(operational.readiness()['ok'])
        # 锁释放后重放要能真正落库，且健康要经连续成功轮次才恢复
        operational.ledger_write_healthy = True
        result = ledgerreplay.replay_batch()
        self.assertEqual(result['applied'], 1)
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM request_logs WHERE request_id=?', ('req-locked',)))

@unittest.skipUnless(hasattr(signal, 'SIGKILL'), 'SIGKILL 只在 POSIX 可用')
class LedgerCrashRecoveryTest(_ReplayFixture):
    """SIGKILL 故障注入：提交前后崩溃都不能重复累计。"""

    def _run_child(self, mode: str, facts: dict) -> int:
        import subprocess
        root = pathlib.Path(__file__).resolve().parents[2]
        script = pathlib.Path(self._tmp.name) / f'child-{mode}.py'
        # before_commit：kill 落在事务内（未提交）；after_commit：事务提交后再 kill
        tail = ('    os.kill(os.getpid(), signal.SIGKILL)'
                if mode == 'before_commit'
                else 'os.kill(os.getpid(), signal.SIGKILL)')
        script.write_text(CHILD_SCRIPT.format(
            root=str(root), dbpath=str(config.DB_PATH), users=str(config.USERS_FILE),
            facts=facts, keyid=self.key['id'], tail=tail), encoding='utf-8')
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=120)
        self.last_child_stderr = proc.stderr.decode('utf-8', 'replace')[-2000:]
        return proc.returncode

    def _reconnect(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.connect()

    def test_sigkill_before_commit_rolls_back(self) -> None:
        before = self._units()
        code = self._run_child('before_commit', self._facts('req-kill-1'))
        self.assertEqual(code, -9, '子进程应被 SIGKILL 终止；stderr=' + getattr(self, 'last_child_stderr', ''))
        self._reconnect()
        self.assertIsNone(db.query_one(
            'SELECT 1 FROM request_logs WHERE request_id=?', ('req-kill-1',)))
        row = db.query_one('SELECT used_tokens FROM api_keys WHERE id=?', (self.key['id'],))
        self.assertEqual(int(row['used_tokens'] or 0), 0)
        self.assertEqual(self._units(), before)

    def test_sigkill_after_commit_then_replay_keeps_single_count(self) -> None:
        code = self._run_child('after_commit', self._facts('req-kill-2'))
        self.assertEqual(code, -9, '子进程应被 SIGKILL 终止；stderr=' + getattr(self, 'last_child_stderr', ''))
        self._reconnect()
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM request_logs WHERE request_id=?', ('req-kill-2',)))
        row = db.query_one('SELECT used_tokens FROM api_keys WHERE id=?', (self.key['id'],))
        self.assertEqual(int(row['used_tokens'] or 0), 15)
        # 崩溃后残留同一请求的回执：重放只补不可变事件，不重复累计
        self._spill(self._facts('req-kill-2'))
        result = ledgerreplay.replay_batch()
        self.assertEqual(result['applied'], 1)
        row = db.query_one('SELECT used_tokens FROM api_keys WHERE id=?', (self.key['id'],))
        self.assertEqual(int(row['used_tokens'] or 0), 15)
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM usage_events WHERE request_id=?', ('req-kill-2',)))


if __name__ == '__main__':
    unittest.main()
