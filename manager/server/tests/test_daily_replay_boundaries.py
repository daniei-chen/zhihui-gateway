"""Synthetic-only regression coverage for replay/manual accounting and midnight."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import budget, budgetowner, config, dailyquota, db, keysvc, ledger, ledgerreplay, observability


class DailyReplayBoundariesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_conn = db._conn
        db._conn = None
        self.addCleanup(self.cleanup_db)
        root = Path(self.tmp.name)
        for name, value in {'DATA_DIR': root, 'DB_PATH': root / 'synthetic.db',
                            'USERS_FILE': root / 'users.json'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {'WB_LEDGER_SPOOL_DIR': str(root / 'spool'),
                                            'WB_KEY_BUDGET_PENDING_QUOTA': '1'})
        patch.start(); self.addCleanup(patch.stop)
        budget.initialize()
        self.token = observability.current.set(None)
        self.addCleanup(observability.current.reset, self.token)
        self.now = int(time.mktime(time.strptime('2026-10-29 12:00:00', '%Y-%m-%d %H:%M:%S')))
        self.yesterday = self.now - 86400
        self.key = keysvc.create_key('synthetic-replay', daily_token_quota=20000, daily_credit_quota=10)

    def cleanup_db(self):
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = self.old_conn

    def reserve(self, rid, *, ts=None, state='unknown', tokens=0, credit=0):
        ts = self.now if ts is None else ts
        db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
                   'reserved_tokens,charged_tokens,charged_credit,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                   (rid, self.key['id'], ts, state, 5000, tokens, credit, ts))

    def facts(self, rid, **updates):
        facts = {'ts': self.now, 'key_id': self.key['id'], 'ip': '127.0.0.1',
                 'model': 'fixture-model', 'mapped_model': 'fixture-model', 'status': 200,
                 'prompt_tokens': 20, 'completion_tokens': 5, 'latency_ms': 10,
                 'stream': 0, 'credit': 0.2, 'realm': 'cn', 'request_id': rid,
                 'terminal_state': 'completed', 'usage_source': 'upstream',
                 'usage_complete': 1, 'platform': 'workbuddy'}
        facts.update(updates)
        return facts

    def apply(self, facts, receipt=None):
        return ledgerreplay.apply(ledgerreplay.build_entry(facts), receipt or facts['request_id'])

    def totals(self):
        return tuple(db.query_one('SELECT used_tokens,used_credit_units FROM api_keys WHERE id=?',
                                  (self.key['id'],)))

    def assert_totals(self, tokens, credit_units, day='2026-10-29'):
        self.assertEqual(self.totals(), (tokens, credit_units))
        self.assertEqual(dailyquota.snapshot(self.key['id'], day),
                         {'tokens': tokens, 'credit_units': credit_units})

    def test_manual_resolution_then_real_replay_never_double_charges(self):
        self.reserve('manual')
        budget.resolve('manual', 25, 0.2, 'admin', 'synthetic verified receipt')
        adjustment = dict(db.query_one('SELECT * FROM adjustment_events WHERE request_id=?', ('manual',)))
        self.assertEqual(self.apply(self.facts('manual')), 'applied')
        self.assertEqual(self.apply(self.facts('manual')), 'duplicate')
        self.assertEqual(self.apply(self.facts('manual'), 'second-file'), 'applied')
        self.assert_totals(25, 200000)
        self.assertEqual(dict(db.query_one('SELECT * FROM adjustment_events WHERE request_id=?', ('manual',))), adjustment)
        self.assertEqual(db.query_one('SELECT counted FROM usage_events WHERE request_id=?', ('manual',))[0], 0)
        self.assertEqual(db.query_one('SELECT count(*) FROM request_logs WHERE request_id=?', ('manual',))[0], 1)
        self.assertEqual(db.query_one('SELECT requests FROM usage_daily')[0], 1)

    def test_real_replay_first_then_manual_resolution_is_noop(self):
        self.reserve('replay-first')
        self.apply(self.facts('replay-first'))
        self.assertFalse(budget.resolve('replay-first', 25, 0.2, 'admin', 'synthetic verified receipt'))
        self.assert_totals(25, 200000)
        self.assertEqual(db.query_one('SELECT count(*) FROM adjustment_events')[0], 0)

    def test_confirmed_zero_is_not_treated_as_an_absent_charge(self):
        self.reserve('zero')
        budget.resolve('zero', 0, 0, 'admin', 'synthetic confirmed zero')
        self.apply(self.facts('zero', prompt_tokens=0, completion_tokens=0, credit=0))
        self.assert_totals(0, 0)
        self.assertEqual(db.query_one('SELECT count(*) FROM adjustment_events')[0], 1)

    def test_confirmed_zero_accepts_unknown_partial_evidence_without_inventing_credit(self):
        self.reserve('zero-partial')
        budget.resolve('zero-partial', 0, 0, 'admin', 'synthetic confirmed zero')
        self.apply(self.facts('zero-partial', prompt_tokens=0, completion_tokens=0,
                              credit=None, usage_complete=0, terminal_state='client_cancelled'))
        self.assert_totals(0, 0)
        self.assertEqual(tuple(db.query_one('SELECT state,charged_tokens,charged_credit FROM budget_reservations')),
                         ('settled', 0, 0))
        self.assertEqual(tuple(db.query_one('SELECT credit_units,counted FROM usage_events')), (None, 0))
        self.assertIsNone(db.query_one('SELECT credit FROM request_logs')[0])

    def test_conflicting_receipt_preserves_manual_evidence_and_is_dead_lettered(self):
        self.reserve('conflict')
        budget.resolve('conflict', 0, 0, 'admin', 'synthetic confirmed zero')
        path = Path(ledgerreplay.spill(ledgerreplay.build_entry(self.facts('conflict'))))
        original_facts = json.loads(path.read_text(encoding='utf-8'))['facts']
        with self.assertLogs('workbuddy.ledgerreplay', level='ERROR'):
            for _ in range(ledgerreplay.MAX_ATTEMPTS):
                result = ledgerreplay.replay_batch()
        self.assertEqual(result['dead_pending'], 1)
        dead = ledgerreplay.dead_dir() / path.name
        self.assertEqual(json.loads(dead.read_text(encoding='utf-8'))['facts'], original_facts)
        self.assert_totals(0, 0)
        for table in ('request_logs', 'usage_events', 'ledger_receipts', 'usage_daily'):
            self.assertEqual(db.query_one('SELECT count(*) FROM ' + table)[0], 0)
        self.assertEqual(db.query_one('SELECT count(*) FROM adjustment_events')[0], 1)
        self.assertTrue(ledgerreplay.admission_block()['blocked'])

    def test_partial_receipt_cannot_erase_or_recharge_a_confirmed_resolution(self):
        self.reserve('partial')
        budget.resolve('partial', 25, 0.2, 'admin', 'synthetic verified receipt')
        self.apply(self.facts('partial', prompt_tokens=10, completion_tokens=0, credit=None,
                              usage_complete=0, terminal_state='client_cancelled'))
        self.assert_totals(25, 200000)
        event = db.query_one('SELECT prompt_tokens,credit_units,counted FROM usage_events')
        self.assertEqual(tuple(event), (10, None, 0))
        self.assertEqual(db.query_one('SELECT charged_tokens,charged_credit FROM budget_reservations')[0], 25)

    def test_already_charged_partial_usage_applies_only_nonnegative_delta(self):
        self.reserve('delta', tokens=10, credit=0.1)
        with db.transaction():
            budget.charge_delta(self.key['id'], 10, 100000, self.now)
        self.apply(self.facts('delta'))
        self.assert_totals(25, 200000)
        self.assertEqual(tuple(db.query_one('SELECT state,charged_tokens,charged_credit FROM budget_reservations')),
                         ('settled', 25, 0.2))

    def test_weaker_partial_unknown_receipt_preserves_known_lower_bound(self):
        self.reserve('lower-bound', tokens=25, credit=0.2)
        with db.transaction():
            budget.charge_delta(self.key['id'], 25, 200000, self.now)
        self.apply(self.facts('lower-bound', prompt_tokens=10, completion_tokens=0, credit=None,
                              usage_complete=0, terminal_state='client_cancelled'))
        self.assert_totals(25, 200000)
        self.assertEqual(tuple(db.query_one('SELECT state,charged_tokens,charged_credit FROM budget_reservations')),
                         ('unknown', 25, 0.2))

    def test_known_smaller_or_mismatched_receipts_fail_without_silent_refund(self):
        self.reserve('protected', tokens=25, credit=0.2)
        with db.transaction():
            budget.charge_delta(self.key['id'], 25, 200000, self.now)
        cases = [{'prompt_tokens': 1, 'completion_tokens': 0}, {'credit': 0.1},
                 {'key_id': self.key['id'] + 1}, {'ts': self.yesterday}, {'credit': -0.1}]
        for index, updates in enumerate(cases):
            with self.subTest(updates=updates), self.assertRaises(ledgerreplay.ReplayConflict):
                self.apply(self.facts('protected', **updates), 'conflict-' + str(index))
            self.assert_totals(25, 200000)
            for table in ('request_logs', 'usage_events', 'ledger_receipts', 'usage_daily'):
                self.assertEqual(db.query_one('SELECT count(*) FROM ' + table)[0], 0)

    def test_complete_smaller_receipt_never_overwrites_a_manual_resolution(self):
        self.reserve('manual-authority')
        budget.resolve('manual-authority', 25, 0.2, 'admin', 'synthetic verified receipt')
        before = dict(db.query_one('SELECT * FROM adjustment_events'))
        with self.assertRaises(ledgerreplay.ReplayConflict):
            self.apply(self.facts('manual-authority', prompt_tokens=1, completion_tokens=0, credit=0))
        self.assert_totals(25, 200000)
        self.assertEqual(dict(db.query_one('SELECT * FROM adjustment_events')), before)
        self.assertEqual(db.query_one('SELECT count(*) FROM request_logs')[0], 0)

    def test_reconciliation_day_mismatch_is_retained_instead_of_moving_old_charge(self):
        self.reserve('manual-day', ts=self.yesterday)
        budget.resolve('manual-day', 25, 0.2, 'admin', 'synthetic verified receipt')
        with self.assertRaises(ledgerreplay.ReplayConflict):
            self.apply(self.facts('manual-day'))
        self.assert_totals(25, 200000, '2026-10-28')
        self.assertEqual(dailyquota.snapshot(self.key['id'], '2026-10-29'), {'tokens': 0, 'credit_units': 0})

    def test_replay_failure_rolls_back_the_delta_and_all_evidence(self):
        self.reserve('rollback', tokens=10, credit=0.1)
        with db.transaction():
            budget.charge_delta(self.key['id'], 10, 100000, self.now)
        with mock.patch.object(ledger, 'record', side_effect=RuntimeError('synthetic ledger failure')):
            with self.assertRaises(RuntimeError):
                self.apply(self.facts('rollback'))
        self.assert_totals(10, 100000)
        self.assertEqual(tuple(db.query_one('SELECT state,charged_tokens FROM budget_reservations')), ('unknown', 10))
        for table in ('request_logs', 'usage_events', 'ledger_receipts', 'usage_daily'):
            self.assertEqual(db.query_one('SELECT count(*) FROM ' + table)[0], 0)

    def test_existing_log_rejects_changed_accounting_but_not_changed_labels(self):
        original = self.facts('canonical')
        db.add_request_log(**original)
        self.apply(self.facts('canonical', error='stale label', account='stale-account',
                              model='stale-model', mapped_model='stale-model', status=502,
                              terminal_state='transport_error', usage_source='upstream'))
        event = dict(db.query_one('SELECT * FROM usage_events WHERE request_id=?', ('canonical',)))
        self.assertEqual((event['model'], event['terminal_state']), ('fixture-model', 'completed'))
        self.assert_totals(0, 0)
        cases = [{'prompt_tokens': 21}, {'completion_tokens': 6}, {'credit': 0.3},
                 {'credit': None}, {'key_id': self.key['id'] + 1}, {'ts': self.yesterday},
                 {'usage_complete': 0}, {'usage_source': 'unknown'}]
        for index, updates in enumerate(cases):
            with self.subTest(updates=updates), self.assertRaises(ledgerreplay.ReplayConflict):
                self.apply(self.facts('canonical', **updates), 'changed-' + str(index))
        self.assertEqual(dict(db.query_one('SELECT * FROM usage_events WHERE request_id=?', ('canonical',))), event)
        self.assertEqual(db.query_one('SELECT count(*) FROM ledger_receipts')[0], 1)
        self.assert_totals(0, 0)

    def test_pruned_log_with_immutable_usage_is_not_a_new_billable_request(self):
        self.apply(self.facts('retained-event'))
        event = dict(db.query_one('SELECT * FROM usage_events WHERE request_id=?', ('retained-event',)))
        db.execute('DELETE FROM request_logs')
        db.execute('DELETE FROM ledger_receipts')
        with self.assertRaises(ledgerreplay.ReplayConflict):
            self.apply(self.facts('retained-event'))
        self.assert_totals(25, 200000)
        self.assertEqual(dict(db.query_one('SELECT * FROM usage_events WHERE request_id=?', ('retained-event',))), event)
        self.assertEqual(db.query_one('SELECT count(*) FROM request_logs')[0], 0)
        self.assertEqual(db.query_one('SELECT count(*) FROM ledger_receipts')[0], 0)

    def test_same_day_receipt_time_drift_does_not_rewrite_canonical_time(self):
        original = self.facts('time-drift')
        db.add_request_log(**original)
        self.apply(self.facts('time-drift', ts=self.now + 1))
        self.assertEqual(db.query_one('SELECT ts FROM request_logs')[0], self.now)
        self.assert_totals(0, 0)

    def prepare(self, rid='next'):
        state = observability.RequestAudit(rid, '/v1/chat/completions', 'POST', 'openai', time.monotonic())
        observability.current.set(state)
        with mock.patch.object(budget.time, 'time', return_value=self.now):
            budget.prepare(self.key, {'messages': [], 'max_tokens': 1})

    def test_old_unknown_does_not_move_to_today_when_update_time_changes(self):
        self.reserve('old', ts=self.yesterday)
        db.execute('UPDATE budget_reservations SET updated_at=?', (self.now - 901,))
        self.prepare()

    def test_old_partial_replay_does_not_block_todays_daily_credit(self):
        self.reserve('old-replay', ts=self.yesterday)
        with mock.patch.object(ledgerreplay.time, 'time', return_value=self.now - 901):
            self.apply(self.facts('old-replay', ts=self.yesterday, credit=None, usage_complete=0))
        self.prepare()
        self.assertEqual(dailyquota.snapshot(self.key['id'], '2026-10-29'), {'tokens': 0, 'credit_units': 0})
        self.assertEqual(dailyquota.snapshot(self.key['id'], '2026-10-28')['tokens'], 25)

    def test_log_day_keeps_cross_midnight_unknown_in_the_correct_daily_window(self):
        self.reserve('crossed', ts=self.yesterday)
        self.apply(self.facts('crossed', credit=None, usage_complete=0))
        db.execute('UPDATE budget_reservations SET updated_at=?', (self.now - 901,))
        with self.assertRaises(budget.BudgetRejected) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'daily_credit_unconfirmed')

    def test_same_day_unknown_stays_held_after_mutable_update_time_is_old(self):
        self.reserve('today')
        db.execute('UPDATE budget_reservations SET updated_at=?', (self.yesterday,))
        with self.assertRaises(budget.BudgetRejected) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'daily_credit_unconfirmed')

    def test_active_request_still_carries_across_midnight(self):
        self.reserve('active', ts=self.yesterday, state='forwarded')
        with self.assertRaises(budget.BudgetRejected) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'budget_pending')

    def test_daily_token_hold_for_active_cross_midnight_request_survives_raised_parallelism(self):
        self.reserve('active-token', ts=self.yesterday, state='forwarded')
        keysvc.update_key(self.key['id'], {'daily_token_quota': 7000})
        with mock.patch.dict(os.environ, {'WB_KEY_BUDGET_PENDING_QUOTA': '2'}):
            with self.assertRaises(budget.BudgetRejected) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, 'daily_token_quota_exhausted')


if __name__ == '__main__':
    unittest.main()
