"""Daily budgets use durable counters, synthetic requests and no paid inference."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import budget, budgetowner, config, dailyquota, db, keysvc, observability, security
from server.routers import keys


class DailyKeyQuotaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_conn = db._conn
        db._conn = None
        self.addCleanup(self.cleanup_db)
        for name, value in {'DATA_DIR': Path(self.tmp.name), 'DB_PATH': Path(self.tmp.name) / 'daily.db',
                            'USERS_FILE': Path(self.tmp.name) / 'users.json'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch('server.ledgerreplay.admission_block', return_value={'blocked': False})
        patch.start(); self.addCleanup(patch.stop)
        budget.initialize()
        token = observability.current.set(None)
        self.addCleanup(observability.current.reset, token)
        self.user = {'username': 'admin', 'role': 'admin'}
        app = FastAPI()
        app.include_router(keys.router)
        app.dependency_overrides[security.current_user] = lambda: self.user
        self.client = TestClient(app)
        security.save_users({'secret': 'synthetic-quota-session', 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': 'synthetic'},
            {'username': 'alice', 'role': 'account_admin', 'pwd_hash': 'synthetic'}]})

    def cleanup_db(self):
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = self.old_conn

    def prepare(self, key, rid='synthetic-request'):
        state = observability.RequestAudit(rid, '/v1/chat/completions', 'POST', 'openai', time.monotonic())
        observability.current.set(state)
        body = {'messages': [], 'max_tokens': 10000}
        budget.prepare(key, body)
        return state, body

    def test_daily_token_cap_reduces_output_and_blocks_exhaustion(self):
        key = keysvc.create_key('fixture', daily_token_quota=8000)
        state, body = self.prepare(key)
        self.assertLess(body['max_tokens'], 8000)
        self.assertEqual(db.query_one('SELECT reserved_tokens FROM budget_reservations')['reserved_tokens'], 8000)
        with self.assertRaises(budget.BudgetRejected):
            self.prepare(key, 'second-request')
        observability.current.set(state)
        budget.forwarded()
        state.terminal_state = 'completed'
        with db.transaction():
            budget.settle({'prompt_tokens': 1000, 'completion_tokens': 7000}, 1000, 7000, 0)
            keysvc.touch(key, '127.0.0.1', 8000, 0)
        with self.assertRaises(budget.BudgetRejected) as error:
            self.prepare(key, 'third-request')
        self.assertEqual(error.exception.code, 'daily_token_quota_exhausted')

    def test_daily_credit_uses_integer_units_and_is_independent_of_total_reset(self):
        key = keysvc.create_key('fixture', daily_credit_quota=0.3, quota_credit=100)
        keysvc.touch(key, '127.0.0.1', 100, 0.1)
        keysvc.touch(key, '127.0.0.1', 100, 0.2)
        self.assertEqual(dailyquota.snapshot(key['id'], db.day_of())['credit_units'], 300000)
        keysvc.reset_usage(key['id'])
        self.assertEqual(keysvc.resolve(key['key'])['daily_used_credit'], 0.3)
        with self.assertRaises(budget.BudgetRejected) as error:
            self.prepare(key)
        self.assertEqual(error.exception.code, 'daily_credit_quota_exhausted')

    def test_next_day_resets_window_without_erasing_yesterday(self):
        key = keysvc.create_key('fixture', daily_token_quota=9000)
        with mock.patch.object(db, 'day_of', return_value='2026-10-28'):
            keysvc.touch(key, '127.0.0.1', 9000, 0.5)
        with mock.patch.object(db, 'day_of', return_value='2026-10-29'):
            self.prepare(key)
            self.assertEqual(dailyquota.snapshot(key['id'], '2026-10-29')['tokens'], 0)
        self.assertEqual(dailyquota.snapshot(key['id'], '2026-10-28')['tokens'], 9000)

    def test_same_day_unknown_credit_blocks_new_charge_but_not_unrelated_days(self):
        key = keysvc.create_key('fixture', daily_credit_quota=10)
        state, _ = self.prepare(key)
        budget.forwarded()
        state.terminal_state = 'completed'
        budget.settle({'prompt_tokens': 1, 'completion_tokens': 1}, 1, 1, None)
        db.execute('UPDATE budget_reservations SET updated_at=?', (int(time.time()) - 901,))
        with self.assertRaises(budget.BudgetRejected) as error:
            self.prepare(key, 'second')
        self.assertEqual(error.exception.code, 'daily_credit_unconfirmed')

    def test_report_aggregate_rebuild_does_not_reset_enforcement(self):
        key = keysvc.create_key('fixture')
        day = db.day_of()
        db.bump_usage(key['id'], 'fixture-model', 100, 20, 0.5)
        keysvc.touch(key, '127.0.0.1', 30, 0.2)
        self.assertEqual(dailyquota.snapshot(key['id'], day), {'tokens': 150, 'credit_units': 700000})
        db.execute('DELETE FROM usage_daily')
        self.assertEqual(dailyquota.snapshot(key['id'], day), {'tokens': 150, 'credit_units': 700000})

    def test_migration_seeds_existing_usage_once_even_before_next_call(self):
        key = keysvc.create_key('fixture')
        day = db.day_of()
        db.bump_usage(key['id'], 'fixture-model', 100, 20, 0.5)
        db.execute("DELETE FROM settings WHERE key='quota.daily-seed.v1'")
        with db.transaction() as conn: dailyquota.seed_existing(conn)
        db.execute('DELETE FROM usage_daily')
        with db.transaction() as conn: dailyquota.seed_existing(conn)
        self.assertEqual(dailyquota.snapshot(key['id'], day), {'tokens': 120, 'credit_units': 500000})

    def test_daily_charge_rolls_back_with_failed_ledger_transaction(self):
        key = keysvc.create_key('fixture')
        with self.assertRaises(RuntimeError):
            with db.transaction():
                keysvc.touch(key, '127.0.0.1', 120, 0.25)
                raise RuntimeError('synthetic commit failure')
        self.assertEqual(dailyquota.snapshot(key['id'], db.day_of()), {'tokens': 0, 'credit_units': 0})

    def test_unknown_reconciliation_is_idempotent_and_charges_original_day(self):
        key = keysvc.create_key('fixture', daily_credit_quota=10)
        state, _ = self.prepare(key)
        budget.forwarded()
        state.terminal_state = 'cancelled'
        budget.settle(None, 0, 0, None)
        old = int(time.mktime(time.strptime('2026-10-28', '%Y-%m-%d')))
        db.execute('UPDATE budget_reservations SET created_at=?,updated_at=?', (old, old))
        self.assertTrue(budget.resolve(state.request_id, 25, 0.2, 'admin', 'synthetic provider receipt'))
        self.assertFalse(budget.resolve(state.request_id, 25, 0.2, 'admin', 'synthetic retry'))
        self.assertEqual(dailyquota.snapshot(key['id'], '2026-10-28'), {'tokens': 25, 'credit_units': 200000})

    def test_assignment_is_super_admin_only_and_never_returns_a_second_plaintext_key(self):
        response = self.client.post('/api/keys', json={'name': 'fixture', 'assigned_to': 'alice',
            'daily_token_quota': 12345, 'daily_credit_quota': 2.25})
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        self.assertEqual(created['assigned_to'], 'alice')
        updated = self.client.patch('/api/keys/' + str(created['id']), json={'daily_token_quota': 54321}).json()
        self.assertNotIn('key', updated)
        self.assertEqual(updated['daily_token_quota'], 54321)
        self.assertEqual(self.client.patch('/api/keys/' + str(created['id']), json={'assigned_to': 'missing'}).status_code, 400)
        self.user = {'username': 'alice', 'role': 'account_admin'}
        self.assertEqual(self.client.post('/api/keys', json={'name': 'forbidden'}).status_code, 403)
        self.user = {'username': 'machine', 'role': 'viewer', 'scope': 'keys'}
        self.assertEqual(self.client.patch('/api/keys/' + str(created['id']), json={'assigned_to': 'alice'}).status_code, 403)
        self.assertEqual(self.client.patch('/api/keys/' + str(created['id']), json={'assigned_to': ''}).status_code, 403)
        for payload in ({'daily_token_quota': -1}, {'daily_credit_quota': -1}):
            self.user = {'username': 'admin', 'role': 'admin'}
            self.assertEqual(self.client.patch('/api/keys/' + str(created['id']), json=payload).status_code, 422)
