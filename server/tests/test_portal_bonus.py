"""Admin-only real grants; no credit line, no future check-in advances."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from unittest import mock

from server import budget, db, keysvc, observability, portalstore as store, portalwallet as wallet
from server import security, config, tokensvc
from server.tests import test_portal_public_pool as fixtures

REASON = 'synthetic administrator gift, no real provider charge'
NOW = fixtures.NOW


class PortalBonusTests(fixtures.PublicPoolTests):
    def bonus(self, value='100', owner='alice', action='c' * 32, days=30):
        return wallet.grant_bonus(owner, action, value, days, 'admin', REASON)

    def body(self, value='100', action='c' * 32, days=30):
        return {'action_id': action, 'credit': value, 'valid_days': days, 'reason': REASON}

    async def test_gift_has_independent_expiry_no_account_or_future_income(self):
        result = self.bonus('10.123456')
        self.assertEqual(result['expires_at'], NOW + 30 * 86400)
        self.assertTrue(self.bonus('10.123456')['already'])
        view = store.overview('alice')
        self.assertEqual(view['account_count'], 0)
        self.assertEqual(view['today_checkin_units'], 0)
        self.assertEqual(view['callable_units'], store.units('10.123456'))
        self.assertEqual(view['pending_pool_units'], 0)
        self.assertEqual(view['grants'][0]['source'], 'admin_bonus')
        self.assertEqual(store.overview('bob')['balance_units'], 0)
        self.assertEqual(wallet.history_page()['items'][0]['actor'], 'admin')
        self.assertEqual(wallet.history_page()['items'][0]['kind'], 'bonus')
        with mock.patch.object(store, 'now', return_value=NOW + 5 * 86400):
            self.assertEqual(self.bonus('10.123456')['expires_at'], result['expires_at'])

    async def test_only_admin_named_superadmin_session_can_gift(self):
        data = security.load_users()
        data['users'].extend([{'username': 'another-super', 'role': 'admin', 'sv': 1, 'pwd_hash': 'no-login'},
            {'username': 'viewer', 'role': 'viewer', 'sv': 1, 'pwd_hash': 'no-login'}])
        security.save_users(data)
        machine = tokensvc.create_token('synthetic', 'admin')['token']
        key = keysvc.create_key('synthetic-data-key')['key']
        headers = [self.headers(), {},
            {'cookie': f'{config.COOKIE_NAME}={security.issue_token("reader", "account_admin")}', 'origin': 'http://testserver'},
            {'cookie': f'{config.COOKIE_NAME}={security.issue_token("another-super", "admin")}', 'origin': 'http://testserver'},
            {'cookie': f'{config.COOKIE_NAME}={security.issue_token("viewer", "viewer")}', 'origin': 'http://testserver'},
            {'authorization': 'Bearer ' + machine, 'origin': 'http://testserver'},
            {'authorization': 'Bearer ' + key, 'origin': 'http://testserver'},
            {**self.admin, 'origin': 'https://attacker.invalid'}]
        for header in headers:
            response = await self.call('POST', '/api/portal-admin/users/alice/bonus', headers=header, json=self.body())
            self.assertIn(response.status_code, (401, 403), response.text)
        self.assertEqual(store.overview('alice')['balance_units'], 0)
        response = await self.call('POST', '/api/portal-admin/users/alice/bonus', headers=self.admin, json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        repeat = await self.call('POST', '/api/portal-admin/users/alice/bonus', headers=self.admin, json=self.body())
        self.assertTrue(repeat.json()['already'])
        self.assertEqual(store.overview('alice')['balance_units'], store.units(100))
        self.assertNotIn(REASON, (await self.call('GET', '/api/portal/overview')).text)

    async def test_bad_values_conflicting_retry_disabled_and_unknown_user_rejected(self):
        for value in (0, -1, True, None, 'NaN', 'Infinity', '0.0000001', '1000000.000001'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bonus(value)
        for days in (0, 366, True, '30'):
            with self.assertRaises(ValueError):
                self.bonus(days=days)
        with self.assertRaises(ValueError):
            self.bonus(owner='missing')
        with store.transaction() as conn:
            conn.execute('UPDATE portal_users SET enabled=0 WHERE id=?', ('bob',))
        with self.assertRaises(ValueError):
            self.bonus(owner='bob')
        self.bonus()
        for changes in ({'value': 101}, {'days': 31}, {'owner': 'bob'}):
            with self.assertRaises(ValueError):
                self.bonus(**changes)
        self.assertEqual(store.overview('alice')['balance_units'], store.units(100))
        response = await self.call('POST', '/api/portal-admin/users/alice/bonus', headers=self.admin, json={**self.body(), 'credit_line': 1000})
        self.assertEqual(response.status_code, 400)

    async def test_concurrent_retries_credit_once_and_audit_failure_rolls_back(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.bonus(), (0, 1)))
        self.assertEqual(sum(not r['already'] for r in results), 1)
        self.assertEqual(store.overview('alice')['balance_units'], store.units(100))
        with store.transaction() as conn:
            conn.execute("CREATE TRIGGER synthetic_bonus_event_failure BEFORE INSERT ON portal_events WHEN NEW.kind='admin_bonus' BEGIN SELECT RAISE(ABORT,'synthetic audit unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bonus(action='d' * 32)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_bonus_grants').fetchone()[0], 1)

    async def test_empty_wallet_rejects_with_contact_admin_not_future_credit(self):
        made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key'])
            state = self.state('empty-wallet', key); token = observability.current.set(state)
            try:
                with self.assertRaises(budget.BudgetRejected) as raised:
                    wallet.reserve(key)
                self.assertEqual(str(raised.exception), '额度已用尽，请联系管理员赠送额度')
                self.assertEqual(raised.exception.code, 'portal_wallet_empty')
                self.assertEqual(store.overview('alice')['balance_units'], 0)
                self.assertEqual(store.overview('alice')['held_units'], 0)
            finally:
                observability.current.reset(token)

    async def test_bonus_consumes_in_expiry_order_with_account_grants(self):
        await self.pooled(amount=5, expiry=NOW + 10 * 86400)
        self.bonus('10', days=1)
        made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('mixed-wallet', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key); wallet.forwarded(state); state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 12)
                wallet.finish(state); wallet.finish(state)
                self.assertEqual(store.overview('alice')['balance_units'], store.units(3))
                with store.transaction() as conn:
                    self.assertEqual(conn.execute('SELECT remaining_units FROM portal_bonus_grants').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT remaining_units FROM portal_grants').fetchone()[0], store.units(3))
            finally:
                observability.current.reset(token)

    async def test_bonus_expiry_unsent_release_and_expired_held_return(self):
        self.bonus('10', days=1); made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('bonus-unsent', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key); wallet.finish(state)
                self.assertEqual(store.overview('alice')['balance_units'], store.units(10))
                state = self.state('bonus-expired-inflight', key); observability.current.set(state)
                wallet.reserve(key); wallet.forwarded(state); state.terminal_state = 'completed'
                with db.transaction():
                    wallet.receipt(state, {'prompt_tokens': 1, 'completion_tokens': 1}, 2)
                with mock.patch.object(store, 'now', return_value=NOW + 86401):
                    wallet.finish(state)
                    view = store.overview('alice')
                    self.assertEqual(view['balance_units'], 0)
                    self.assertEqual(next(e['amount_units'] for e in view['events'] if e['kind'] == 'expired'), -store.units(8))
                    self.assertEqual(store.overview('alice')['held_units'], 0)
            finally:
                observability.current.reset(token)

    async def test_unused_bonus_expires_once_without_touching_later_daily_grant(self):
        await self.pooled(amount=20, expiry=NOW + 10 * 86400)
        self.bonus('10', days=1)
        with mock.patch.object(store, 'now', return_value=NOW + 86401):
            first = store.overview('alice'); second = store.overview('alice')
        self.assertEqual(first['balance_units'], store.units(20))
        self.assertEqual(sum(e['kind'] == 'expired' for e in second['events']), 1)
        self.assertEqual(next(e['amount_units'] for e in first['events'] if e['kind'] == 'expired'), -store.units(10))

    async def test_bonus_does_not_erase_unknown_hold_or_debt(self):
        self.bonus('10'); made = await self.make_key(); db.execute(wallet.RECEIPTS)
        with self.enabled():
            key = keysvc.resolve(made['key']); state = self.state('bonus-unknown', key)
            token = observability.current.set(state)
            try:
                wallet.reserve(key); wallet.forwarded(state); wallet.finish(state)
                self.bonus('20', action='d' * 32)
                self.assertEqual(store.overview('alice')['held_units'], store.units(10))
                self.assertEqual(store.overview('alice')['balance_units'], store.units(20))
                with self.assertRaises(budget.BudgetRejected) as raised:
                    wallet.reserve(key)
                self.assertEqual(raised.exception.code, 'portal_wallet_pending')
            finally:
                observability.current.reset(token)
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_debts VALUES(?,?)', ('alice', store.units(5)))
        wallet.adjust_debt('alice', 'e' * 32, 'repay', 3, store.units(5), 'admin', REASON, REASON)
        self.assertEqual(store.overview('alice')['balance_units'], store.units(17))
        self.assertEqual(store.overview('alice')['debt_units'], store.units(2))
        self.assertEqual(store.overview('alice')['held_units'], store.units(10))

    async def test_admin_revoked_at_commit_cannot_gift(self):
        from fastapi import HTTPException
        with mock.patch.object(security, 'authorization_owner', side_effect=HTTPException(status_code=401, detail='synthetic revoked')):
            response = await self.call('POST', '/api/portal-admin/users/alice/bonus', headers=self.admin, json=self.body())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(store.overview('alice')['balance_units'], 0)

    async def test_gateway_can_use_admin_bonus_without_scan_and_stops_when_spent(self):
        import httpx
        from fastapi import FastAPI
        from server import platformpolicy, portalpolicy
        from server.routers import gateway
        from server.tests.test_audit_20261029_gateway_ledger import GOOD
        made = await self.make_key(); self.bonus('0.01')
        app = FastAPI(); app.include_router(gateway.router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json=GOOD)
        def factory(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with self.enabled(), mock.patch.object(config, 'http_client', side_effect=factory), mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 0), \
                mock.patch('server.services.wb2api.load_upstream_config', return_value={}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                for expected in (200, 429):
                    response = await client.post('/v1/chat/completions', json={'model': portalpolicy.MODELS[0], 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8},
                        headers={'authorization': 'Bearer ' + made['key']})
                    self.assertEqual(response.status_code, expected, response.text)
                self.assertIn('额度已用尽，请联系管理员赠送额度', response.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.overview('alice')['account_count'], 0)
        self.assertEqual(store.overview('alice')['debt_units'], 0)


for cls in (fixtures.PublicPoolTests, fixtures.fixtures.PortalTests):
    for name in tuple(vars(cls)):
        if name.startswith('test_') and name not in vars(PortalBonusTests):
            setattr(PortalBonusTests, name, None)
del cls, name
