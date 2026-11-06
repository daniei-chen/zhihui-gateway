"""Disposable accounting operations: exact money, scope, conflicts and rollback."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from unittest import mock

from server import budgetowner, db, security, config, portalwallet as wallet, portalstore as store
from server.tests import test_user_portal as fixtures

NOW = fixtures.NOW
REASON = 'synthetic reason verified by operator'
EVIDENCE = 'synthetic provider receipt reference 001'


class PortalOperationsTests(fixtures.PortalTests):
    def setUp(self):
        super().setUp()
        self.addCleanup(budgetowner.close)

    async def grant(self, owner='alice', uid='ops-account', amount=100, expiry=None, pooled=True):
        out, _ = await self.bind(store.user(owner), uid, amount, expiry)
        if pooled:
            with store.transaction() as conn:
                conn.execute('INSERT INTO portal_pool_links VALUES(?,?,?)', (out['id'], 'workbuddy-' + uid + '.json', NOW))
        return out['id']

    def debt(self, amount=10, owner='alice'):
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_debts VALUES(?,?)', (owner, store.units(amount)))

    def adjust(self, action='a' * 32, kind='repay', amount=4, expected=10, owner='alice'):
        return wallet.adjust_debt(owner, action, kind, amount, store.units(expected), 'admin', REASON, EVIDENCE)

    def unknown(self, request='synthetic-unknown', owner='alice', state='unknown'):
        with store.transaction() as conn:
            account = dict(conn.execute('SELECT * FROM portal_accounts WHERE owner_id=?', (owner,)).fetchone())
            grant = conn.execute('SELECT id,remaining_units FROM portal_grants WHERE account_id=?', (account['id'],)).fetchone()
            conn.execute('INSERT INTO portal_spends VALUES(?,?,?,?,?,?,?,?,?)',
                (request, owner, 1, 'synthetic-worker', state, NOW, grant['remaining_units'], None, None))
            conn.execute('INSERT INTO portal_allocations VALUES(?,?,?)', (request, grant['id'], grant['remaining_units']))
            conn.execute('UPDATE portal_grants SET remaining_units=0 WHERE id=?', (grant['id'],))
        return request

    async def test_repay_only_owners_grants_by_earliest_expiry_once(self):
        early = await self.grant(amount=3, expiry=NOW + 10)
        late = await self.grant(uid='ops-later', amount=20, expiry=NOW + 20)
        await self.grant('bob', 'bob-account', amount=80)
        self.debt()
        result = self.adjust(amount=4)
        self.assertEqual(result['after_units'], 6 * store.SCALE)
        self.assertEqual(self.adjust(amount=4), {**result, 'already': True})
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT remaining_units FROM portal_grants WHERE account_id=?', (early,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT remaining_units FROM portal_grants WHERE account_id=?', (late,)).fetchone()[0], 19 * store.SCALE)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_debt_actions').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT SUM(amount_units) FROM portal_debt_allocations').fetchone()[0], 4 * store.SCALE)
        self.assertEqual(store.overview('bob')['balance_units'], 80 * store.SCALE)
        event = next(e for e in store.overview('alice')['events'] if e['kind'] == 'debt_repaid')
        self.assertEqual(event['amount_units'], -4 * store.SCALE)

    async def test_waive_never_mints_balance_or_rewrites_supplier_charge(self):
        await self.grant(amount=100); self.debt()
        db.execute('INSERT INTO request_logs(ts,model,status,credit,request_id) VALUES(?,?,?,?,?)', (NOW, 'deepseek-v4-pro', 200, 110, 'original-cost'))
        self.adjust(kind='waive', amount=10)
        view = store.overview('alice')
        self.assertEqual(view['debt_units'], 0)
        self.assertEqual(view['balance_units'], 100 * store.SCALE)
        self.assertEqual(next(e for e in view['events'] if e['kind'] == 'debt_waived')['amount_units'], 0)
        self.assertEqual(db.query_one('SELECT credit FROM request_logs WHERE request_id=?', ('original-cost',))[0], 110)

    async def test_expired_unpooled_and_frozen_grants_cannot_repay(self):
        await self.grant(amount=30); self.unknown()
        await self.grant(uid='expired', amount=50, expiry=NOW + 1)
        await self.grant(uid='unpooled', amount=50, pooled=False)
        self.debt()
        with mock.patch.object(store, 'now', return_value=NOW + 2), self.assertRaises(ValueError):
            self.adjust(amount=10)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_debt_actions').fetchone()[0], 0)
        self.assertEqual(store.overview('alice')['held_units'], 30 * store.SCALE)
        self.assertEqual(store.overview('alice')['debt_units'], 10 * store.SCALE)

    async def test_stale_debt_and_conflicting_retry_are_rejected(self):
        await self.grant(); self.debt()
        self.adjust()
        for kwargs in ({'action': 'b' * 32}, {'amount': 5}, {'kind': 'waive'}):
            with self.assertRaises(ValueError):
                self.adjust(**kwargs)
        self.assertEqual(store.overview('alice')['debt_units'], 6 * store.SCALE)
        self.assertEqual(store.overview('alice')['balance_units'], 96 * store.SCALE)

    async def test_two_operators_only_one_stale_form_can_change_debt(self):
        await self.grant(); self.debt()
        def one(action):
            try:
                self.adjust(action=action); return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(one, ('a' * 32, 'b' * 32))), 1)
        self.assertEqual(store.overview('alice')['debt_units'], 6 * store.SCALE)

    async def test_audit_insert_failure_rolls_back_grants_debt_and_events(self):
        await self.grant(); self.debt()
        with store.transaction() as conn:
            conn.execute("CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON portal_debt_actions BEGIN SELECT RAISE(ABORT,'synthetic audit unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.adjust()
        view = store.overview('alice')
        self.assertEqual(view['debt_units'], 10 * store.SCALE)
        self.assertEqual(view['balance_units'], 100 * store.SCALE)
        self.assertFalse(any(e['kind'] == 'debt_repaid' for e in view['events']))

    async def test_invalid_amount_kind_action_id_and_evidence_never_mutate(self):
        await self.grant(); self.debt()
        for amount in (0, -1, True, float('nan'), float('inf'), '0.0000001', 11):
            with self.assertRaises(ValueError):
                self.adjust(amount=amount)
        for args in [('invalid', 'repay', REASON, EVIDENCE), ('a' * 32, 'refund', REASON, EVIDENCE),
                ('a' * 32, 'repay', 'short', EVIDENCE), ('a' * 32, 'repay', REASON, 'x' * 501)]:
            action, kind, reason, evidence = args
            with self.assertRaises(ValueError):
                wallet.adjust_debt('alice', action, kind, 1, 10 * store.SCALE, 'admin', reason, evidence)
        self.assertEqual(store.overview('alice')['balance_units'], 100 * store.SCALE)
        self.assertEqual(store.overview('alice')['debt_units'], 10 * store.SCALE)

    async def test_pending_worker_status_and_receipt_conflicts_are_truthful(self):
        await self.grant(); request = self.unknown()
        for live, status in ((True, 'alive'), (None, 'unproven'), (False, 'dead')):
            with mock.patch.object(budgetowner, 'alive', return_value=live):
                row = wallet.pending_page()['items'][0]
                self.assertEqual(row['worker_status'], status)
                self.assertEqual(row['can_resolve'], live is False)
                self.assertNotIn('worker', row)
        db.execute(wallet.RECEIPTS)
        db.execute('INSERT INTO portal_spend_receipts VALUES(?,?,?)', (request, 'bob', json.dumps({'amount_units': store.units(2)})))
        with mock.patch.object(budgetowner, 'alive', return_value=False):
            self.assertFalse(wallet.pending_page()['items'][0]['can_resolve'])
            with self.assertRaises(ValueError):
                wallet.resolve_unknown(request, 2, 'admin', REASON, EVIDENCE)
        self.assertEqual(store.overview('alice')['held_units'], 100 * store.SCALE)

    async def test_pending_spool_or_dead_letters_block_manual_resolution(self):
        await self.grant(); request = self.unknown()
        from server import ledgerreplay
        spool = self.root / 'spool-local'
        with mock.patch.object(ledgerreplay, 'spool_dir', return_value=spool), mock.patch.object(budgetowner, 'alive', return_value=False):
            spool.mkdir()
            for path in (spool / 'late-receipt.json', spool / 'dead' / 'unknown.json'):
                path.parent.mkdir(exist_ok=True)
                path.write_text('{}')
                self.assertFalse(wallet.pending_page()['items'][0]['can_resolve'])
                with self.assertRaises(ValueError):
                    wallet.resolve_unknown(request, 0, 'admin', REASON, EVIDENCE)
                path.unlink()
        self.assertEqual(store.overview('alice')['held_units'], 100 * store.SCALE)
        (config.DATA_DIR / '.spool-used').write_text('synthetic used marker')
        with mock.patch.object(ledgerreplay, 'spool_dir', return_value=self.root / 'missing-spool'):
            self.assertIn('通道身份不可核实', wallet.replay_problem())

    async def test_known_receipt_cannot_be_manually_replaced_with_zero(self):
        await self.grant(); request = self.unknown()
        db.execute(wallet.RECEIPTS)
        db.execute('INSERT INTO portal_spend_receipts VALUES(?,?,?)', (request, 'alice', json.dumps({'amount_units': store.units(2)})))
        with mock.patch.object(budgetowner, 'alive', return_value=False):
            self.assertEqual(wallet.pending_page()['items'][0]['receipt_units'], store.units(2))
            with self.assertRaises(ValueError):
                wallet.resolve_unknown(request, 0, 'admin', REASON, EVIDENCE)
        self.assertEqual(store.overview('alice')['held_units'], 100 * store.SCALE)

    async def test_late_receipt_conflict_never_silently_overwrites_resolution(self):
        await self.grant(); request = self.unknown()
        with mock.patch.object(budgetowner, 'alive', return_value=False):
            wallet.resolve_unknown(request, 2, 'admin', REASON, EVIDENCE)
        db.execute(wallet.RECEIPTS)
        facts = {'request_id': request, 'terminal_state': 'completed', 'usage_complete': 1, 'credit': 3}
        with self.assertRaises(ValueError), db.transaction():
            wallet.replay_receipt({'budget': {'portal_owner': 'alice'}}, facts)
        self.assertIsNone(db.query_one('SELECT * FROM portal_spend_receipts WHERE request_id=?', (request,)))
        self.assertEqual(store.overview('alice')['balance_units'], 98 * store.SCALE)
        with db.transaction():
            wallet.replay_receipt({'budget': {'portal_owner': 'alice'}}, {**facts, 'credit': 2})
        self.assertEqual(wallet.history_page()['total'], 1)

    async def test_pending_history_and_debts_pagination(self):
        await self.grant(); self.debt(amount=40)
        for index in range(31):
            self.adjust(action=f'{index:032x}', amount=1, expected=40 - index)
        first, second = wallet.history_page(1), wallet.history_page(2)
        self.assertEqual(first['total'], 31)
        self.assertEqual(len(first['items']), 30)
        self.assertEqual(len(second['items']), 1)
        self.assertTrue(set(row['id'] for row in first['items']).isdisjoint(row['id'] for row in second['items']))
        self.assertEqual(wallet.debts_page()['items'][0]['amount_units'], 9 * store.SCALE)
        self.assertEqual(wallet.history_page(-1)['page'], 1)
        self.assertEqual(wallet.history_page(10001)['page'], 10000)

    async def test_pending_and_debt_pages_are_bounded_and_deterministic(self):
        with store.transaction() as conn:
            for index in range(31):
                owner = f'owner{index:02d}'
                conn.execute('INSERT INTO portal_users(id,username,pwd_hash,created_at) VALUES(?,?,?,?)', (owner, owner, 'no-login', NOW))
                conn.execute('INSERT INTO portal_debts VALUES(?,?)', (owner, 1))
                conn.execute('INSERT INTO portal_spends VALUES(?,?,?,?,?,?,?,?,?)', (f'request{index:02d}', owner, 1, 'synthetic', 'unknown', NOW, 1, None, None))
        with mock.patch.object(budgetowner, 'alive', return_value=None):
            first, second = wallet.pending_page(), wallet.pending_page(2)
        self.assertEqual(first['total'], 31)
        self.assertEqual(len(first['items']), 30)
        self.assertEqual(len(second['items']), 1)
        self.assertEqual(second['items'][0]['request_id'], 'request30')
        self.assertFalse(first['items'][0]['can_resolve'])
        self.assertEqual(len(wallet.debts_page()['items']), 30)
        self.assertEqual(len(wallet.debts_page(2)['items']), 1)

    async def test_clearing_debt_does_not_remove_unknown_request_hold(self):
        await self.grant(amount=10); self.unknown(); self.debt()
        self.adjust(kind='waive', amount=10)
        self.assertEqual(store.overview('alice')['debt_units'], 0)
        self.assertEqual(store.overview('alice')['held_units'], 10 * store.SCALE)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT state FROM portal_spends').fetchone()[0], 'unknown')

    async def test_revoke_admin_before_commit_cannot_adjust_wallet(self):
        from fastapi import HTTPException
        from server import portalwallet
        await self.grant(); self.debt()
        def guard_failure(*args, **kwargs):
            raise AssertionError('revoked administrator must not reach accounting')
        with mock.patch.object(security, 'authorization_owner', side_effect=HTTPException(status_code=401, detail='synthetic revoked')), \
                mock.patch.object(portalwallet, 'adjust_debt', side_effect=guard_failure):
            response = await self.call('POST', '/api/portal-admin/wallet-debts/alice/adjust', headers=self.admin,
                json={'action_id': 'a' * 32, 'kind': 'repay', 'credit': '2', 'expected_units': store.units(10), 'reason': REASON, 'evidence': EVIDENCE})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(store.overview('alice')['debt_units'], store.units(10))

    async def test_api_superadmin_session_and_same_origin_required(self):
        await self.grant(); self.debt()
        paths = ('wallet-pending', 'wallet-debts', 'wallet-history')
        account_admin = {'cookie': f'{config.COOKIE_NAME}={security.issue_token("reader", "account_admin")}', 'origin': 'http://testserver'}
        for path in paths:
            for header in (self.headers(), account_admin, {}):
                response = await self.call('GET', '/api/portal-admin/' + path, headers=header)
                self.assertIn(response.status_code, (401, 403))
        body = {'action_id': 'a' * 32, 'kind': 'repay', 'credit': '2', 'expected_units': 10 * store.SCALE, 'reason': REASON, 'evidence': EVIDENCE}
        response = await self.call('POST', '/api/portal-admin/wallet-debts/alice/adjust', headers={**self.admin, 'origin': 'https://attacker.invalid'}, json=body)
        self.assertEqual(response.status_code, 403)
        response = await self.call('POST', '/api/portal-admin/wallet-debts/alice/adjust', headers=self.admin, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        retry = await self.call('POST', '/api/portal-admin/wallet-debts/alice/adjust', headers=self.admin, json=body)
        self.assertTrue(retry.json()['already'])
        self.assertEqual(store.overview('alice')['balance_units'], 98 * store.SCALE)
        history = await self.call('GET', '/api/portal-admin/wallet-history', headers=self.admin)
        self.assertEqual(history.json()['items'][0]['actor'], 'admin')
        self.assertEqual(history.json()['items'][0]['before_units'], 10 * store.SCALE)
        self.assertEqual(history.json()['items'][0]['after_units'], 8 * store.SCALE)
        own = await self.call('GET', '/api/portal/overview')
        self.assertNotIn(EVIDENCE, own.text)
        self.assertNotIn(REASON, own.text)

    async def test_unknown_resolution_api_only_dead_and_once_preserves_expiry(self):
        await self.grant(expiry=NOW + 10); request = self.unknown()
        body = {'credit': '2.123456', 'reason': REASON, 'evidence': EVIDENCE}
        for live in (True, None):
            with mock.patch.object(budgetowner, 'alive', return_value=live):
                response = await self.call('POST', f'/api/portal-admin/wallet-pending/{request}/resolve', headers=self.admin, json=body)
                self.assertEqual(response.status_code, 409)
        with mock.patch.object(budgetowner, 'alive', return_value=False):
            response = await self.call('POST', f'/api/portal-admin/wallet-pending/{request}/resolve', headers=self.admin, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            repeat = await self.call('POST', f'/api/portal-admin/wallet-pending/{request}/resolve', headers=self.admin, json=body)
            self.assertEqual(repeat.status_code, 409)
        view = store.overview('alice')
        self.assertEqual(view['balance_units'], store.units('97.876544'))
        self.assertEqual(view['grants'][0]['expires_at'], NOW + 10)
        self.assertEqual(wallet.history_page()['items'][0]['amount_units'], store.units(body['credit']))


for _name in tuple(vars(fixtures.PortalTests)):
    if _name.startswith('test_'):
        setattr(PortalOperationsTests, _name, None)
