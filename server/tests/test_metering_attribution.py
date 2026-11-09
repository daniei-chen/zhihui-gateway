"""K27 · 计量归因与四表对账的测试。

覆盖：usage 三态（不把未知填 0）、路由归因（入口平台 vs 实际平台）、
花费五分类（成功/取消/未知分开）、以及 log↔event↔budget↔outcome 的对账。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import budget, config, db, ledger, metering  # noqa: E402


class UsageTriStateTest(unittest.TestCase):
    def test_absent_usage_is_none_not_zero(self) -> None:
        usage = metering.parse_usage(None)
        self.assertFalse(usage.observed)
        self.assertIsNone(usage.prompt_tokens)
        self.assertIsNone(usage.completion_tokens)
        self.assertFalse(usage.complete)
        self.assertIsNone(usage.known_tokens)

    def test_present_zero_is_known(self) -> None:
        usage = metering.parse_usage({'prompt_tokens': 0, 'completion_tokens': 0})
        self.assertTrue(usage.observed)
        self.assertEqual(usage.prompt_tokens, 0)
        self.assertTrue(usage.complete)
        self.assertEqual(usage.known_tokens, 0)

    def test_partial_usage_is_incomplete(self) -> None:
        usage = metering.parse_usage({'prompt_tokens': 12})
        self.assertIsNone(usage.completion_tokens)
        self.assertFalse(usage.complete)
        self.assertIsNone(usage.known_tokens)

    def test_invalid_numbers_are_marked_malformed(self) -> None:
        usage = metering.parse_usage({'prompt_tokens': 'x', 'completion_tokens': True,
                                      'credit': False})
        self.assertTrue(usage.observed)
        self.assertTrue(usage.malformed)
        self.assertIsNone(usage.prompt_tokens)
        self.assertIsNone(usage.completion_tokens)
        self.assertIsNone(usage.credit)

    def test_negative_credit_is_not_zero(self) -> None:
        usage = metering.parse_usage({'prompt_tokens': 1, 'completion_tokens': 2,
                                      'credit': -5})
        self.assertIsNone(usage.credit)
        self.assertTrue(usage.malformed)

    def test_storage_tokens_keep_legacy_integer_shape(self) -> None:
        usage = metering.parse_usage({})
        self.assertEqual(usage.storage_tokens(), (0, 0))


class RouteAttributionTest(unittest.TestCase):
    def test_forced_platform_wins(self) -> None:
        info = metering.route_attribution(entry_platform='qoder',
                                          requested_model='x', upstream=None)
        self.assertEqual(info['actual_platform'], 'qoder')
        self.assertEqual(info['platform_source'], 'forced')

    def test_model_prefix_routes_to_platform(self) -> None:
        info = metering.route_attribution(entry_platform='',
                                          requested_model='trae/deepseek-v3')
        self.assertEqual(info['actual_platform'], 'trae')
        self.assertEqual(info['platform_source'], 'model_prefix')

    def test_threeway_upstream_name_wins(self) -> None:
        info = metering.route_attribution(
            entry_platform='', requested_model='deepseek-v3',
            upstream={'id': None, 'name': 'qoder', 'base_url': 'http://127.0.0.1:8799'})
        self.assertEqual(info['actual_platform'], 'qoder')
        self.assertEqual(info['platform_source'], 'threeway')
        self.assertEqual(info['upstream_ref'], 'qoder@127.0.0.1')

    def test_upstream_ref_has_no_path_or_query(self) -> None:
        info = metering.route_attribution(
            entry_platform='', requested_model='m',
            upstream={'id': 7, 'base_url': 'https://api.example.com/secret/path?token=x'})
        self.assertEqual(info['upstream_ref'], '7@api.example.com')
        self.assertNotIn('secret', info['upstream_ref'])
        self.assertNotIn('token', info['upstream_ref'])

    def test_key_upstream_and_default(self) -> None:
        info = metering.route_attribution(entry_platform='', requested_model='m',
                                          upstream={'id': 3})
        self.assertEqual(info['actual_platform'], 'workbuddy')
        self.assertEqual(info['platform_source'], 'key_upstream')
        default = metering.route_attribution(entry_platform='', requested_model='m')
        self.assertEqual(default['upstream_ref'], None)


class SpendClassTest(unittest.TestCase):
    def _usage(self, complete: bool = True) -> metering.Usage:
        if complete:
            return metering.Usage(10, 5, 0.01, observed=True)
        return metering.Usage(None, None, None, observed=False)

    def test_completed_with_usage_is_settled(self) -> None:
        self.assertEqual(
            metering.spend_class(terminal_state='completed', usage=self._usage(),
                                 credit=0.01), metering.SPEND_SETTLED)

    def test_completed_without_usage_is_unknown_not_zero(self) -> None:
        self.assertEqual(
            metering.spend_class(terminal_state='completed', usage=self._usage(False),
                                 credit=None), metering.SPEND_UNKNOWN)

    def test_cancelled_is_its_own_bucket(self) -> None:
        self.assertEqual(
            metering.spend_class(terminal_state='client_cancelled', usage=self._usage(),
                                 credit=0.01), metering.SPEND_CANCELLED)

    def test_upstream_error_is_unknown(self) -> None:
        self.assertEqual(
            metering.spend_class(terminal_state='upstream_error', usage=self._usage(False),
                                 credit=None), metering.SPEND_UNKNOWN)

    def test_rejected_without_usage_has_no_spend(self) -> None:
        self.assertEqual(
            metering.spend_class(terminal_state='rejected', usage=self._usage(False),
                                 credit=None), metering.SPEND_REJECTED)

    def test_models_list_is_not_billable(self) -> None:
        self.assertEqual(
            metering.spend_class(terminal_state='completed', usage=self._usage(False),
                                 credit=None, endpoint='/v1/models'),
            metering.SPEND_NOT_BILLABLE)

    def test_unknown_budget_forces_unknown(self) -> None:
        self.assertEqual(
            metering.spend_class(terminal_state='completed', usage=self._usage(False),
                                 credit=None, budget_state='unknown'),
            metering.SPEND_UNKNOWN)


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k27.db'
        db._conn = None
        db.connect()
        budget.initialize()
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at) '
                   "VALUES(1,'k','h','p',0)")

    def tearDown(self) -> None:
        try:
            db._conn.close()  # type: ignore[union-attr]
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    def _log(self, rid: str, *, platform: str, actual: str | None,
             terminal: str = 'completed', pt: int = 10, ct: int = 5,
             usage_source: str = 'upstream', usage_complete: int = 1,
             endpoint: str = '/v1/chat/completions') -> None:
        db.add_request_log(
            ts=int(time.time()), key_id=1, model='deepseek-v3', status=200,
            prompt_tokens=pt, completion_tokens=ct, stream=1, realm='cn',
            request_id=rid, endpoint=endpoint, method='POST', protocol='openai',
            http_status=200, terminal_state=terminal, release_id='test',
            usage_source=usage_source, usage_complete=usage_complete,
            first_byte_ms=100, platform=platform, actual_platform=actual,
            upstream_ref='1@api.example.com')

    def test_platform_mismatch_is_reported(self) -> None:
        self._log('rid-mm', platform='workbuddy', actual='qoder')
        report = metering.reconcile(db._conn)
        self.assertEqual(report['findings']['platform_mismatch']['count'], 1)

    def test_missing_usage_event_is_reported(self) -> None:
        self._log('rid-no-event', platform='workbuddy', actual='workbuddy')
        report = metering.reconcile(db._conn)
        self.assertEqual(report['findings']['missing_usage_event']['count'], 1)
        ledger.record('rid-no-event', 1, 'deepseek-v3', 'cn', 10, 5, 0.01, True,
                      'completed', 'test')
        report = metering.reconcile(db._conn)
        self.assertEqual(report['findings']['missing_usage_event']['count'], 0)

    def test_token_mismatch_is_reported(self) -> None:
        self._log('rid-tok', platform='workbuddy', actual='workbuddy', pt=10, ct=5)
        ledger.record('rid-tok', 1, 'deepseek-v3', 'cn', 99, 5, 0.01, True,
                      'completed', 'test')
        report = metering.reconcile(db._conn)
        self.assertEqual(report['findings']['token_mismatch']['count'], 1)

    def test_cancelled_and_unknown_are_separate_from_settled(self) -> None:
        self._log('rid-ok', platform='workbuddy', actual='workbuddy')
        self._log('rid-cancel', platform='workbuddy', actual='workbuddy',
                  terminal='client_cancelled', pt=0, ct=0, usage_source='unknown',
                  usage_complete=0)
        self._log('rid-unknown', platform='workbuddy', actual='workbuddy',
                  pt=0, ct=0, usage_source='unknown', usage_complete=0)
        report = metering.reconcile(db._conn)
        classes = report['spend_classes']
        self.assertEqual(classes[metering.SPEND_SETTLED], 1)
        self.assertEqual(classes[metering.SPEND_CANCELLED], 1)
        self.assertEqual(classes[metering.SPEND_UNKNOWN], 1, '未知用量不能被算成 0/成功')

    def test_unknown_budget_is_flagged(self) -> None:
        self._log('rid-budget', platform='workbuddy', actual='workbuddy')
        db.execute(
            'INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
            'reserved_tokens,reserved_credit,charged_tokens,charged_credit,updated_at,'
            'owner,fence,lease_until) VALUES(?,1,?,\'unknown\',10,0.1,0,0,?,\'\',0,0)',
            ('rid-budget', int(time.time()), int(time.time())))
        report = metering.reconcile(db._conn)
        self.assertEqual(report['findings']['budget_unknown']['count'], 1)

    def test_legacy_rows_without_request_id_are_counted_not_fabricated(self) -> None:
        db.add_request_log(ts=int(time.time()), key_id=1, model='m', status=200,
                           prompt_tokens=1, completion_tokens=1, stream=0)
        report = metering.reconcile(db._conn)
        self.assertGreaterEqual(report['findings']['log_without_request_id']['count'], 1)

    def test_reconcile_never_modifies_data(self) -> None:
        self._log('rid-ro', platform='workbuddy', actual='qoder')
        before = db.query_one('SELECT COUNT(*) c FROM request_logs')['c']
        metering.reconcile(db._conn)
        after = db.query_one('SELECT COUNT(*) c FROM request_logs')['c']
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
