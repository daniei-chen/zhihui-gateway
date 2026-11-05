"""IP limits must hold before dispatch, including simultaneous first use."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from server import config, db, keysvc


class AtomicIPBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path_patch = mock.patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'ip.db')
        self.conn_patch = mock.patch.object(db, '_conn', None)
        self.path_patch.start()
        self.conn_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.conn_patch.stop)
        db.connect()
        self.addCleanup(lambda: db._conn.close() if db._conn is not None else None)
        self.key = keysvc.create_key('atomic-ip', max_ips=1)

    def test_simultaneous_new_ips_cannot_exceed_limit(self):
        barrier = threading.Barrier(12)

        def claim(index):
            ip = f'192.0.2.{index + 1}'
            # All callers observe the same empty binding set before dispatch.
            self.assertIsNone(keysvc.validate(self.key, ip, 'model'))
            barrier.wait(timeout=5)
            return ip, keysvc.claim_ip(self.key, ip)

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(claim, range(12)))
        accepted = [ip for ip, reason in results if reason is None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual({reason.code for _, reason in results if reason}, {'too_many_ips'})
        self.assertEqual([r['ip'] for r in db.query('SELECT ip FROM api_key_ips')], accepted)
        self.assertIsNone(keysvc.claim_ip(self.key, accepted[0]))
        self.assertEqual(db.query_one('SELECT used_tokens FROM api_keys')['used_tokens'], 0)

    def test_current_limit_wins_over_stale_key_snapshot(self):
        stale = dict(self.key, max_ips=0)
        self.assertIsNone(keysvc.claim_ip(stale, '192.0.2.1'))
        self.assertEqual(keysvc.claim_ip(stale, '192.0.2.2').code, 'too_many_ips')

    def test_preflight_and_unlimited_keys_do_not_create_bindings(self):
        self.assertIsNone(keysvc.validate(self.key, '192.0.2.1', 'model'))
        unlimited = keysvc.create_key('unlimited', max_ips=0)
        self.assertIsNone(keysvc.claim_ip(unlimited, '192.0.2.2'))
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM api_key_ips')['n'], 0)

    def test_revoked_and_expired_keys_cannot_bind(self):
        for patch, expected in (({'enabled': False}, 'key_disabled'),
                                ({'enabled': True, 'expires_at': int(time.time()) - 5}, 'key_expired')):
            with self.subTest(expected=expected):
                keysvc.update_key(self.key['id'], patch)
                self.assertEqual(keysvc.claim_ip(self.key, '192.0.2.1').code, expected)
        keysvc.delete_key(self.key['id'])
        self.assertEqual(keysvc.claim_ip(self.key, '192.0.2.1').code, 'invalid_api_key')
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM api_key_ips')['n'], 0)

    def test_existing_ip_remains_valid_when_limit_is_lowered(self):
        keysvc.update_key(self.key['id'], {'max_ips': 2})
        self.assertIsNone(keysvc.claim_ip(self.key, '192.0.2.1'))
        self.assertIsNone(keysvc.claim_ip(self.key, '192.0.2.2'))
        keysvc.update_key(self.key['id'], {'max_ips': 1})
        self.assertIsNone(keysvc.claim_ip(self.key, '192.0.2.2'))
        self.assertEqual(keysvc.claim_ip(self.key, '192.0.2.3').code, 'too_many_ips')
