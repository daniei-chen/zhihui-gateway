"""Parallel login KDF workers must retain ordered in-memory/durable counters."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

from server import db, security
from server.tests.authorization_test_support import isolated_data


class LoginCounterConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        db.connect()
        self.enterContext(patch.object(security, '_fail', {}))
        self.enterContext(patch.object(security, '_user_fail', {}))
        self.enterContext(patch.object(security, '_FAILS_LOADED', True))

    def test_parallel_failures_keep_both_counters_and_sqlite_mirror_equal(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda _: security.record_fail('192.0.2.1', 'Fixture'), range(64)))
        self.assertEqual(security._fail['192.0.2.1'][0], 64)
        self.assertEqual(security._user_fail['fixture'][0], 64)
        self.assertEqual(db.query_one('SELECT fails FROM login_fails_ip WHERE ip=?', ('192.0.2.1',))[0], 64)
        self.assertEqual(db.query_one('SELECT fails FROM login_fails_user WHERE username=?', ('fixture',))[0], 64)
        self.assertTrue(security.login_blocked('192.0.2.1', 'Fixture'))

    def test_clear_cannot_overtake_a_half_persisted_failure(self):
        entered, release, clearing = threading.Event(), threading.Event(), threading.Event()
        original = security._mirror_fail

        def mirror(kind, *args):
            if kind == 'ip':
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('synthetic counter fixture deadline')
            return original(kind, *args)

        def clear():
            clearing.set()
            security.clear_fail('192.0.2.1', 'fixture')

        with ThreadPoolExecutor(max_workers=2) as workers, patch.object(security, '_mirror_fail', side_effect=mirror):
            failure = workers.submit(security.record_fail, '192.0.2.1', 'fixture')
            try:
                self.assertTrue(entered.wait(2))
                cleared = workers.submit(clear)
                self.assertTrue(clearing.wait(2))
                self.assertFalse(cleared.done())
            finally:
                release.set()
            failure.result(timeout=3)
            cleared.result(timeout=3)
        self.assertNotIn('192.0.2.1', security._fail)
        self.assertNotIn('fixture', security._user_fail)
        self.assertIsNone(db.query_one('SELECT 1 FROM login_fails_ip WHERE ip=?', ('192.0.2.1',)))
        self.assertIsNone(db.query_one('SELECT 1 FROM login_fails_user WHERE username=?', ('fixture',)))

    def test_first_load_is_not_visible_as_complete_before_rows_are_loaded(self):
        entered, release, checking = threading.Event(), threading.Event(), threading.Event()
        now = time.time()
        original = db.query
        calls = []

        def query(sql, args):
            if 'SELECT ip, fails, first' in sql:
                calls.append('ip')
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('synthetic counter fixture deadline')
                return [{'ip': '192.0.2.1', 'fails': 5, 'first': now}]
            return original(sql, args)

        def check():
            checking.set()
            return security.login_blocked('192.0.2.1', 'fixture')

        with patch.object(security, '_FAILS_LOADED', False), patch.object(db, 'query', side_effect=query), \
                ThreadPoolExecutor(max_workers=2) as workers:
            loading = workers.submit(security._load_fails_once)
            try:
                self.assertTrue(entered.wait(2))
                checked = workers.submit(check)
                self.assertTrue(checking.wait(2))
                self.assertFalse(checked.done())
            finally:
                release.set()
            loading.result(timeout=3)
            self.assertTrue(checked.result(timeout=3))
        self.assertEqual(calls, ['ip'])


if __name__ == '__main__':
    unittest.main()
