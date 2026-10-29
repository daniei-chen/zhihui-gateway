"""U02 回归：unknown 的分类、下界展示与「有证据才结清」。

拒绝错误只用于诊断，不能证明请求级零计费。自动结清必须有完整上游回执
和一致的不可变用量记录；未知、部分计费和日志缺失继续保留。

这里锁定：
  1. 六类互斥分类的判定；
  2. 汇总里的下界语义（token 是预留估算、credit 恒为 0，不编造）；
  3. 自动结清只碰完整零用量证据，且不改动密钥累计用量、留下审计痕迹；
  4. 幂等与年龄门槛。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import budget, config, db, keysvc, ledger  # noqa: E402


class UnknownClosureTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        root = pathlib.Path(self._tmp.name)
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        db._conn = None
        db.connect()
        budget.initialize()
        self.key = keysvc.resolve(keysvc.create_key(name='u02', realm='cn')['key'])

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _reserve(self, request_id: str, *, state: str = 'unknown', reserved: int = 1000,
                 charged_tokens: int = 0, charged_credit: float = 0.0,
                 age: int = 7200) -> None:
        now = int(time.time())
        db.execute(
            'INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
            'reserved_tokens,reserved_credit,charged_tokens,charged_credit,updated_at,'
            'owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (request_id, self.key['id'], now - age, state, reserved, 0.0,
             charged_tokens, charged_credit, now - age, 'test', 1, 0))

    def _log(self, request_id: str, **overrides) -> None:
        fields = {
            'ts': int(time.time()), 'key_id': self.key['id'], 'ip': '127.0.0.1',
            'model': 'm', 'mapped_model': 'm', 'status': 502, 'prompt_tokens': 0,
            'completion_tokens': 0, 'latency_ms': 10, 'ua': 'pytest', 'stream': 0,
            'credit': None, 'realm': 'cn', 'request_id': request_id,
            'terminal_state': 'upstream_error', 'usage_source': 'unknown',
            'error': '{"error":{"code":"no_healthy_account"}}', 'platform': 'workbuddy',
        }
        fields.update(overrides)
        db.add_request_log(**fields)

    def _state(self, request_id: str) -> str:
        return db.query_one('SELECT state FROM budget_reservations WHERE request_id=?',
                            (request_id,))['state']

    def _zero_receipt(self, request_id):
        db.execute("UPDATE request_logs SET usage_source='upstream',usage_complete=1,credit=0 WHERE request_id=?", (request_id,))
        ledger.record(request_id, self.key['id'], 'm', 'cn', 0, 0, 0, False, 'completed', 'fixture')

    def _usage(self) -> tuple[int, int]:
        row = db.query_one('SELECT used_tokens, used_credit_units FROM api_keys WHERE id=?',
                           (self.key['id'],))
        return int(row['used_tokens'] or 0), int(row['used_credit_units'] or 0)

    # ── 1. 分类 ────────────────────────────────────────────────────────
    def test_classify_six_classes(self) -> None:
        self._reserve('r-charged', charged_tokens=5)
        self._reserve('r-usage')
        self._log('r-usage', usage_source='upstream', credit=0.1)
        self._reserve('r-cancel')
        self._log('r-cancel', status=499, terminal_state='client_cancelled')
        self._reserve('r-reject')
        self._log('r-reject')
        self._reserve('r-nolog')
        self._reserve('r-unexplained')
        self._log('r-unexplained', error='upstream timeout')
        summary = budget.unresolved_summary()['summary']
        self.assertEqual(summary['by_class']['charged_partial'], 1)
        self.assertEqual(summary['by_class']['usage_present'], 1)
        self.assertEqual(summary['by_class']['client_cancelled'], 1)
        self.assertEqual(summary['by_class']['upstream_rejected'], 1)
        self.assertEqual(summary['by_class']['no_request_log'], 1)
        self.assertEqual(summary['by_class']['unexplained'], 1)
        self.assertEqual(summary['unknown_total'], 6)
        self.assertEqual(summary['auto_reconcilable'], 0)
        self.assertEqual(summary['needs_human'], 6)

    # ── 2. 下界语义 ────────────────────────────────────────────────────
    def test_summary_bounds_are_estimates_not_bills(self) -> None:
        self._reserve('a', reserved=1000)
        self._log('a')
        self._reserve('b', reserved=2000)
        self._log('b')
        summary = budget.unresolved_summary()['summary']
        self.assertEqual(summary['token_lower_bound'], 3000)
        self.assertEqual(summary['credit_lower_bound'], 0.0)
        self.assertIn('不是实际用量', summary['bounds_note'])

    # ── 3. 只结清有证据的 ─────────────────────────────────────────────
    def test_evidenced_reconcile_only_upstream_rejected(self) -> None:
        self._reserve('ok')
        self._log('ok')
        self._zero_receipt('ok')
        self._reserve('cancel')
        self._log('cancel', status=499, terminal_state='client_cancelled')
        self._reserve('partial', charged_tokens=7)
        self._log('partial')
        settled = budget.reconcile_evidenced_unknowns(int(time.time()))
        self.assertEqual(settled, 1)
        self.assertEqual(self._state('ok'), 'settled')
        self.assertEqual(self._state('cancel'), 'unknown')
        self.assertEqual(self._state('partial'), 'unknown')

    def test_evidenced_reconcile_does_not_change_key_usage(self) -> None:
        before = self._usage()
        self._reserve('ok', reserved=5000)
        self._log('ok')
        self._zero_receipt('ok')
        budget.reconcile_evidenced_unknowns(int(time.time()))
        self.assertEqual(self._usage(), before)

    def test_evidenced_reconcile_writes_audit(self) -> None:
        self._reserve('ok')
        self._log('ok')
        self._zero_receipt('ok')
        budget.reconcile_evidenced_unknowns(int(time.time()))
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM budget_adjustments WHERE request_id=?', ('ok',)))
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM adjustment_events WHERE request_id=?', ('ok',)))
        row = db.query_one('SELECT resolution FROM budget_reservations WHERE request_id=?',
                           ('ok',))
        self.assertIn('完整上游回执', row['resolution'] or '')

    # ── 4. 幂等与年龄门槛 ─────────────────────────────────────────────
    def test_evidenced_reconcile_is_idempotent(self) -> None:
        self._reserve('ok')
        self._log('ok')
        self._zero_receipt('ok')
        self.assertEqual(budget.reconcile_evidenced_unknowns(int(time.time())), 1)
        self.assertEqual(budget.reconcile_evidenced_unknowns(int(time.time())), 0)

    def test_min_age_gate_keeps_fresh_unknown(self) -> None:
        self._reserve('fresh', age=60)
        self._log('fresh')
        self._zero_receipt('fresh')
        self.assertEqual(budget.reconcile_evidenced_unknowns(int(time.time())), 0)
        self.assertEqual(self._state('fresh'), 'unknown')

    def test_needs_human_old_counts_only_stale_non_evidenced(self) -> None:
        self._reserve('old-cancel', age=7200)
        self._log('old-cancel', status=499, terminal_state='client_cancelled')
        self._reserve('old-reject', age=7200)
        self._log('old-reject')
        self._reserve('fresh-cancel', age=60)
        self._log('fresh-cancel', status=499, terminal_state='client_cancelled')
        summary = budget.unresolved_summary()['summary']
        self.assertEqual(summary['needs_human'], 3)
        self.assertEqual(summary['needs_human_old'], 2)
        self.assertEqual(summary['auto_reconcilable'], 0)


if __name__ == '__main__':
    unittest.main()
