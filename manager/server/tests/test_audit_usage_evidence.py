"""Malformed provider counters cannot become a complete zero-cost receipt."""
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest import mock

from server import budget, budgetowner, config, db, keysvc, observability
from server import portalbilling, portalstore, portalwallet
from server.routers import gateway


class UsageEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_connection = db._conn
        db._conn = None
        self.addCleanup(setattr, db, '_conn', self.previous_connection)
        for owner, name, value in (
            (config, 'DB_PATH', Path(self.tmp.name) / 'ledger.db'),
            (config, 'DATA_DIR', Path(self.tmp.name)),
            (budgetowner, 'identity', lambda: 'audit-owner'),
        ):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        conn = db.connect()
        self.addCleanup(conn.close)
        budget.initialize(force=True)
        self.key = keysvc.create_key('usage-evidence')
        self.sequence = 0

    def record(self, usage, pt=0, ct=0, *, portal=False):
        self.sequence += 1
        request_id = f'audit-usage-{self.sequence}'
        state = observability.RequestAudit(request_id, '/v1/chat/completions', 'POST',
                                           'openai', time.monotonic(), terminal_state='completed',
                                           key_id=self.key['id'], budget_reserved=True)
        state.budget_fence = 1
        if portal:
            state.portal_owner = 'audit-user'
            db.execute(portalwallet.RECEIPTS)
            with portalstore.transaction() as conn:
                conn.execute('INSERT OR IGNORE INTO portal_users '
                             '(id,username,pwd_hash,created_at) VALUES(?,?,?,?)',
                             ('audit-user', 'audit-user', 'synthetic-no-login', 1))
                conn.execute('INSERT INTO portal_bonus_grants VALUES(?,?,?,?,?,?,?,?,?)',
                             (request_id, 'audit-user', 128, 0, 1, int(time.time()) + 1000,
                              'admin', 'synthetic held bonus', 1))
                conn.execute('INSERT INTO portal_spends VALUES(?,?,?,?,?,?,?,?,?)',
                             (request_id, 'audit-user', self.key['id'], 'audit-owner',
                              'forwarded', 1, 128, None, None))
                conn.execute('INSERT INTO portal_spend_policies VALUES(?,?)',
                             (request_id, portalbilling.POLICY))
                conn.execute('INSERT INTO portal_bonus_allocations VALUES(?,?,?)',
                             (request_id, request_id, 128))
        db.execute('INSERT INTO budget_reservations '
                   '(request_id,key_id,created_at,state,reserved_tokens,updated_at,owner,fence) '
                   "VALUES(?,?,1,'forwarded',128,1,'audit-owner',1)",
                   (request_id, self.key['id']))
        token = observability.current.set(state)
        try:
            gateway._record(self.key, '127.0.0.1', 'model', 'model', 200, pt, ct,
                            1, None, None, False, usage=usage)
        finally:
            observability.current.reset(token)
        self.assertTrue(state.recorded, 'Synthetic receipt must commit, not disappear into spool')
        reservation = dict(db.query_one('SELECT * FROM budget_reservations WHERE request_id=?', (request_id,)))
        log = dict(db.query_one('SELECT * FROM request_logs WHERE request_id=?', (request_id,)))
        return reservation, log

    def test_invalid_token_values_remain_unknown_and_do_not_prove_zero(self):
        for value in (None, True, -1, 1.5, float('inf'), float('nan'), 2**53, 'invalid'):
            with self.subTest(value=value):
                reservation, log = self.record({'prompt_tokens': value, 'completion_tokens': 0, 'credit': 0})
                self.assertEqual(reservation['state'], 'unknown')
                self.assertEqual(reservation['reserved_tokens'], 128)
                self.assertEqual(log['usage_complete'], 0)

    def test_invalid_credit_is_unknown_even_for_an_unlimited_key(self):
        for value in (True, -1, float('inf'), float('nan'), 'invalid', 10**30):
            with self.subTest(value=value):
                reservation, log = self.record({'prompt_tokens': 0, 'completion_tokens': 0, 'credit': value})
                self.assertEqual(reservation['state'], 'unknown')
                self.assertEqual(log['usage_complete'], 0)
                self.assertIsNone(log['credit'])

    def test_partial_valid_counter_is_preserved_without_releasing_hold(self):
        reservation, log = self.record({'prompt_tokens': 7, 'completion_tokens': None}, 7, 0)
        self.assertEqual(reservation['state'], 'unknown')
        self.assertEqual(reservation['charged_tokens'], 7)
        self.assertEqual(log['prompt_tokens'], 7)
        self.assertEqual(log['usage_complete'], 0)

    def test_real_zero_and_compatible_integer_strings_can_settle(self):
        for usage, tokens in (({'prompt_tokens': 0, 'completion_tokens': 0, 'credit': 0}, (0, 0)),
                              ({'prompt_tokens': '2', 'completion_tokens': '3'}, (2, 3))):
            with self.subTest(usage=usage):
                reservation, log = self.record(usage, *tokens)
                self.assertEqual(reservation['state'], 'settled')
                self.assertEqual(reservation['charged_tokens'], sum(tokens))
                self.assertEqual(log['usage_complete'], 1)

    def test_credit_limited_key_requires_credit_evidence(self):
        db.execute('UPDATE api_keys SET quota_credit=10 WHERE id=?', (self.key['id'],))
        reservation, log = self.record({'prompt_tokens': 0, 'completion_tokens': 0})
        self.assertEqual(reservation['state'], 'unknown')
        self.assertEqual(log['usage_complete'], 1)
        self.assertIsNone(log['credit'])

    def test_portal_malformed_usage_retains_hold_and_matches_replay(self):
        for value in (2**53, True, -1, 1.5, None):
            with self.subTest(value=value):
                reservation, log = self.record(
                    {'prompt_tokens': value, 'completion_tokens': 0, 'credit': 0}, portal=True)
                self.assertEqual(reservation['state'], 'unknown')
                rid = log['request_id']
                receipt = json.loads(db.query_one(
                    'SELECT payload FROM portal_spend_receipts WHERE request_id=?', (rid,))[0])
                self.assertIsNone(receipt['amount_units'])
                portalwallet.apply(rid)
                with portalstore.transaction() as conn:
                    spend = conn.execute('SELECT state,held_units FROM portal_spends '
                                         'WHERE request_id=?', (rid,)).fetchone()
                    self.assertEqual(tuple(spend), ('unknown', 128))
                    self.assertEqual(conn.execute('SELECT remaining_units FROM portal_bonus_grants '
                                                  'WHERE id=?', (rid,)).fetchone()[0], 0)
                replay_id = rid + '-replay'
                portalwallet.replay_receipt({'budget': {'portal_owner': 'audit-user'}},
                                             {**log, 'request_id': replay_id})
                replayed = json.loads(db.query_one(
                    'SELECT payload FROM portal_spend_receipts WHERE request_id=?', (replay_id,))[0])
                self.assertEqual(replayed, receipt)

    def test_portal_valid_zero_and_integer_strings_match_main_ledger(self):
        for value in (0, '0'):
            with self.subTest(value=value):
                reservation, log = self.record(
                    {'prompt_tokens': value, 'completion_tokens': value, 'credit': 0}, portal=True)
                self.assertEqual(reservation['state'], 'settled')
                portalwallet.apply(log['request_id'])
                with portalstore.transaction() as conn:
                    spend = conn.execute('SELECT state,charged_units FROM portal_spends '
                                         'WHERE request_id=?', (log['request_id'],)).fetchone()
                    self.assertEqual(tuple(spend), ('settled', 0))
                    self.assertEqual(conn.execute('SELECT remaining_units FROM portal_bonus_grants '
                                                  'WHERE id=?', (log['request_id'],)).fetchone()[0], 128)
