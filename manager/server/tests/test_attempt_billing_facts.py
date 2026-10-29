"""U04 回归：attempt 级「有无计费事实」（三态 + 来源），不合成金额。

背景（2026-10-28 审计 U04）：`attempt_events` 只有阶段事件（生产实测 111,724 行：
selected / upstream_headers / stream_terminal / aggregate_terminal），**没有任何
计费字段**；`usage_events` 是请求级；`ledger.record` 的 attempt 列恒为 1。于是
「这次请求用了几个 attempt、每个有没有计费证据」在库里答不出来。

审计口径是「取得池侧回执和关联 ID 后再计 per-attempt；缺失证据单列，不合成虚假的
token/费用」。这里锁定：
  1. 三态 `reported` / `unknown` / `not_applicable` 与来源 `provider_receipt` /
     `absent` 的判定只依赖既有不可变事实（attempt 阶段 + 请求级 usage 证据）；
  2. 多个 attempt 到达上游时**不做归属**（无法证明是哪一次被计费 → 全部 unknown）；
  3. `usage_events` 仍只记请求级一行（attempt 变多不重复计费），且能回答
     「几个 attempt、各自有无证据」；
  4. 事实表不可变、写入幂等，per-attempt **金额**明确不做（`not_derived`）。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, ledger  # noqa: E402


class AttemptBillingFactsTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        root = pathlib.Path(self._tmp.name)
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        db._conn = None
        db.connect()
        self.key = keysvc.resolve(keysvc.create_key(name='u04', realm='cn')['key'])
        self.now = int(time.time())

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _log(self, request_id: str, **overrides) -> None:
        fields = {
            'ts': self.now, 'key_id': self.key['id'], 'ip': '127.0.0.1',
            'model': 'm', 'mapped_model': 'm', 'status': 200, 'prompt_tokens': 10,
            'completion_tokens': 5, 'latency_ms': 100, 'ua': 'pytest', 'stream': 0,
            'credit': 0.5, 'realm': 'cn', 'request_id': request_id,
            'terminal_state': 'completed', 'usage_source': 'upstream',
            'usage_complete': 1, 'platform': 'workbuddy',
        }
        fields.update(overrides)
        db.add_request_log(**fields)

    def _attempt(self, request_id: str, number: int, phase: str, status: int = 200) -> None:
        """走真实入口 `ledger.attempt()`，锁定阶段事件的解析与落库。"""
        self.assertTrue(ledger.attempt({
            'event': 'workbuddy_attempt', 'request_id': request_id, 'attempt': number,
            'phase': phase, 'account_uid': 'acct-1', 'resolved_model': 'm',
            'realm': 'cn', 'status': status, 'config_hash': 'a' * 64}))

    def _facts(self, request_id: str) -> list:
        return db.query('SELECT * FROM attempt_billing_facts WHERE request_id=? '
                        'ORDER BY attempt', (request_id,))

    # ── 1. 三态判定 ────────────────────────────────────────────────────
    def test_single_attempt_with_usage_is_reported_by_provider_receipt(self) -> None:
        self._log('r1')
        for phase in ('selected', 'upstream_headers', 'stream_terminal'):
            self._attempt('r1', 1, phase)
        report = ledger.attempt_billing_report('r1')
        self.assertEqual(report['attempt_count'], 1)
        self.assertEqual(report['attempt_count_source'], 'pool_attempt_events')
        self.assertEqual(report['billing_fact_counts'][ledger.BILLING_REPORTED], 1)
        entry = report['attempts'][0]
        self.assertEqual(entry['attempt'], 1)
        self.assertEqual(entry['billing_fact'], 'reported')
        self.assertEqual(entry['billing_source'], 'provider_receipt')
        self.assertTrue(entry['reached_upstream'])
        self.assertIn('usage_events', entry['evidence'])

    def test_attempt_that_never_reached_upstream_is_not_applicable(self) -> None:
        self._log('r2', status=502, terminal_state='upstream_error',
                  usage_source='unknown', usage_complete=0, credit=None,
                  prompt_tokens=0, completion_tokens=0)
        self._attempt('r2', 1, 'selected', status=0)
        report = ledger.attempt_billing_report('r2')
        self.assertEqual(report['attempts'][0]['billing_fact'], 'not_applicable')
        self.assertEqual(report['attempts'][0]['billing_source'], 'absent')
        self.assertFalse(report['attempts'][0]['reached_upstream'])
        self.assertEqual(report['billing_fact_counts']['not_applicable'], 1)

    def test_attempt_reached_upstream_without_terminal_is_unknown(self) -> None:
        self._log('r3', status=502, terminal_state='upstream_error',
                  usage_source='unknown', usage_complete=0, credit=None,
                  prompt_tokens=0, completion_tokens=0)
        self._attempt('r3', 1, 'selected')
        self._attempt('r3', 1, 'upstream_headers')
        entry = ledger.attempt_billing_report('r3')['attempts'][0]
        self.assertTrue(entry['reached_upstream'])
        self.assertEqual(entry['billing_fact'], 'unknown')
        self.assertEqual(entry['billing_source'], 'absent')
        self.assertIn('无终态事件', entry['evidence'])

    def test_terminal_attempt_without_usage_receipt_is_unknown(self) -> None:
        self._log('r4', status=502, terminal_state='upstream_error',
                  usage_source='unknown', usage_complete=0, credit=None,
                  prompt_tokens=0, completion_tokens=0)
        self._attempt('r4', 1, 'stream_terminal', status=502)
        entry = ledger.attempt_billing_report('r4')['attempts'][0]
        self.assertEqual(entry['billing_fact'], 'unknown')
        self.assertEqual(entry['billing_source'], 'absent')
        self.assertIn('usage_source', entry['evidence'])

    def test_multiple_attempts_are_never_attributed_to_one_receipt(self) -> None:
        """2 个 attempt 都到达上游 + 请求级有 usage → 无法归属，全部 unknown。"""
        self._log('r5')
        for phase in ('selected', 'upstream_headers', 'stream_terminal'):
            self._attempt('r5', 1, phase)
        self._attempt('r5', 2, 'selected')
        self._attempt('r5', 2, 'upstream_headers')
        report = ledger.attempt_billing_report('r5')
        self.assertEqual(report['attempt_count'], 2)
        self.assertEqual(report['billing_fact_counts']['reported'], 0,
                         '不能把一份请求级 usage 摊给某个 attempt')
        self.assertEqual(report['billing_fact_counts']['unknown'], 2)
        self.assertIn('无法归属', report['attempts'][1]['evidence'])

    # ── 2. usage_events 仍只记请求级，不重复计费 ──────────────────────
    def test_usage_events_stay_request_level_and_never_double_count(self) -> None:
        self._log('r6')
        self._attempt('r6', 1, 'stream_terminal')
        self._attempt('r6', 2, 'stream_terminal')
        with db.transaction():
            ledger.record('r6', self.key['id'], 'm', 'cn', 10, 5, 0.5, True,
                          'completed', 'test')
            ledger.record('r6', self.key['id'], 'm', 'cn', 10, 5, 0.5, True,
                          'completed', 'test')
        rows = db.query('SELECT attempt,event_id FROM usage_events WHERE request_id=?', ('r6',))
        self.assertEqual(len(rows), 1, 'attempt 变多不允许重复记用量')
        self.assertEqual(int(rows[0]['attempt']), ledger.REQUEST_LEVEL_ATTEMPT)
        self.assertEqual(rows[0]['event_id'], 'r6:1:usage',
                         '事件 ID 是幂等键，格式不能漂移')
        report = ledger.attempt_billing_report('r6')
        self.assertEqual(report['request_level']['usage_events'], 1)
        self.assertEqual(report['attempt_count'], 2)
        self.assertIn('不会重复计费', report['request_level']['note'])

    # ── 3. 事实落库：不可变 + 幂等 ────────────────────────────────────
    def test_recorded_facts_are_immutable_and_idempotent(self) -> None:
        self._log('r7')
        self._attempt('r7', 1, 'stream_terminal')
        self.assertEqual(ledger.record_attempt_billing('r7', self.now), 1)
        self.assertEqual(ledger.record_attempt_billing('r7', self.now), 0)
        self.assertEqual(len(self._facts('r7')), 1)
        stored = self._facts('r7')[0]
        self.assertEqual(stored['billing_fact'], 'reported')
        self.assertEqual(stored['billing_source'], 'provider_receipt')
        for sql in ('UPDATE attempt_billing_facts SET billing_fact=\'unknown\'',
                    'DELETE FROM attempt_billing_facts'):
            with self.assertRaises(sqlite3.IntegrityError):
                with db.transaction():
                    db.execute(sql)
        self.assertEqual(ledger.attempt_billing_report('r7')['recorded_facts'], 1)
        self.assertEqual(ledger.attempt_billing_report('r7')['recorded_drift'], {})

    def test_report_flags_drift_between_recorded_and_derived(self) -> None:
        self._log('r8', status=502, terminal_state='upstream_error',
                  usage_source='unknown', usage_complete=0, credit=None,
                  prompt_tokens=0, completion_tokens=0)
        self._attempt('r8', 1, 'selected')
        self.assertEqual(ledger.record_attempt_billing('r8', self.now), 1)
        # 阶段事件后到：同一个 attempt 又出现了终态 + 请求级 usage
        self._attempt('r8', 1, 'stream_terminal')
        db.execute("UPDATE request_logs SET usage_source='upstream',usage_complete=1,"
                   "credit=0.1,status=200,terminal_state='completed' WHERE request_id=?",
                   ('r8',))
        report = ledger.attempt_billing_report('r8')
        self.assertEqual(report['recorded_drift']['1'],
                         {'recorded': 'not_applicable', 'derived': 'reported'})
        self.assertEqual(report['attempts'][0]['billing_fact'], 'reported',
                         '报告以当前事实为准，同时明示已落库值发生了漂移')

    # ── 4. 后台补齐：稳定窗口 ─────────────────────────────────────────
    def test_backfill_only_covers_requests_past_the_settle_window(self) -> None:
        self._log('old', ts=self.now - 3600)
        self._attempt('old', 1, 'stream_terminal')
        self._log('fresh', ts=self.now)
        self._attempt('fresh', 1, 'stream_terminal')
        self.assertEqual(ledger.backfill_attempt_billing(now=self.now), 1)
        self.assertEqual(len(self._facts('old')), 1)
        self.assertEqual(self._facts('fresh'), [],
                         '刚结束的请求先等池侧 attempt 事件到齐，避免写死错事实')
        self.assertEqual(ledger.backfill_attempt_billing(now=self.now), 0,
                         '已落过事实的请求不重复处理')

    # ── 5. 不编造金额 + 池侧依赖清单 ──────────────────────────────────
    def test_report_never_fabricates_per_attempt_amounts(self) -> None:
        self._log('r9')
        self._attempt('r9', 1, 'stream_terminal')
        report = ledger.attempt_billing_report('r9')
        self.assertEqual(report['per_attempt_billing'], 'not_derived')
        self.assertIn('绝不合成', report['per_attempt_billing_note'])
        allowed = {'attempt', 'billing_fact', 'billing_source', 'evidence',
                   'reached_upstream'}
        for entry in report['attempts']:
            self.assertEqual(set(entry), allowed,
                             'attempt 明细只允许三态与来源，不允许出现金额/token')
        self.assertNotIn('credit', report['attempts'][0])
        self.assertNotIn('credit_units', report['attempts'][0])

    def test_report_lists_what_the_pool_must_provide(self) -> None:
        self._log('r10')
        report = ledger.attempt_billing_report('r10')
        requirements = report['pool_side_requirements']
        self.assertGreaterEqual(len(requirements), 5)
        joined = ' '.join(requirements)
        for keyword in ('回执', 'attempt', 'usage', '幂等'):
            self.assertIn(keyword, joined)

    def test_request_without_pool_events_reports_absent_not_zero(self) -> None:
        """池侧没有 attempt 事件时必须说「不知道」，不能声称只有 1 个 attempt。"""
        self._log('r11')
        report = ledger.attempt_billing_report('r11')
        self.assertEqual(report['attempt_count'], 0)
        self.assertEqual(report['attempt_count_source'], 'absent')
        self.assertEqual(report['attempts'], [])
        self.assertEqual(report['request_level']['usage_source'], 'upstream')
        self.assertEqual(report['request_level']['usage_events'], 0)


if __name__ == '__main__':
    unittest.main()
