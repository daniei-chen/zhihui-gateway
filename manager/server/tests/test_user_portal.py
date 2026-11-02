"""Disposable portal fixtures: ownership, expiry and shared idempotent check-in."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi import HTTPException

from server import config, db, security, portalstore as store, portalservice as service
from server.routers import portal, accounts
from server.services import tencent

NOW = 1793600000


class PortalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portal-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in {'DATA_DIR': self.root / 'data', 'DB_PATH': self.root / 'manager.db',
                           'USERS_FILE': self.root / 'users.json', 'AUTH_DIR': self.root / 'pool'}.items():
            self.stack.enter_context(mock.patch.object(config, key, value))
        self.stack.enter_context(mock.patch.object(store, 'now', return_value=NOW))
        self.stack.enter_context(mock.patch.object(security.time, 'time', return_value=NOW))
        self.stack.enter_context(mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '0'}))
        security.save_users({'secret': 'synthetic-admin-' + 's' * 40, 'role_policy_version': 2,
            'users': [{'username': 'admin', 'role': 'admin', 'sv': 1, 'pwd_hash': 'no-login'},
                      {'username': 'reader', 'role': 'account_admin', 'sv': 1, 'pwd_hash': 'no-login'}]})
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.connect()
        self.addCleanup(self.close_db)
        self.app = FastAPI()
        self.app.include_router(portal.router)
        self.app.include_router(portal.admin_router)
        self.app.include_router(accounts.router)
        self.transport = httpx.ASGITransport(self.app, raise_app_exceptions=False)
        self.admin = {'cookie': f'{config.COOKIE_NAME}={security.issue_token("admin", "admin")}', 'origin': 'http://testserver'}
        self.a = self.add_user('alice')
        self.b = self.add_user('bob')

    def close_db(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None

    def add_user(self, name):
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_users(id,username,pwd_hash,created_at) VALUES(?,?,?,?)',
                         (name, name, security.make_hash('synthetic-password'), NOW))
        return store.user(name)

    def headers(self, who='alice'):
        user = store.user(who)
        token = security._sign(json.dumps({'id': who, 'sv': user['sv'], 'exp': NOW + 1000}), store.secret())
        return {'cookie': f'{portal.COOKIE}={token}', 'origin': 'http://testserver'}

    async def call(self, method, path, headers=None, **kw):
        async with httpx.AsyncClient(transport=self.transport, base_url='http://testserver') as client:
            return await client.request(method, path, headers=self.headers() if headers is None else headers, **kw)

    async def bind(self, user=None, uid='synthetic-uid', total=260, expiry=None):
        user = user or self.a
        expiry = expiry or NOW + 20 * 86400
        result = {'uid': uid, 'nickname': 'Synthetic Account', 'realm': 'cn', 'enterprise_id': '',
                  'access_token': 'synthetic-access-do-not-return', 'refresh_token': 'synthetic-refresh',
                  'expires_at': NOW + 3600}
        with mock.patch.object(tencent, 'fetch_credits', return_value=(True, total, 'ok', [{'at': expiry, 'amount': total}] if total else [])):
            out = await service.import_ready(user, result)
        return out, result

    async def test_initial_balance_has_original_expiry_and_never_duplicates(self):
        out, result = await self.bind()
        with mock.patch.object(tencent, 'fetch_credits', side_effect=AssertionError('must not fetch again')):
            await service.import_ready(self.a, result)
        overview = store.overview('alice')
        self.assertEqual(overview['balance_units'], 260 * store.SCALE)
        self.assertEqual(overview['grants'][0]['expires_at'], NOW + 20 * 86400)
        self.assertFalse(list(config.AUTH_DIR.glob('*.json')))

    async def test_cross_user_cannot_claim_or_read_credentials(self):
        out, result = await self.bind()
        with self.assertRaises(ValueError):
            await service.import_ready(self.b, result)
        response = await self.call('POST', f'/api/portal/accounts/{out["id"]}/checkin', headers=self.headers('bob'))
        self.assertEqual(response.status_code, 404)
        overview = await self.call('GET', '/api/portal/overview')
        self.assertNotIn('access_token', overview.text)
        self.assertNotIn('synthetic-access', overview.text)
        self.assertEqual(store.overview('bob')['account_count'], 0)

    async def test_missing_expiry_is_pending_not_free_credit(self):
        result = {'uid': 'pending-uid', 'nickname': 'pending', 'realm': 'cn', 'access_token': 'synthetic'}
        with mock.patch.object(tencent, 'fetch_credits', return_value=(True, 260, 'ok', [])):
            out = await service.import_ready(self.a, result)
        self.assertEqual(out['initial_state'], 'pending')
        self.assertEqual(store.overview('alice')['balance_units'], 0)
        with self.assertRaises(ValueError):
            await service.checkin(out['id'], 'user', 'alice')

    async def test_daily_credit_is_once_and_shared_with_admin(self):
        out, _ = await self.bind()
        initial_expiry = NOW + 20 * 86400
        snapshots = [(True, 260, 'ok', [{'at': initial_expiry, 'amount': 260}]),
                     (True, 360, 'ok', [{'at': initial_expiry, 'amount': 260}, {'at': NOW + 30 * 86400, 'amount': 100}])]
        with mock.patch.object(tencent, 'fetch_credits', side_effect=snapshots), \
                mock.patch.object(tencent, 'checkin', return_value=(0, 'ok')) as rpc:
            first = await self.call('POST', f'/api/portal-admin/accounts/{out["id"]}/checkin', headers=self.admin)
            self.assertEqual(first.status_code, 200, first.text)
            second = await self.call('POST', f'/api/portal/accounts/{out["id"]}/checkin')
            self.assertEqual(second.status_code, 200)
            self.assertEqual(rpc.call_count, 1)
        user_view = store.overview('alice')
        admin_view = await self.call('GET', '/api/portal-admin/users', headers=self.admin)
        self.assertEqual(user_view['today_checkin_units'], 100 * store.SCALE)
        self.assertEqual(user_view['accounts'][0]['checkin_state'], 'confirmed')
        self.assertIn('confirmed', admin_view.text)
        self.assertEqual(user_view['balance_units'], 360 * store.SCALE)

    async def test_concurrent_admin_user_only_one_rpc(self):
        out, _ = await self.bind(total=0)
        gate, entered = asyncio.Event(), asyncio.Event()
        async def rpc(*a):
            entered.set(); await gate.wait(); return 0, 'ok'
        with mock.patch.object(tencent, 'checkin', side_effect=rpc) as check, \
                mock.patch.object(tencent, 'fetch_credits', side_effect=[(True, 0, 'ok', []), (True, 100, 'ok', [{'at': NOW + 30 * 86400, 'amount': 100}])]):
            task = asyncio.create_task(service.checkin(out['id'], 'admin'))
            await asyncio.wait_for(entered.wait(), 2)
            second = await service.checkin(out['id'], 'user', 'alice')
            self.assertEqual(second['state'], 'running')
            gate.set(); await task
            self.assertEqual(check.call_count, 1)

    async def test_already_signed_updates_status_without_minting(self):
        out, _ = await self.bind(total=100)
        with mock.patch.object(tencent, 'checkin', return_value=(10001, 'already')), \
                mock.patch.object(tencent, 'fetch_credits', return_value=(True, 100, 'ok', [{'at': NOW + 20 * 86400, 'amount': 100}])):
            result = await service.checkin(out['id'], 'user', 'alice')
        self.assertEqual(result['state'], 'confirmed_uncredited')
        self.assertEqual(store.overview('alice')['balance_units'], 100 * store.SCALE)

    async def test_expiry_only_removes_remaining_of_that_batch(self):
        out, _ = await self.bind(total=100, expiry=NOW + 100)
        with store.transaction() as conn:
            conn.execute('UPDATE portal_grants SET remaining_units=?', (30 * store.SCALE,))
            account = store.require_account(conn, out['id'])
            store._grant(conn, account, 'checkin', 'next-day', 100 * store.SCALE, NOW + 10, NOW + 200)
        with mock.patch.object(store, 'now', return_value=NOW + 101):
            first = store.overview('alice'); second = store.overview('alice')
        self.assertEqual(first['balance_units'], 100 * store.SCALE)
        self.assertEqual(sum(row['kind'] == 'expired' for row in second['events']), 1)
        self.assertEqual(next(row['amount_units'] for row in first['events'] if row['kind'] == 'expired'), -30 * store.SCALE)

    async def test_unknown_result_never_retries_or_grants(self):
        out, _ = await self.bind(total=0)
        with mock.patch.object(tencent, 'checkin', return_value=(-1, 'timeout')) as rpc, \
                mock.patch.object(tencent, 'fetch_credits', return_value=(True, 0, 'ok', [])):
            await service.checkin(out['id'], 'user', 'alice')
            result = await service.checkin(out['id'], 'admin')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(store.overview('alice')['balance_units'], 0)
        with mock.patch.object(store, 'now', return_value=NOW + 86400):
            with self.assertRaises(ValueError):
                store.claim_checkin(out['id'], 'automatic-next-day')

    async def test_confirmed_signin_balance_failure_can_reconcile_without_rpc(self):
        out, _ = await self.bind(total=0)
        with mock.patch.object(tencent, 'checkin', return_value=(0, 'ok')) as rpc, \
                mock.patch.object(tencent, 'fetch_credits', side_effect=[(True, 0, 'ok', []), (False, None, 'unavailable', [])]):
            result = await service.checkin(out['id'], 'admin')
            self.assertEqual(result['state'], 'confirmed_uncredited')
            self.assertEqual(rpc.call_count, 1)
        with mock.patch.object(tencent, 'checkin', side_effect=AssertionError('never repeat RPC')), \
                mock.patch.object(tencent, 'fetch_credits', return_value=(True, 100, 'ok', [{'at': NOW + 30 * 86400, 'amount': 100}])):
            await service.reconcile_checkin(out['id'], 'alice')
        self.assertEqual(store.overview('alice')['today_checkin_units'], 100 * store.SCALE)

    async def test_automatic_is_opt_in_and_shares_user_receipt(self):
        out, _ = await self.bind(total=0)
        with mock.patch.object(tencent, 'checkin', side_effect=AssertionError('disabled by default')):
            self.assertEqual(await service.automatic_once(), 0)
        response = await self.call('PUT', '/api/portal-admin/automatic-checkin', headers=self.admin, json={'enabled': True})
        self.assertEqual(response.status_code, 200)
        with mock.patch.object(tencent, 'checkin', return_value=(0, 'ok')) as rpc, \
                mock.patch.object(tencent, 'fetch_credits', side_effect=[(True, 0, 'ok', []), (True, 100, 'ok', [{'at': NOW + 30 * 86400, 'amount': 100}])]):
            self.assertEqual(await service.automatic_once(), 1)
            await service.checkin(out['id'], 'user', 'alice')
            self.assertEqual(rpc.call_count, 1)

    async def test_login_success_and_admin_password_reset_revoke_portal_only(self):
        response = await self.call('POST', '/api/portal/login', headers={'origin': 'http://testserver'},
                                   json={'username': 'alice', 'password': 'synthetic-password'})
        self.assertEqual(response.status_code, 200, response.text)
        cookie = response.cookies.get(portal.COOKIE)
        self.assertTrue(cookie)
        headers = {'cookie': f'{portal.COOKIE}={cookie}', 'origin': 'http://testserver'}
        self.assertEqual((await self.call('GET', '/api/portal/me', headers=headers)).status_code, 200)
        self.assertEqual((await self.call('PATCH', '/api/portal-admin/users/alice', headers=self.admin,
                                         json={'password': 'changed-password-123'})).status_code, 200)
        self.assertEqual((await self.call('GET', '/api/portal/me', headers=headers)).status_code, 401)
        self.assertEqual((await self.call('GET', '/api/portal-admin/users', headers=self.admin)).status_code, 200)

    async def test_existing_administrator_pool_is_not_claimed(self):
        config.AUTH_DIR.mkdir()
        existing = config.AUTH_DIR / 'workbuddy-synthetic-uid.json'
        existing.write_bytes(b'synthetic-existing-owner')
        with self.assertRaises(ValueError):
            await self.bind()
        self.assertEqual(existing.read_bytes(), b'synthetic-existing-owner')
        self.assertEqual(store.overview('alice')['account_count'], 0)

    def test_concurrent_account_reservation_has_single_owner(self):
        def reserve(owner):
            try:
                return store.reserve_account(owner, 'contended-account', 'synthetic')['owner_id']
            except ValueError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, ['alice', 'bob']))
        self.assertEqual(results.count('rejected'), 1)

    async def test_provider_duration_does_not_extend_daily_grant(self):
        out, _ = await self.bind(total=0)
        with mock.patch.object(tencent, 'checkin', return_value=(0, 'ok')), \
                mock.patch.object(tencent, 'fetch_credits', side_effect=[(True, 0, 'ok', []), (True, 100, 'ok', [{'at': NOW + 31 * 86400, 'amount': 100}])]):
            await service.checkin(out['id'], 'user', 'alice')
        self.assertEqual(store.overview('alice')['grants'][0]['expires_at'], NOW + 30 * 86400)

    async def test_revoked_owner_cannot_commit_initial_after_provider_await(self):
        result = {'uid': 'revoke-at-balance', 'nickname': 'Synthetic', 'realm': 'cn', 'access_token': 'synthetic'}
        async def revoked(auth):
            with store.transaction() as conn:
                conn.execute("UPDATE portal_users SET sv=sv+1 WHERE id='alice'")
            return True, 100, 'ok', [{'at': NOW + 30 * 86400, 'amount': 100}]
        with mock.patch.object(tencent, 'fetch_credits', side_effect=revoked):
            with self.assertRaises(HTTPException):
                await service.import_ready(self.a, result)
        self.assertEqual(store.overview('alice')['balance_units'], 0)

    async def test_cancelled_scan_does_not_call_provider_again(self):
        with mock.patch.object(tencent, 'start_login', return_value={'state': 'synthetic', 'authUrl': 'https://example.invalid'}):
            response = await self.call('POST', '/api/portal/auth/start')
        session = response.json()['session']
        self.assertEqual((await self.call('POST', '/api/portal/auth/cancel', json={'session': session})).status_code, 200)
        with mock.patch.object(tencent, 'poll_login', side_effect=AssertionError('cancelled')):
            response = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
        self.assertEqual(response.json()['state'], 'expired')

    async def test_expired_token_refresh_is_not_fixed_by_guessing(self):
        out, result = await self.bind(total=0)
        with store.transaction() as conn:
            account = dict(store.require_account(conn, out['id']))
        service.save_credentials(account, {**result, 'expires_at': NOW - 1})
        with mock.patch.object(tencent, 'refresh_token', return_value=(False, 'failed', {})), \
                mock.patch.object(tencent, 'checkin', side_effect=AssertionError('no checkin with expired auth')):
            with self.assertRaises(ValueError):
                await service.checkin(out['id'], 'user', 'alice')
        self.assertEqual(store.overview('alice')['balance_units'], 0)

    async def test_balance_reallocation_without_net_growth_does_not_mint(self):
        out, _ = await self.bind(total=100)
        with mock.patch.object(tencent, 'checkin', return_value=(0, 'ok')), \
                mock.patch.object(tencent, 'fetch_credits', side_effect=[
                    (True, 100, 'ok', [{'at': NOW + 20 * 86400, 'amount': 100}]),
                    (True, 100, 'ok', [{'at': NOW + 30 * 86400, 'amount': 100}])]):
            result = await service.checkin(out['id'], 'user', 'alice')
        self.assertEqual(result['credited_units'], 0)
        self.assertEqual(store.overview('alice')['balance_units'], 100 * store.SCALE)

    async def test_main_gateway_schema_and_existing_key_are_untouched(self):
        before = [tuple(row) for row in db.query("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")]
        original_version = db.query_one('PRAGMA user_version')[0]
        await self.bind()
        after = [tuple(row) for row in db.query("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")]
        self.assertEqual(before, after)
        self.assertEqual(db.query_one('PRAGMA user_version')[0], original_version)

    async def test_two_day_grants_are_independently_expiring(self):
        out, _ = await self.bind(total=0)
        store.claim_checkin(out['id'], 'user')
        store.finish_checkin(out['id'], store.day(), 0, {}, {NOW + 30 * 86400:100 * store.SCALE}, NOW)
        with mock.patch.object(store, 'now', return_value=NOW + 86400):
            store.claim_checkin(out['id'], 'admin')
            store.finish_checkin(out['id'], store.day(), 0, {NOW + 30 * 86400:100 * store.SCALE},
                {NOW + 30 * 86400:100 * store.SCALE, NOW + 31 * 86400:100 * store.SCALE}, NOW + 86400)
        with mock.patch.object(store, 'now', return_value=NOW + 30 * 86400 + 1):
            overview = store.overview('alice')
        self.assertEqual(overview['balance_units'], 100 * store.SCALE)
        self.assertEqual(overview['grants'][0]['expires_at'], NOW + 31 * 86400)

    async def test_admin_and_customer_sessions_are_not_interchangeable(self):
        response = await self.call('GET', '/api/portal/me', headers=self.admin)
        self.assertEqual(response.status_code, 401)
        response = await self.call('GET', '/api/portal-admin/users')
        self.assertEqual(response.status_code, 401)
        response = await self.call('GET', '/api/accounts')
        self.assertEqual(response.status_code, 401)

    async def test_login_origin_failed_counter_and_revocation(self):
        bad = await self.call('POST', '/api/portal/login', headers={'origin': 'http://other.invalid'}, json={'username': 'alice', 'password': 'synthetic-password'})
        self.assertEqual(bad.status_code, 403)
        for _ in range(5):
            response = await self.call('POST', '/api/portal/login', headers={'origin': 'http://testserver'}, json={'username': 'alice', 'password': 'wrong'})
            self.assertEqual(response.status_code, 401)
        response = await self.call('POST', '/api/portal/login', headers={'origin': 'http://testserver'}, json={'username': 'alice', 'password': 'synthetic-password'})
        self.assertEqual(response.status_code, 429)
        headers = self.headers()
        response = await self.call('PATCH', '/api/portal-admin/users/alice', headers=self.admin, json={'enabled': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.call('GET', '/api/portal/me', headers=headers)).status_code, 401)

    async def test_admin_creates_normal_user_not_manager(self):
        response = await self.call('POST', '/api/portal-admin/users', headers=self.admin, json={'username': 'new-user', 'password': 'new-password-123'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('pwd_hash', response.text)
        self.assertFalse(any(row['username'] == 'new-user' for row in security.load_users()['users']))

    async def test_requests_are_empty_without_explicit_key_ownership(self):
        response = await self.call('GET', '/api/portal/requests')
        self.assertEqual(response.json()['items'], [])
        self.assertFalse(response.json()['dispatch_enabled'])

    async def test_request_list_filters_explicit_key_hash_and_never_matches_names(self):
        from server import keysvc
        a = keysvc.create_key('synthetic-alice')
        b = keysvc.create_key('synthetic-alice')
        row = db.query_one('SELECT key_hash FROM api_keys WHERE id=?', (a['id'],))
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_key_links VALUES(?,?,?)', (a['id'], 'alice', row['key_hash']))
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,prompt_tokens,completion_tokens,latency_ms,credit) VALUES(?,?,?,?,?,?,?,?)',
                   (NOW, a['id'], 'own-model', 200, 5, 2, 90, None))
        db.execute('INSERT INTO request_logs(ts,key_id,model,status) VALUES(?,?,?,?)', (NOW, b['id'], 'other-model', 200))
        response = await self.call('GET', '/api/portal/requests')
        self.assertEqual(response.json()['total'], 1)
        self.assertEqual(response.json()['items'][0]['model'], 'own-model')
        self.assertIsNone(response.json()['items'][0]['credit'])
        self.assertNotIn('other-model', response.text)
        db.execute('UPDATE api_keys SET key_hash=? WHERE id=?', ('new-synthetic-generation', a['id']))
        self.assertEqual((await self.call('GET', '/api/portal/requests')).json()['total'], 0)

    async def test_scanning_does_not_checkin_or_reload_pool(self):
        ready = {'status': 'ready', 'uid': 'qr-uid', 'nickname': 'Synthetic QR', 'realm': 'cn',
                 'access_token': 'synthetic-secret', 'expires_at': NOW + 3600}
        with mock.patch.object(tencent, 'start_login', return_value={'state': 'synthetic-provider-state', 'authUrl': 'https://example.invalid/authorize'}), \
                mock.patch.object(tencent, 'poll_login', return_value=ready), \
                mock.patch.object(tencent, 'fetch_credits', return_value=(True, 0, 'ok', [])), \
                mock.patch.object(tencent, 'checkin', side_effect=AssertionError('no checkin during initial balance')):
            start = await self.call('POST', '/api/portal/auth/start')
            self.assertEqual(start.status_code, 200, start.text)
            session = start.json()['session']
            denied = await self.call('POST', '/api/portal/auth/poll', headers=self.headers('bob'), json={'session': session})
            self.assertEqual(denied.status_code, 404)
            poll = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
            self.assertEqual(poll.json()['state'], 'complete', poll.text)
            self.assertNotIn('synthetic-secret', poll.text)
            again = await self.call('POST', '/api/portal/auth/poll', json={'session': session})
            self.assertEqual(again.json()['state'], 'complete')
        self.assertEqual(store.overview('alice')['account_count'], 1)
        self.assertFalse(config.AUTH_DIR.exists())

    def test_strict_amounts_and_expiry(self):
        for value in (True, float('nan'), float('inf'), -1, 0.0000001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store.units(value)
        with self.assertRaises(ValueError):
            store.snapshot(True, 100, [{'at': NOW - 1, 'amount': 100}])


if __name__ == '__main__':
    unittest.main()
