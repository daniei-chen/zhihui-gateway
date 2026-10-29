"""Authorization ownership is fenced by the current human account generation."""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from server import config, db, edgeidentity, security
from server.routers import accounts, auth
from server.services import platformauth
from server.tests.authorization_test_support import install_users, owner


READY = {'status': 'ready', 'uid': 'synthetic-owner-account', 'nickname': 'Synthetic',
         'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh',
         'realm': 'cn', 'expires_at': 9999999999}


class AuthorizationGenerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.original_conn = db._conn
        db._conn = None
        self.addCleanup(self.close_db)
        for name, value in [('DATA_DIR', root), ('DB_PATH', root / 'generation.db'),
                            ('USERS_FILE', root / 'users.json'), ('AUTH_DIR', root / 'auths')]:
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        self.cfg = install_users('alice')
        self.user = owner('alice')
        self.qoder = SimpleNamespace(
            oauth_start=mock.AsyncMock(return_value={
                'state': 'synthetic-engine-state',
                'authUrl': 'https://qoder.com.cn/device/selectAccounts?fixture=1'}),
            oauth_poll=mock.AsyncMock(return_value={'status': 'pending'}))
        self.request = Request({'type': 'http', 'method': 'POST', 'path': '/api/auth/start',
                                'scheme': 'http', 'server': ('testserver', 80),
                                'headers': [], 'query_string': b'',
                                'app': SimpleNamespace(state=SimpleNamespace(
                                    rt=SimpleNamespace(qoder=self.qoder)))})
        for name in ('_login_scopes', '_login_bindings', '_login_locks', '_login_ready',
                     '_login_polls', '_login_tasks', '_login_owner', '_login_regions',
                     '_provisioned_states'):
            getattr(accounts, name).clear()
            self.addCleanup(getattr(accounts, name).clear)
        for obj, name, kwargs in [
            (security, 'audit', {'return_value': None}),
            (accounts, '_group', {'return_value': {'id': 0, 'is_default': True,
                                                  'auth_dir': str(config.AUTH_DIR)}}),
            (accounts, '_schedule_revive_after_login', {'return_value': None}),
            (accounts.reload, 'request_reload_or_restart', {'return_value': None}),
        ]:
            patch = mock.patch.object(obj, name, **kwargs)
            patch.start(); self.addCleanup(patch.stop)

    def close_db(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self.original_conn

    def mutate_owner(self, action):
        cfg = security.load_users()
        row = next(row for row in cfg['users'] if row['username'] == 'alice')
        if action == 'delete':
            cfg['users'].remove(row)
        elif action == 'recreate':
            cfg['users'].remove(row)
            cfg['users'].append({**row, 'pwd_hash': security.make_hash('new-fixture-password')})
        elif action == 'sv':
            row['sv'] += 1
        elif action == 'password':
            row['pwd_hash'] = security.make_hash('changed-fixture-password')
        elif action == 'role':
            row['role'] = 'account_admin'
        elif action == 'secret':
            cfg['secret'] = 'rotated-synthetic-test-secret'
        security.save_users(cfg)

    async def start(self):
        return await platformauth.start(self.request, self.user, 'qoder', 'cn', None, {})

    async def wb_poll(self, state):
        return await accounts.auth_poll(state=state, realm='cn', region=None,
                                        upstream_id=None, user=self.user, request=None)

    def test_every_identity_change_rejects_old_owner_without_provider_effect(self):
        for action in ('delete', 'recreate', 'sv', 'password', 'role'):
            with self.subTest(action=action):
                security.save_users(self.cfg)
                self.user = owner('alice')
                self.qoder.oauth_start.reset_mock()
                self.mutate_owner(action)
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(self.start())
                self.assertEqual(caught.exception.status_code, 401)
                self.qoder.oauth_start.assert_not_awaited()

    def test_missing_or_invalid_generation_binding_cannot_capture_current_identity(self):
        for binding in (None, '', 0, [], {}, 'wrong-generation'):
            with self.subTest(binding=binding):
                self.user = {'username': 'alice', 'role': 'admin'}
                if binding is not None:
                    self.user['_authorization_binding'] = binding
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(self.start())
                self.assertEqual(caught.exception.status_code, 401)
        self.qoder.oauth_start.assert_not_awaited()

    def test_fresh_login_cannot_inherit_previous_generation_state(self):
        state = asyncio.run(self.start())['state']
        self.mutate_owner('sv')
        current = owner('alice')
        for operation in ('poll', 'cancel', 'review'):
            with self.subTest(operation=operation):
                with self.assertRaises(HTTPException) as caught:
                    if operation == 'poll':
                        asyncio.run(platformauth.poll(self.request, current, 'qoder', 'cn', state, None))
                    elif operation == 'cancel':
                        asyncio.run(platformauth.cancel(self.request, current, 'qoder', 'cn', state))
                    else:
                        platformauth.review(self.request, current, 'qoder', 'cn', state)
                self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(platformauth.review(self.request, current, 'qoder', 'cn')['sessions'], [])
        self.qoder.oauth_poll.assert_not_awaited()

    def test_revocation_during_committed_provider_poll_preserves_truthful_receipt(self):
        state = asyncio.run(self.start())['state']

        async def completed(_state):
            self.mutate_owner('sv')
            return {'status': 'ok', 'account': {'uid': 'fixture-qoder', 'realm': 'cn'}}

        self.qoder.oauth_poll.side_effect = completed
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(platformauth.poll(self.request, self.user, 'qoder', 'cn', state, None))
        self.assertEqual(caught.exception.status_code, 401)
        evidence = platformauth._read(state)
        self.assertEqual(evidence['status'], 'success')
        self.assertEqual(evidence['result']['uid'], 'fixture-qoder')
        self.qoder.oauth_poll.assert_awaited_once()
        with self.assertRaises(HTTPException):
            asyncio.run(platformauth.poll(self.request, self.user, 'qoder', 'cn', state, None))
        self.qoder.oauth_poll.assert_awaited_once()

    def test_background_revocation_during_poll_stops_next_effect_and_local_write(self):
        accounts._bind_login('fixture-wb', self.user, 'cn', None)

        async def ready(_state, _realm):
            self.mutate_owner('delete')
            return dict(READY)

        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(side_effect=ready)) as poll, \
                mock.patch.object(accounts.tencent, 'checkin', mock.AsyncMock()) as checkin, \
                mock.patch.object(accounts.tencent, 'write_auth_file') as write:
            asyncio.run(accounts._poll_login_background('fixture-wb', 'cn', None, None, self.user))
        poll.assert_awaited_once()
        checkin.assert_not_awaited()
        write.assert_not_called()
        self.assertNotIn('fixture-wb', accounts._login_polls)
        self.assertEqual(list(config.AUTH_DIR.glob('*.json')), [])

    def test_revocation_during_checkin_prevents_local_credential_commit(self):
        accounts._bind_login('fixture-checkin', self.user, 'cn', None)

        async def checkin(_probe, _realm):
            self.mutate_owner('sv')
            return 0, 'fixture-ok'

        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(return_value=dict(READY))), \
                mock.patch.object(accounts.tencent, 'checkin', mock.AsyncMock(side_effect=checkin)) as sent, \
                mock.patch.object(accounts.tencent, 'write_auth_file') as write, \
                mock.patch.object(db, 'add_checkin_log') as log:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.wb_poll('fixture-checkin'))
        self.assertEqual(caught.exception.status_code, 401)
        sent.assert_awaited_once()
        log.assert_not_called()
        write.assert_not_called()

    def test_stale_owner_cannot_use_cached_ready_to_write(self):
        accounts._bind_login('fixture-cached', self.user, 'cn', None)
        accounts._login_ready['fixture-cached'] = (dict(READY), 'cn', '', 0)
        self.mutate_owner('sv')
        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock()) as poll, \
                mock.patch.object(accounts.tencent, 'write_auth_file') as write:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.wb_poll('fixture-cached'))
        self.assertEqual(caught.exception.status_code, 401)
        poll.assert_not_awaited()
        write.assert_not_called()

    def test_fresh_login_cannot_read_previous_workbuddy_generation(self):
        accounts._bind_login('fixture-old-generation', self.user, 'cn', None)
        accounts._login_polls['fixture-old-generation'] = {'status': 'success', 'uid': 'old-account'}
        self.mutate_owner('sv')
        self.user = owner('alice')
        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock()) as poll:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.wb_poll('fixture-old-generation'))
        self.assertEqual(caught.exception.status_code, 404)
        poll.assert_not_awaited()

    def test_revoked_auto_revive_stops_before_provider_write(self):
        async def disabled(_uid, **_kwargs):
            self.mutate_owner('sv')
            return True

        with mock.patch.object(accounts.wb2api, 'account_disabled',
                               mock.AsyncMock(side_effect=disabled)) as status, \
                mock.patch.object(accounts.wb2api, 'revive_account', mock.AsyncMock()) as revive:
            asyncio.run(accounts._revive_after_login('fixture-revive', {'base_url': 'http://fixture'},
                                                     owner=self.user))
        status.assert_awaited_once()
        revive.assert_not_awaited()

    def test_unowned_auto_revive_has_no_provider_effect(self):
        with mock.patch.object(accounts.wb2api, 'account_disabled', mock.AsyncMock()) as status, \
                mock.patch.object(accounts.wb2api, 'revive_account', mock.AsyncMock()) as revive:
            asyncio.run(accounts._revive_after_login('fixture-revive', {'base_url': 'http://fixture'}))
        status.assert_not_awaited()
        revive.assert_not_awaited()

    def test_parallel_workbuddy_pollers_commit_once(self):
        accounts._bind_login('fixture-parallel', self.user, 'cn', None)

        async def ready(_state, _realm):
            await asyncio.sleep(0.01)
            return dict(READY)

        async def scenario():
            return await asyncio.gather(self.wb_poll('fixture-parallel'), self.wb_poll('fixture-parallel'))

        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(side_effect=ready)) as poll, \
                mock.patch.object(accounts.tencent, 'checkin', mock.AsyncMock(return_value=(0, 'ok'))) as checkin, \
                mock.patch.object(accounts.tencent, 'write_auth_file',
                                  return_value=('workbuddy-fixture.json', False)) as write:
            results = asyncio.run(scenario())
        self.assertEqual([result['status'] for result in results], ['success', 'success'])
        poll.assert_awaited_once()
        checkin.assert_awaited_once()
        write.assert_called_once()

    def test_cancel_and_new_start_do_not_interrupt_inflight_provider_operation(self):
        state = 'fixture-inflight'
        accounts._bind_login(state, self.user, 'cn', None)
        accounts._login_owner[state] = accounts._owner_key(self.user, None)

        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def pending(_state, _realm):
                entered.set()
                await release.wait()
                return {'status': 'waiting'}

            with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(side_effect=pending)) as poll:
                task = asyncio.create_task(self.wb_poll(state))
                accounts._login_tasks[state] = task
                await asyncio.wait_for(entered.wait(), timeout=1)
                with self.assertRaises(HTTPException) as caught:
                    await accounts.auth_cancel(self.request, {'state': state, 'realm': 'cn'}, user=self.user)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertIn(state, accounts._login_scopes)
                accounts._cancel_other_login_polls('fixture-new-state', accounts._owner_key(self.user, None))
                self.assertIs(accounts._login_tasks[state], task)
                self.assertFalse(task.cancelled())
                release.set()
                self.assertEqual((await task)['status'], 'waiting')
                poll.assert_awaited_once()
                accounts._login_tasks.pop(state)

        asyncio.run(scenario())

    def test_missing_user_store_never_bootstraps_during_background_authorization(self):
        config.USERS_FILE.unlink()
        with mock.patch.object(security, 'bootstrap_users', side_effect=AssertionError('no bootstrap')) as bootstrap:
            with self.assertRaises(HTTPException) as caught:
                security.authorization_owner(self.user)
        self.assertEqual(caught.exception.status_code, 401)
        bootstrap.assert_not_called()
        self.assertFalse(config.USERS_FILE.exists())

    def test_login_rejects_identity_changes_during_password_verification(self):
        app = FastAPI()
        app.include_router(auth.router)
        verified = security.verify_pwd
        for action in ('secret', 'role', 'sv', 'password', 'delete', 'recreate'):
            with self.subTest(action=action):
                security.save_users(self.cfg)

                def verify_then_change(password, password_hash):
                    accepted = verified(password, password_hash)
                    self.assertTrue(accepted)
                    self.mutate_owner(action)
                    return accepted

                with TestClient(app) as client, \
                        mock.patch.object(security, 'verify_pwd', side_effect=verify_then_change):
                    response = client.post('/api/login', json={
                        'username': 'alice', 'password': 'synthetic-password'})
                self.assertEqual(response.status_code, 401, response.text)
                self.assertNotIn('set-cookie', response.headers)

    def test_internal_owner_binding_is_not_exposed_by_session_identity(self):
        app = FastAPI()
        app.include_router(auth.router)
        with TestClient(app) as client:
            response = client.post('/api/login', json={
                'username': 'alice', 'password': 'synthetic-password'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {'ok': True, 'username': 'alice', 'role': 'admin'})
            identity = client.get('/api/me')
            self.assertEqual(identity.status_code, 200, identity.text)
            self.assertEqual(identity.json(), {'username': 'alice', 'role': 'admin'})
            with mock.patch.object(edgeidentity, '_secret', return_value='synthetic-bridge-secret-' * 2):
                probe = client.get('/api/authz/probe')
            self.assertEqual(probe.status_code, 200, probe.text)
            payload = probe.headers['x-qtg-identity'].split('.')[1]
            assertion = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            self.assertEqual(assertion['sub'], 'alice')
            self.assertNotIn('_authorization_binding', assertion)
            for response in (response, identity, probe):
                self.assertNotIn('_authorization_binding', response.text)
                self.assertNotIn(self.user['_authorization_binding'], response.text)

    def test_revoke_between_tencent_token_and_account_http_stops_second_request(self):
        state = 'fixture-token-boundary'
        accounts._bind_login(state, self.user, 'cn', None)
        accounts.tencent._state_cache[state] = (time.time(), 'cn')
        self.addCleanup(accounts.tencent.drop_state, state)
        seen = []

        async def response(request):
            seen.append(request.url.path)
            self.assertTrue(request.url.path.endswith('/auth/token'))
            self.mutate_owner('sv')
            return httpx.Response(200, json={'code': 0, 'data': {
                'accessToken': 'fixture-access', 'refreshToken': 'fixture-refresh'}})

        with mock.patch.object(accounts.tencent, '_account_http_client', side_effect=lambda *_a, **_k:
                               httpx.AsyncClient(transport=httpx.MockTransport(response))), \
                mock.patch.object(accounts.tencent, 'checkin', mock.AsyncMock()) as checkin, \
                mock.patch.object(accounts.tencent, 'write_auth_file') as write:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.wb_poll(state))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(seen, ['/v2/plugin/auth/token'])
        checkin.assert_not_awaited()
        write.assert_not_called()

    def test_revoke_between_region_lookup_and_mutation_stops_second_http(self):
        state = 'fixture-region-boundary'
        accounts._bind_login(state, self.user, 'global', None)
        seen = []

        async def response(request):
            seen.append(request.url.path)
            self.assertTrue(request.url.path.endswith('/get-country-code'))
            self.mutate_owner('sv')
            return httpx.Response(200, json={'code': 0, 'data': {'data': {'list': [
                {'IOS2': 'HK', 'EnName': 'Hong Kong', 'Code': '810000'}]}}})

        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(return_value={**READY, 'realm': 'global'})), \
                mock.patch.object(accounts.tencent, '_account_http_client', side_effect=lambda *_a, **_k:
                                  httpx.AsyncClient(transport=httpx.MockTransport(response))), \
                mock.patch.object(accounts.tencent, 'claim_trial', mock.AsyncMock()) as trial, \
                mock.patch.object(accounts.tencent, 'write_auth_file') as write:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(accounts.auth_poll(state=state, realm='global', region='HK',
                                               upstream_id=None, user=self.user, request=None))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(seen, ['/billing/area/get-country-code'])
        trial.assert_not_awaited()
        write.assert_not_called()

    def test_scoped_http_auth_failure_propagates_without_helper_error_conversion(self):
        async def operation(name):
            auth = {**READY, 'realm': 'global'}
            return await {
                'start': lambda: accounts.tencent.start_login('cn'),
                'poll': lambda: accounts.tencent.poll_login('fixture-scope', 'cn'),
                'checkin': lambda: accounts.tencent.checkin(READY, 'cn'),
                'registration': lambda: accounts.tencent.registration_status(auth),
                'submit_region': lambda: accounts.tencent.submit_region(auth, 'HK'),
                'region_fields': lambda: accounts.tencent._region_fields('HK', auth),
                'trial': lambda: accounts.tencent.claim_trial(auth),
            }[name]()

        def revoked():
            raise HTTPException(status_code=401, detail='synthetic revoked')

        accounts.tencent._state_cache['fixture-scope'] = (time.time(), 'cn')
        self.addCleanup(accounts.tencent.drop_state, 'fixture-scope')
        transport = mock.Mock(side_effect=AssertionError('no request after revoked'))
        with mock.patch.object(accounts.tencent, '_account_http_client', side_effect=lambda *_a, **_k:
                               httpx.AsyncClient(transport=httpx.MockTransport(transport))):
            for name in ('start', 'poll', 'checkin', 'registration', 'submit_region', 'region_fields', 'trial'):
                with self.subTest(name=name):
                    with accounts.tencent.authorization_scope(revoked):
                        with self.assertRaises(HTTPException) as caught:
                            asyncio.run(operation(name))
                    self.assertEqual(caught.exception.status_code, 401)
        transport.assert_not_called()

    def test_authorization_scope_is_nested_task_local_and_resets_on_exception(self):
        checks = []

        async def scenario():
            ready = asyncio.Event()
            release = asyncio.Event()

            async def scoped():
                with accounts.tencent.authorization_scope(lambda: checks.append('scoped')):
                    ready.set()
                    await release.wait()
                    accounts.tencent._check_authorization()

            task = asyncio.create_task(scoped())
            await ready.wait()
            accounts.tencent._check_authorization()
            self.assertEqual(checks, [])
            release.set()
            await task

        asyncio.run(scenario())
        self.assertEqual(checks, ['scoped'])
        with accounts.tencent.authorization_scope(lambda: checks.append('outer')):
            with self.assertRaisesRegex(ValueError, 'fixture'):
                with accounts.tencent.authorization_scope(lambda: checks.append('inner')):
                    accounts.tencent._check_authorization()
                    raise ValueError('fixture')
            accounts.tencent._check_authorization()
        accounts.tencent._check_authorization()
        self.assertEqual(checks, ['scoped', 'inner', 'outer'])

    def test_cancel_clears_all_session_maps_and_private_credential_memo(self):
        state = 'fixture-discard'
        accounts._bind_login(state, self.user, 'cn', None)
        accounts._login_locks[state] = asyncio.Lock()
        accounts._login_ready[state] = (dict(READY), 'cn', '', time.time())
        accounts._login_polls[state] = {'status': 'waiting', '_at': time.time()}
        accounts._login_regions[state] = 'HK'
        accounts._login_owner[state] = accounts._owner_key(self.user, None)
        accounts._provisioned_states[state] = time.time()
        accounts.tencent._state_cache[state] = (time.time(), 'cn')

        async def scenario():
            task = asyncio.create_task(asyncio.Event().wait())
            accounts._login_tasks[state] = task
            result = await accounts.auth_cancel(self.request, {'state': state, 'realm': 'cn'}, user=self.user)
            self.assertEqual(result, {'ok': True})
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        accounts._prune_login_polls()
        for name in ('_login_scopes', '_login_bindings', '_login_locks', '_login_ready',
                     '_login_polls', '_login_tasks', '_login_owner', '_login_regions', '_provisioned_states'):
            self.assertNotIn(state, getattr(accounts, name), name)
        self.assertNotIn(state, accounts.tencent._state_cache)

    def test_expired_session_prune_defers_busy_provider_then_cleans_after_release(self):
        state = 'fixture-expired-busy'
        accounts._bind_login(state, self.user, 'cn', None)
        scope = accounts._login_scopes[state]
        accounts._login_scopes[state] = (*scope[:3], time.time() - accounts._LOGIN_RESULT_TTL - 1)
        accounts._login_ready[state] = (dict(READY), 'cn', '', time.time() - accounts._LOGIN_RESULT_TTL - 1)
        accounts.tencent._state_cache[state] = (time.time(), 'cn')
        lock = asyncio.Lock()
        accounts._login_locks[state] = lock

        async def scenario():
            async with lock:
                accounts._prune_login_polls()
                self.assertIn(state, accounts._login_scopes)
                self.assertIn(state, accounts._login_ready)
                self.assertIn(state, accounts.tencent._state_cache)
            accounts._prune_login_polls()

        asyncio.run(scenario())
        for name in ('_login_scopes', '_login_bindings', '_login_locks', '_login_ready'):
            self.assertNotIn(state, getattr(accounts, name), name)
        self.assertNotIn(state, accounts.tencent._state_cache)
