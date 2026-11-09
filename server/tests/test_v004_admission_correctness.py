"""Synthetic-only regressions for v0.0.4 policy and budget admission."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from server import budget, budgetowner, config, db, keysvc, observability
from server.routers.keys import KeyIn, KeyPatch


class AdmissionCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zh-v004-admission-')
        self.addCleanup(self.tmp.cleanup)
        self.old_conn = db._conn
        db._conn = None
        self.addCleanup(self.close_db)
        root = Path(self.tmp.name)
        for name, value in {'DATA_DIR': root, 'DB_PATH': root / 'test.db',
                            'USERS_FILE': root / 'users.json'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(observability.current.reset, observability.current.set(None))
        patch = mock.patch('server.ledgerreplay.admission_block', return_value={'blocked': False})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {'WB_KEY_BUDGET_PENDING_QUOTA': '1'})
        patch.start()
        self.addCleanup(patch.stop)
        budget.initialize()

    def close_db(self):
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = self.old_conn

    def prepare(self, key, rid, body=None):
        state = observability.RequestAudit(rid, '/v1/chat/completions', 'POST',
                                           'openai', time.monotonic())
        observability.current.set(state)
        body = {'messages': [], 'max_tokens': 20000} if body is None else body
        budget.prepare(key, body)
        return state, body

    def unknown(self, key, rid, reserved, *, charged=0, age=901):
        now = int(time.time())
        db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
                   'reserved_tokens,charged_tokens,updated_at) VALUES(?,?,?,?,?,?,?)',
                   (rid, key['id'], now - age, 'unknown', reserved, charged, now - age))

    def test_corrupt_policy_is_visible_but_never_authorizes(self):
        for field in ('ip_allowlist', 'models'):
            for raw in ('[', 'null', '{}', '"model"', '[1]', '[false]', '[null]', ''):
                with self.subTest(field=field, raw=raw):
                    made = keysvc.create_key('synthetic')
                    db.execute(f'UPDATE api_keys SET {field}=? WHERE id=?', (raw, made['id']))
                    row = keysvc.resolve(made['key'])
                    listed = next(x for x in keysvc.list_keys() if x['id'] == made['id'])
                    self.assertFalse(listed.get('request_policy_valid', True))
                    rejection = keysvc.validate(row, '192.0.2.8', 'other')
                    self.assertIsNotNone(rejection)
                    self.assertEqual((rejection.status, rejection.code), (403, 'invalid_key_policy'))
                    db.execute(f'UPDATE api_keys SET {field}=? WHERE id=?', ('[]', made['id']))
                    repaired = keysvc.resolve(made['key'])
                    self.assertTrue(repaired['request_policy_valid'])
                    self.assertIsNone(keysvc.validate(repaired, '192.0.2.8', 'other'))

    def test_malformed_cidr_is_invalid_not_partially_permissive(self):
        made = keysvc.create_key('synthetic')
        db.execute('UPDATE api_keys SET ip_allowlist=? WHERE id=?',
                   (json.dumps(['192.0.2.8', 'not-a-network']), made['id']))
        row = keysvc.resolve(made['key'])
        self.assertEqual(keysvc.validate(row, '192.0.2.8', 'model').code, 'invalid_key_policy')

    def test_valid_empty_and_restrictive_policy_remain_distinct(self):
        open_key = keysvc.create_key('open')
        restricted = keysvc.create_key('restricted', models=['allowed'], ip_allowlist=['192.0.2.0/24'])
        self.assertIsNone(keysvc.validate(open_key, '203.0.113.7', 'any'))
        self.assertIsNone(keysvc.validate(restricted, '192.0.2.8', 'allowed'))
        self.assertEqual(keysvc.validate(restricted, '203.0.113.7', 'allowed').code, 'ip_not_allowed')
        self.assertEqual(keysvc.validate(restricted, '192.0.2.8', 'other').code, 'model_not_allowed')

    def test_old_unknown_cannot_spend_same_total_tokens_again(self):
        key = keysvc.create_key('total', quota=8000)
        self.unknown(key, 'old', 8000)
        with self.assertRaises(budget.BudgetRejected) as error:
            self.prepare(key, 'new')
        self.assertEqual(error.exception.code, 'insufficient_quota')
        self.assertIsNone(db.query_one('SELECT request_id FROM budget_reservations WHERE request_id=?', ('new',)))

    def test_age_does_not_change_total_hold(self):
        for age in (899, 900, 901, 86401):
            with self.subTest(age=age):
                key = keysvc.create_key('aged', quota=8000)
                self.unknown(key, 'old-' + str(age), 8000, age=age)
                with self.assertRaises(budget.BudgetRejected):
                    self.prepare(key, 'next-' + str(age))

    def test_partial_unknown_does_not_double_count_already_charged_tokens(self):
        key = keysvc.create_key('partial', quota=10000)
        keysvc.touch(key, '192.0.2.8', 1000, 0)
        self.unknown(key, 'partial-old', 6000, charged=1000)
        self.prepare(key, 'partial-new')
        new = db.query_one('SELECT reserved_tokens FROM budget_reservations WHERE request_id=?', ('partial-new',))
        self.assertEqual(new['reserved_tokens'], 4000)

    def test_released_and_settled_estimates_no_longer_hold_tokens(self):
        key = keysvc.create_key('terminal', quota=10000)
        for rid, status in (('released', 'released'), ('settled', 'settled')):
            self.unknown(key, rid, 10000)
            db.execute('UPDATE budget_reservations SET state=? WHERE request_id=?', (status, rid))
        self.prepare(key, 'terminal-new')
        self.assertEqual(db.query_one('SELECT reserved_tokens FROM budget_reservations WHERE request_id=?',
                                     ('terminal-new',))['reserved_tokens'], 10000)

    def test_legacy_unknown_without_estimate_fails_closed_for_total_quota(self):
        key = keysvc.create_key('legacy', quota=10000)
        self.unknown(key, 'legacy-old', 0)
        with self.assertRaises(budget.BudgetRejected) as error:
            self.prepare(key, 'legacy-new')
        self.assertEqual(error.exception.code, 'total_token_unconfirmed')

    def test_old_unknown_credit_remains_unconfirmed_for_lifetime_credit_cap(self):
        key = keysvc.create_key('credits', quota_credit=10)
        self.unknown(key, 'credit-old', 5000)
        with self.assertRaises(budget.BudgetRejected) as error:
            self.prepare(key, 'credit-new')
        self.assertEqual(error.exception.code, 'credit_quota_unconfirmed')

    def test_total_hold_is_atomic_under_concurrent_admission(self):
        key = keysvc.create_key('parallel', quota=10000)
        self.unknown(key, 'parallel-old', 5000)
        barrier = threading.Barrier(2)
        def run(index):
            token = observability.current.set(None)
            try:
                barrier.wait(timeout=5)
                try:
                    self.prepare(key, 'race-' + str(index))
                    return 'reserved'
                except budget.BudgetRejected:
                    return 'rejected'
            finally:
                observability.current.reset(token)
        with mock.patch.dict(os.environ, {'WB_KEY_BUDGET_PENDING_QUOTA': '2'}), ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(run, range(2))), ['reserved', 'rejected'])
        outstanding = db.query_one("SELECT sum(reserved_tokens-charged_tokens) n FROM budget_reservations "
                                   "WHERE key_id=? AND state IN ('unknown','reserved','forwarded')", (key['id'],))
        self.assertEqual(outstanding['n'], 10000)

    def test_invalid_numeric_limits_are_rejected_before_any_database_change(self):
        bad = {'quota': (-1, 1.5, True, float('inf'), 'invalid'),
               'max_ips': (-1, 1.5, True, float('inf'), 'invalid'),
               'quota_credit': (-1, True, float('inf'), float('nan'), 'invalid', 0.0000001),
               'daily_token_quota': (-1, 1.5, True),
               'daily_credit_quota': (-1, True, float('nan'), 0.0000001)}
        key = keysvc.create_key('unchanged', quota=5000, quota_credit=3)
        original = dict(db.query_one('SELECT * FROM api_keys WHERE id=?', (key['id'],)))
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        KeyIn(name='invalid', **{field: value})
                    with self.assertRaises(ValidationError):
                        KeyPatch(**{field: value})
                    with self.assertRaises(ValueError):
                        keysvc.create_key('invalid', **{field: value})
                    with self.assertRaises(ValueError):
                        keysvc.update_key(key['id'], {field: value})
                    self.assertEqual(dict(db.query_one('SELECT * FROM api_keys WHERE id=?', (key['id'],))), original)
        self.assertEqual(len(keysvc.list_keys()), 1)

    def test_null_is_not_an_implicit_unlimited_numeric_patch(self):
        key = keysvc.create_key('nonzero', quota=1234, quota_credit=3)
        for field in ('quota', 'quota_credit', 'max_ips', 'daily_token_quota', 'daily_credit_quota'):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    KeyPatch(**{field: None})
                with self.assertRaises(ValueError):
                    keysvc.update_key(key['id'], {field: None})
        self.assertEqual(KeyPatch().model_dump(exclude_unset=True), {})
        self.assertEqual(keysvc.update_key(key['id'], {'quota': 0})['quota'], 0)

    def test_explicit_zero_and_integer_valued_legacy_amount_remain_valid(self):
        key = keysvc.create_key('packet-compatible', quota=2000.0, quota_credit='0.25')
        self.assertEqual((key['quota'], key['quota_credit']), (2000, 0.25))
        self.assertEqual(KeyIn(name='unlimited', quota=0, quota_credit=0).quota, 0)


if __name__ == '__main__':
    unittest.main()
