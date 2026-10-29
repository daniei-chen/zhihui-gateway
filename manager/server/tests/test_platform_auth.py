"""Platform/realm authorization contracts. All providers and accounts are synthetic."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode, urlsplit, unquote

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, db, security
from server.routers import accounts
from server.services import platformauth
from server.tests.authorization_test_support import install_users, owner


REDIRECT = 'http://127.0.0.1:17388/authorize'
TRACE = 'a' * 32


def trae_start():
    return {'state': TRACE, 'redirect_uri': REDIRECT,
            'url': 'https://www.trae.cn/authorization?' + urlencode({
                'login_trace_id': TRACE, 'auth_callback_url': REDIRECT,
                'code_challenge': 'x' * 43, 'code_challenge_method': 'S256'})}


def callback(**overrides):
    return REDIRECT + '?' + urlencode({
        'loginTraceID': TRACE, 'host': 'https://api.trae.com.cn',
        'authCodeInfo': json.dumps({'AuthCode': 'synthetic-one-use-code'}),
        **overrides})


class PlatformAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original_conn = db._conn
        db._conn = None
        for name, value in [('DB_PATH', Path(self.tmp.name) / 'auth.db'),
                            ('USERS_FILE', Path(self.tmp.name) / 'users.json'),
                            ('DATA_DIR', Path(self.tmp.name))]:
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        self.addCleanup(self.close_db)
        install_users('alice', 'bob')
        self.user = owner('alice')
        self.qoder = SimpleNamespace(
            oauth_start=mock.AsyncMock(side_effect=lambda r: {
                'state': 'engine-' + r, 'realm': r,
                'authUrl': ('https://qoder.com.cn' if r == 'cn' else 'https://qoder.com')
                           + '/device/selectAccounts?challenge=fixture'}),
            oauth_poll=mock.AsyncMock(return_value={'status': 'pending'}),
            oauth_cancel=mock.AsyncMock(return_value={'cancelled': True}))
        self.trae = SimpleNamespace(
            oauth_login_url=mock.AsyncMock(side_effect=trae_start),
            oauth_finish=mock.AsyncMock(return_value={
                'user_id': 'fixture-trae', 'name': 'Synthetic Trae',
                'jwt': 'MUST-NOT-LEAK', 'refresh_token': 'MUST-NOT-LEAK'}))
        app = FastAPI()
        app.include_router(accounts.router)
        app.state.rt = SimpleNamespace(qoder=self.qoder, trae=self.trae)
        app.dependency_overrides[security.current_user] = lambda: self.user
        self.app = app
        self.client = TestClient(app)
        for obj, attr, kwargs in [
            (accounts, '_group', {'return_value': {'id': 1}}),
            (accounts.tencent, 'start_login', {'new': mock.AsyncMock(return_value={
                'state': '', 'realm': 'cn', 'authUrl': 'https://copilot.tencent.com/'})}),
            (security, 'audit', {'return_value': None}),
        ]:
            patch = mock.patch.object(obj, attr, **kwargs)
            value = patch.start(); self.addCleanup(patch.stop)
            if attr == 'start_login': self.workbuddy_start = value

    def close_db(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self.original_conn

    def start(self, platform='qoder', realm='cn', **kwargs):
        return self.client.post('/api/auth/start', params={'platform': platform, 'realm': realm},
                                json={'realm': realm, **kwargs})

    def poll(self, state, platform='qoder', realm='cn'):
        return self.client.get('/api/auth/poll', params={
            'state': state, 'platform': platform, 'realm': realm})

    def finish(self, state, url=None):
        return self.client.post('/api/auth/finish?platform=trae&realm=cn', json={
            'state': state, 'callback_url': url or callback(), 'realm': 'cn'})

    def review(self, state=None, platform='trae', realm='cn', **kwargs):
        return self.client.post('/api/auth/review', params={'platform': platform, 'realm': realm},
                                json={'realm': realm, **({'state': state} if state is not None else {}),
                                      **kwargs})

    def evidence_rows(self):
        return [dict(row) for row in db.query('SELECT * FROM settings ORDER BY key')]

    def test_review_unknown_is_read_only_private_and_not_a_retry(self):
        state = self.start('trae').json()['state']
        self.trae.oauth_finish.side_effect = TimeoutError('MUST-NOT-LEAK')
        self.assertEqual(self.finish(state).status_code, 502)
        before = self.evidence_rows()
        for _ in range(2):
            with mock.patch.object(db, 'transaction', side_effect=AssertionError('read-only review')):
                response = self.review(state)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            item = response.json()['sessions'][0]
            self.assertEqual(item['status'], 'unconfirmed')
            self.assertTrue(item['requires_reconciliation'])
            self.assertEqual(item['reference'], state[-8:])
            for secret in ('MUST-NOT-LEAK', state, TRACE, 'engine', 'auth_url', 'authUrl'):
                self.assertNotIn('"' + secret + '"' if secret == 'engine' else secret, response.text)
        self.assertEqual(self.evidence_rows(), before)
        self.trae.oauth_finish.assert_awaited_once()
        self.qoder.oauth_poll.assert_not_awaited()
        self.assertEqual(self.start('trae').status_code, 409)

    def test_review_survives_engine_generation_change_and_missing_runtime(self):
        self.trae.authorization_generation = 'before'
        state = self.start('trae').json()['state']
        self.trae.authorization_generation = 'after'
        response = self.review(state)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['sessions'][0]['status'], 'engine_changed')
        self.assertEqual(self.finish(state).status_code, 404)
        self.app.state.rt = None
        with mock.patch.object(platformauth, '_adapter', side_effect=AssertionError('must not start runtime')):
            response = self.review(state)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['sessions'][0]['engine_binding'], 'unavailable')
        self.trae.oauth_finish.assert_not_awaited()

    def test_review_projects_confirmed_success_after_engine_change_without_credentials(self):
        state = self.start('trae').json()['state']
        self.assertEqual(self.finish(state).status_code, 200)
        session = platformauth._read(state)
        session['result']['accessToken'] = 'MUST-NOT-LEAK'
        session['result']['authUrl'] = 'MUST-NOT-LEAK'
        platformauth._save(state, session)
        self.trae.authorization_generation = 'changed'
        response = self.review(state)
        item = response.json()['sessions'][0]
        self.assertEqual(item['status'], 'success')
        self.assertEqual(item['uid'], 'fixture-trae')
        self.assertNotIn('MUST-NOT-LEAK', response.text)
        self.trae.oauth_finish.assert_awaited_once()

    def test_review_list_is_owner_platform_and_realm_bound(self):
        state = self.start('qoder').json()['state']
        self.start('qoder', 'global')
        self.user = owner('bob')
        bob_state = self.start('qoder').json()['state']
        self.assertEqual(self.review(state, 'qoder').status_code, 404)
        response = self.review(platform='qoder')
        self.assertEqual([item['reference'] for item in response.json()['sessions']], [bob_state[-8:]])
        self.user = owner('alice')
        self.assertEqual(self.review(state, 'trae').status_code, 404)
        self.assertEqual(self.review(state, 'qoder', 'global').status_code, 404)
        self.assertEqual(self.review(state, 'qoder', upstream_id=9).status_code, 404)
        response = self.review(platform='qoder')
        self.assertEqual([item['reference'] for item in response.json()['sessions']], [state[-8:]])
        self.qoder.oauth_poll.assert_not_awaited()

    def test_review_orphaned_operation_remains_unconfirmed_without_mutating_or_pruning(self):
        state = self.start('trae').json()['state']
        session = platformauth._read(state)
        session.update(status='finishing', operation_deadline=0)
        platformauth._save(state, session)
        before = self.evidence_rows()
        with mock.patch.object(platformauth.time, 'time', return_value=9999999999):
            response = self.review()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['sessions'][0]['status'], 'unconfirmed')
        self.assertEqual(self.evidence_rows(), before)
        self.trae.oauth_finish.assert_not_awaited()

    def test_review_does_not_prune_expired_or_cancelled_evidence(self):
        state = self.start('qoder').json()['state']
        expires = platformauth._read(state)['expires']
        before = self.evidence_rows()
        with mock.patch.object(platformauth.time, 'time', return_value=expires + 1):
            self.assertEqual(self.review(state, 'qoder').json()['sessions'][0]['status'], 'expired')
        with mock.patch.object(platformauth.time, 'time', return_value=expires + platformauth.RESULT_TTL + 1):
            self.assertEqual(self.review(platform='qoder').json()['sessions'], [])
        self.assertEqual(self.evidence_rows(), before)
        self.assertEqual(self.client.post('/api/auth/cancel?platform=qoder', json={
            'state': state, 'realm': 'cn'}).status_code, 200)
        self.assertEqual(self.review(state, 'qoder').json()['sessions'][0]['status'], 'invalid')
        self.qoder.oauth_poll.assert_not_awaited()

    def test_review_authz_contract_requires_a_human_admin(self):
        from server.qtgcore.authz import find_rule, ROLE_ADMIN
        rule = find_rule('POST', '/api/auth/review')
        self.assertTrue(rule.human_only)
        self.assertEqual(rule.allow, frozenset({ROLE_ADMIN}))

    def test_review_rejects_viewer_service_token_conflicts_and_cross_origin(self):
        state = self.start('trae').json()['state']
        self.user = {'username': 'alice', 'role': 'viewer'}
        self.assertEqual(self.review(state).status_code, 403)
        self.user = owner('alice')
        from fastapi import Request
        def token_user(request):
            request.state.token_id = 1
            return self.user
        token_user.__annotations__['request'] = Request
        self.app.dependency_overrides[security.current_user] = token_user
        self.assertEqual(self.review(state).status_code, 403)
        self.app.dependency_overrides[security.current_user] = lambda: self.user
        self.assertEqual(self.review(state, realm='global').status_code, 404)
        for body in ({'state': state, 'platform': 'qoder'}, {'state': state, 'realm': 'global'}):
            response = self.client.post('/api/auth/review?platform=trae&realm=cn', json=body)
            self.assertEqual(response.status_code, 400, response.text)
        for headers in ({'Origin': 'https://evil.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
            response = self.client.post('/api/auth/review?platform=trae&realm=cn',
                                        json={'state': state}, headers=headers)
            self.assertEqual(response.status_code, 403)

    def test_qoder_cn_and_global_never_call_workbuddy(self):
        for realm, engine, host in [('cn', 'cn', 'qoder.com.cn'),
                                    ('global', 'intl', 'qoder.com')]:
            with self.subTest(realm=realm):
                response = self.start(realm=realm)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result['platform'], 'qoder')
                self.assertEqual(result['realm'], realm)
                self.assertEqual(result['mode'], 'poll')
                self.assertIn(host + '/device/selectAccounts', result['authUrl'])
                self.assertNotEqual(result['state'], 'engine-' + engine)
                self.qoder.oauth_start.assert_awaited_with(engine)
        self.workbuddy_start.assert_not_awaited()

    def test_trae_cn_uses_trae_callback_and_global_is_explicitly_unsupported(self):
        response = self.start('trae')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['mode'], 'callback')
        self.assertIn('www.trae.cn/authorization', response.json()['authUrl'])
        unsupported = self.start('trae', 'global')
        self.assertEqual(unsupported.status_code, 409, unsupported.text)
        self.trae.oauth_login_url.assert_awaited_once()
        self.workbuddy_start.assert_not_awaited()

    def test_invalid_or_conflicting_platform_is_rejected_before_any_provider(self):
        for params, headers in [({'platform': 'not-a-platform'}, {}),
                                ({'platform': 'qoder'}, {'X-QTG-Platform': 'trae'})]:
            response = self.client.post('/api/auth/start', params=params, headers=headers,
                                        json={'realm': 'cn'})
            self.assertEqual(response.status_code, 400, response.text)
        self.workbuddy_start.assert_not_awaited()
        self.qoder.oauth_start.assert_not_awaited()
        self.trae.oauth_login_url.assert_not_awaited()

    def test_qoder_poll_is_owner_platform_realm_and_group_bound(self):
        state = self.start().json()['state']
        self.user = owner('bob')
        self.assertEqual(self.poll(state).status_code, 404)
        self.user = owner('alice')
        self.assertEqual(self.poll(state, realm='global').status_code, 404)
        self.assertEqual(self.poll(state, platform='workbuddy').status_code, 404)
        response = self.client.get('/api/auth/poll', params={
            'platform': 'qoder', 'realm': 'cn', 'state': state, 'upstream_id': 9})
        self.assertEqual(response.status_code, 404)
        self.qoder.oauth_poll.assert_not_awaited()
        self.assertEqual(self.poll(state).json()['status'], 'waiting')

    def test_qoder_success_is_cached_and_only_projects_safe_metadata(self):
        state = self.start().json()['state']
        self.qoder.oauth_poll.return_value = {'status': 'ok', 'account': {
            'uid': 'synthetic-qoder', 'nickname': 'Synthetic', 'realm': 'cn',
            'accessToken': 'MUST-NOT-LEAK'}}
        for _ in range(2):
            response = self.poll(state)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'success')
            self.assertNotIn('MUST-NOT-LEAK', response.text)
        self.qoder.oauth_poll.assert_awaited_once()

    def test_trae_finish_is_one_use_and_never_returns_credentials(self):
        state = self.start('trae').json()['state']
        response = self.finish(state)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'success')
        self.assertNotIn('MUST-NOT-LEAK', response.text)
        self.assertEqual(self.finish(state).json()['status'], 'success')
        self.trae.oauth_finish.assert_awaited_once()

    def test_trae_callback_rejects_ssrf_legacy_token_bypass_and_wrong_trace(self):
        state = self.start('trae').json()['state']
        for url in [callback(host='http://127.0.0.1:8080'),
                    callback(host='https://api.trae.com.cn.evil.invalid'),
                    callback(loginTraceID='b' * 32),
                    callback(refreshToken='forged'),
                    callback().replace('/authorize?', '/other?'),
                    callback() + '&host=https://api.trae.com.cn',
                    callback().replace('http://127.0.0.1:17388', 'https://evil.invalid')]:
            with self.subTest(url=url):
                self.assertEqual(self.finish(state, url).status_code, 400)
        self.trae.oauth_finish.assert_not_awaited()
        self.assertEqual(self.finish(state).status_code, 200)

    def test_trae_single_pending_engine_state_cannot_be_overwritten_by_other_owner(self):
        self.start('trae')
        self.user = owner('bob')
        self.assertEqual(self.start('trae').status_code, 409)
        self.trae.oauth_login_url.assert_awaited_once()

    def test_non_workbuddy_options_are_not_silently_ignored(self):
        for body in [{'region': 'HK'}, {'proxy': 'route-a'}]:
            self.assertEqual(self.start(**body).status_code, 409)
        response = self.client.post('/api/auth/start?platform=qoder&upstream_id=9',
                                    json={'realm': 'cn'})
        self.assertEqual(response.status_code, 409)
        self.qoder.oauth_start.assert_not_awaited()

    def test_viewer_and_service_token_cannot_start_or_finish_authorization(self):
        self.user = {'username': 'viewer', 'role': 'viewer'}
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(self.finish('unknown').status_code, 403)
        self.user = {'username': 'service', 'role': 'admin', 'scope': 'admin'}
        def token_user(request):
            request.state.token_id = 1
            return self.user
        from fastapi import Request
        token_user.__annotations__['request'] = Request
        self.app.dependency_overrides[security.current_user] = token_user
        self.assertEqual(self.start().status_code, 403)

    def test_cookie_write_origin_and_effectful_poll_origin_are_enforced(self):
        def authenticated(request):
            security.check_same_origin(request)
            return self.user
        from fastapi import Request
        authenticated.__annotations__['request'] = Request
        self.app.dependency_overrides[security.current_user] = authenticated
        self.client.cookies.set(config.COOKIE_NAME, 'synthetic-cookie')
        response = self.client.post('/api/auth/start?platform=qoder', json={'realm': 'cn'},
                                    headers={'Origin': 'https://evil.invalid'})
        self.assertEqual(response.status_code, 403)
        state = self.start().json()['state']
        response = self.client.get('/api/auth/poll', params={
            'platform': 'qoder', 'realm': 'cn', 'state': state},
            headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(response.status_code, 403)
        self.qoder.oauth_poll.assert_not_awaited()

    def test_expired_session_cannot_poll_or_finish(self):
        state = self.start('trae').json()['state']
        expires = platformauth._read(state)['expires']
        with mock.patch.object(platformauth.time, 'time', return_value=expires + 1):
            self.assertEqual(self.poll(state, 'trae').json()['status'], 'expired')
            self.assertEqual(self.finish(state).status_code, 409)
        with mock.patch.object(platformauth.time, 'time', return_value=expires + platformauth.RESULT_TTL + 1):
            self.assertEqual(self.poll(state, 'trae').status_code, 404)
        self.trae.oauth_finish.assert_not_awaited()

    def test_engine_errors_never_leak_credentials(self):
        self.qoder.oauth_start.side_effect = RuntimeError('MUST-NOT-LEAK')
        response = self.start()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('MUST-NOT-LEAK', response.text)
        self.assertNotIn('MUST-NOT-LEAK', repr([dict(r) for r in db.query(
            'SELECT * FROM settings WHERE key LIKE ?', (platformauth.PREFIX + '%',))]))

    def test_ambiguous_finish_is_not_replayed_or_replaced_after_timeout(self):
        state = self.start('trae').json()['state']
        self.trae.oauth_finish.side_effect = TimeoutError('MUST-NOT-LEAK')
        response = self.finish(state)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('MUST-NOT-LEAK', response.text)
        self.assertEqual(self.finish(state).status_code, 409)
        self.assertEqual(self.start('trae').status_code, 409)
        with mock.patch.object(platformauth.time, 'time', return_value=9999999999):
            self.assertEqual(self.start('trae').status_code, 409)
        self.trae.oauth_finish.assert_awaited_once()

    def test_cancelling_a_waiting_trae_session_releases_the_engine_slot(self):
        state = self.start('trae').json()['state']
        response = self.client.post('/api/auth/cancel?platform=trae', json={
            'state': state, 'realm': 'cn'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.finish(state).status_code, 409)
        self.user = owner('bob')
        self.assertEqual(self.start('trae').status_code, 200)

    def test_qoder_lost_response_is_unconfirmed_and_not_polled_again(self):
        state = self.start().json()['state']
        self.qoder.oauth_poll.side_effect = TimeoutError('MUST-NOT-LEAK')
        for _ in range(2):
            response = self.poll(state)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'unconfirmed')
            self.assertNotIn('MUST-NOT-LEAK', response.text)
        self.qoder.oauth_poll.assert_awaited_once()

    def test_orphaned_poll_never_claims_waiting_or_expired_or_retries(self):
        state = self.start().json()['state']
        session = platformauth._read(state)
        session.update(status='polling', operation_deadline=0)
        platformauth._save(state, session)
        self.assertEqual(self.poll(state).json()['status'], 'unconfirmed')
        with mock.patch.object(platformauth.time, 'time', return_value=9999999999):
            self.assertEqual(self.poll(state).json()['status'], 'unconfirmed')
        self.qoder.oauth_poll.assert_not_awaited()

    def test_starting_trae_session_cannot_be_cancelled_while_engine_is_busy(self):
        state = self.start('trae').json()['state']
        session = platformauth._read(state)
        session['status'] = 'starting'
        platformauth._save(state, session)
        response = self.client.post('/api/auth/cancel?platform=trae', json={
            'state': state, 'realm': 'cn'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.start('trae').status_code, 409)

    def test_trae_code_plus_sign_and_space_survive_the_legacy_url_decoder(self):
        state = self.start('trae').json()['state']
        value = 'synthetic+a/b=c with space'
        response = self.finish(state, callback(authCodeInfo=json.dumps({'AuthCode': value})))
        self.assertEqual(response.status_code, 200, response.text)
        raw = urlsplit(self.trae.oauth_finish.await_args.args[0]).query
        legacy = {k: unquote(v) for k, v in (pair.split('=', 1) for pair in raw.split('&'))}
        self.assertEqual(json.loads(legacy['authCodeInfo'])['AuthCode'], value)

    def test_realm_conflicts_and_duplicates_never_call_provider(self):
        for path, body in [('/api/auth/start?platform=qoder&realm=cn', {'realm': 'global'}),
                           ('/api/auth/start?platform=qoder&realm=cn&realm=global', {}),
                           ('/api/auth/finish?platform=trae&realm=cn', {'realm': 'global'}),
                           ('/api/auth/cancel?platform=trae&realm=cn', {'realm': 'global'})]:
            self.assertEqual(self.client.post(path, json=body).status_code, 400)
        self.qoder.oauth_start.assert_not_awaited()
        self.trae.oauth_finish.assert_not_awaited()

    def test_completed_success_is_not_changed_by_secondary_audit_failure(self):
        state = self.start('trae').json()['state']
        with mock.patch.object(security, 'audit', side_effect=OSError('MUST-NOT-LEAK')):
            response = self.finish(state)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(platformauth._read(state)['status'], 'success')
        self.assertEqual(self.finish(state).json()['status'], 'success')
        self.trae.oauth_finish.assert_awaited_once()

    def test_trae_callback_cannot_choose_the_saved_account_identity(self):
        state = self.start('trae').json()['state']
        response = self.finish(state, callback(userInfo=json.dumps({'UserID': 'another-account'})))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('userInfo', self.trae.oauth_finish.await_args.args[0])
        self.assertNotIn('another-account', self.trae.oauth_finish.await_args.args[0])

    def test_restarted_engine_generation_invalidates_old_trae_session(self):
        self.trae.authorization_generation = 'first'
        state = self.start('trae').json()['state']
        self.trae.authorization_generation = 'second'
        self.assertEqual(self.finish(state).status_code, 404)
        self.trae.oauth_finish.assert_not_awaited()

    def test_authorization_session_details_are_never_part_of_settings_panel(self):
        self.start('trae')
        from server.routers.settings import settings_panel
        self.assertNotIn(TRACE, json.dumps(settings_panel(user=self.user)))

    def test_full_manager_middleware_does_not_capture_platform_auth_routes(self):
        from server.main import app
        old_rt = getattr(app.state, 'rt', None)
        old_overrides = dict(app.dependency_overrides)
        app.state.rt = self.app.state.rt
        app.dependency_overrides[security.current_user] = lambda: self.user
        try:
            # No context manager: lifespan/background jobs are not part of this fixture.
            client = TestClient(app)
            for platform in ('qoder', 'trae'):
                response = client.post('/api/auth/start', params={'platform': platform},
                                       headers={'X-QTG-Platform': platform}, json={'realm': 'cn'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['platform'], platform)
            self.workbuddy_start.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(old_overrides)
            if old_rt is None:
                delattr(app.state, 'rt')
            else:
                app.state.rt = old_rt


class PlatformAuthAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_retry_also_disables_401_reauthentication_and_replay(self):
        import httpx
        from server.qtgcore.adapters.base import BaseAdapter
        from server.qtgcore.config import Settings
        calls = []
        async def respond(request):
            calls.append(request.url.path)
            return httpx.Response(401, json={'detail': 'synthetic-expired-session'})
        adapter = BaseAdapter('http://127.0.0.1:1', Settings())
        adapter.authenticate = mock.AsyncMock()
        adapter._authenticated = True
        adapter._client = httpx.AsyncClient(base_url=adapter.base_url, transport=httpx.MockTransport(respond))
        try:
            response = await adapter._request('POST', '/stateful-operation', retry=False)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(calls, ['/stateful-operation'])
            adapter.authenticate.assert_not_awaited()
        finally:
            await adapter.close()

    async def test_trae_uses_exact_commands_and_unwraps_only_confirmed_envelope(self):
        from server.qtgcore.adapters.trae2 import Trae2Adapter
        adapter = Trae2Adapter.__new__(Trae2Adapter)
        adapter._json = mock.AsyncMock(return_value={'ok': True, 'data': trae_start()})
        self.assertEqual((await adapter.oauth_login_url())['state'], TRACE)
        adapter._json.assert_awaited_once_with('POST', '/api/cmd/oauth_get_login_url',
                                              json_body={}, retry=False)
        adapter._json.reset_mock()
        adapter._json.return_value = {'ok': True, 'data': {'user_id': 'fixture'}}
        self.assertEqual(await adapter.oauth_finish('synthetic-callback'), {'user_id': 'fixture'})
        adapter._json.assert_awaited_once_with('POST', '/api/cmd/oauth_login',
            json_body={'callback_url': 'synthetic-callback'}, retry=False, auth=False)
        from server.qtgcore.errors import UpstreamError
        adapter._json.return_value = {'ok': False, 'data': {'user_id': 'fixture'}}
        with self.assertRaises(UpstreamError):
            await adapter.oauth_finish('synthetic-callback')

    async def test_qoder_oauth_never_retries_stateful_poll_and_never_swallows_error(self):
        from server.qtgcore.adapters.qoder2 import Qoder2Adapter
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        adapter._json = mock.AsyncMock(return_value={'status': 'pending'})
        await adapter.oauth_start('intl')
        adapter._json.assert_awaited_with('POST', '/accounts/login/start',
            json_body={'platform': 'CLI', 'realm': 'intl'}, retry=False)
        await adapter.oauth_poll('fixture')
        adapter._json.assert_awaited_with('GET', '/accounts/login/poll',
                                         params={'state': 'fixture'}, retry=False)


if __name__ == '__main__':
    unittest.main()
