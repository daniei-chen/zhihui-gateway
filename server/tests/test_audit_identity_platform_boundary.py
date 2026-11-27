"""Authenticated audit and retired-platform boundaries, with synthetic data only."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, field_validator
from starlette.responses import JSONResponse

from server import (auditidentity, config, db, keysvc, ledger, metricsauth,
                    observability, platformpolicy, security, tokensvc)
from server.routers import anthropic, gateway, responses
from server.qtgcore.errors import install_handlers
from server.tests._db_fixture import isolate_database
from server.tests.authorization_test_support import install_users


class _PlatformValidationFixture(BaseModel):
    platforms: list[str]

    @field_validator('platforms')
    @classmethod
    def known_platforms(cls, value):
        return platformpolicy.normalize(value, default=False)


class ValidationErrorBoundaryTest(unittest.TestCase):
    def test_pydantic_value_error_returns_structured_422(self):
        app = FastAPI()
        install_handlers(app)

        @app.post('/fixture')
        def validate(body: _PlatformValidationFixture):
            return body

        with TestClient(app) as client:
            response = client.post('/fixture', json={'platforms': ['unknown']})
        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload['error']['code'], 'validation_error')
        self.assertEqual(payload['detail'], payload['error']['details'])
        self.assertEqual(payload['detail'][0]['loc'], ['body', 'platforms'])
        self.assertEqual(payload['detail'][0]['type'], 'value_error')
        self.assertNotIn('ctx', payload['detail'][0])

    def test_validation_does_not_echo_input_or_internal_context(self):
        app = FastAPI()
        install_handlers(app)
        secret = 'synthetic-private-context-never-return'

        @app.post('/fixture')
        def validate():
            raise RequestValidationError([{
                'type': 'value_error', 'loc': ('body', 'platforms'),
                'msg': 'Invalid platform', 'input': {'password': secret},
                'ctx': {'error': ValueError(secret), 'private_state': secret},
            }] * 9)

        with TestClient(app) as client:
            response = client.post('/fixture')
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertEqual(len(response.json()['detail']), 8)
        self.assertEqual(set(response.json()['detail'][0]), {'type', 'loc', 'msg'})


class ManagementAuditIdentityTest(unittest.TestCase):
    def setUp(self):
        directory = isolate_database(self)
        self.enterContext(mock.patch.object(config, 'USERS_FILE', directory / 'users.json'))
        self.enterContext(mock.patch.object(config, 'SHARED_ADMISSION', False))
        install_users('operator')
        app = FastAPI()

        @app.post('/api/settings/example')
        def write(user=Depends(security.require_admin)):
            return {'ok': True}

        @app.post('/api/keys')
        def keys(user=Depends(security.require_keys_writer)):
            return {'ok': True}

        @app.get('/api/me')
        def me(user=Depends(security.current_user)):
            return {'username': user['username']}

        @app.post('/api/legacy')
        def legacy(request: Request):
            request.state.audit_actor = 'legacy:operator'
            return {'ok': True}

        @app.get('/api/forbidden')
        def forbidden(user=Depends(security.require_admin)):
            return {'ok': True}

        @app.get('/api/failure')
        def failure(user=Depends(security.current_user)):
            raise HTTPException(status_code=500, detail='synthetic server failure')

        @app.get('/api/sensitive')
        def sensitive(user=Depends(security.require_admin)):
            security.audit(user, 'fixture.sensitive_read', 'synthetic')
            return {'ok': True}

        app.add_middleware(observability.RequestAuditMiddleware)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def event(self, response):
        row = db.query_one('SELECT * FROM admin_events WHERE request_id=?',
                           (response.headers['x-request-id'],))
        self.assertIsNotNone(row, response.text)
        self.assertTrue(ledger.verify_chain())
        return row

    def bearer(self, scope):
        token = tokensvc.create_token('fixture-' + scope, scope, None, 'operator')['token']
        return {'Authorization': 'Bearer ' + token}

    def test_cookie_success_keeps_username_and_request_id_in_ledger(self):
        self.client.cookies.set(config.COOKIE_NAME, security.issue_token('operator', 'admin'))
        response = self.client.post('/api/settings/example')
        self.assertEqual(response.status_code, 200, response.text)
        event = self.event(response)
        self.assertEqual(event['actor'], 'operator')
        self.assertEqual(event['action'], 'POST /api/settings/example')
        self.assertEqual(event['outcome'], 'completed')
        self.assertEqual(event['status'], 200)

    def test_admin_and_keys_service_tokens_keep_success_identity(self):
        for scope, path in (('admin', '/api/settings/example'), ('keys', '/api/keys')):
            with self.subTest(scope=scope):
                response = self.client.post(path, headers=self.bearer(scope))
                self.assertEqual(response.status_code, 200, response.text)
                event = self.event(response)
                self.assertEqual(event['actor'], 'token:fixture-' + scope)
                self.assertEqual(event['outcome'], 'completed')

    def test_scope_and_role_refusals_keep_authenticated_actor(self):
        for scope in ('read', 'keys', 'readonly'):
            with self.subTest(scope=scope):
                response = self.client.post('/api/settings/example', headers=self.bearer(scope))
                self.assertEqual(response.status_code, 403, response.text)
                event = self.event(response)
                self.assertEqual(event['actor'], 'token:fixture-' + scope)
                self.assertEqual(event['outcome'], 'denied')

    def test_legacy_string_identity_remains_readable(self):
        response = self.client.post('/api/legacy')
        self.assertEqual(self.event(response)['actor'], 'legacy:operator')

    def test_authenticated_successful_poll_does_not_append_permanent_admin_chain(self):
        before = db.query_one('SELECT COUNT(*) n FROM admin_events')['n']
        response = self.client.get('/api/me', headers=self.bearer('admin'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM admin_events')['n'], before)
        self.assertIsNotNone(db.query_one('SELECT request_id FROM request_outcomes WHERE request_id=?',
                                         (response.headers['x-request-id'],)))

    def test_authenticated_failed_reads_keep_status_outcome_and_identity(self):
        for path, scope, status, outcome in (
                ('/api/forbidden', 'readonly', 403, 'denied'),
                ('/api/failure', 'admin', 500, 'failed')):
            with self.subTest(status=status):
                response = self.client.get(path, headers=self.bearer(scope))
                self.assertEqual(response.status_code, status, response.text)
                event = self.event(response)
                self.assertEqual(event['actor'], 'token:fixture-' + scope)
                self.assertEqual(event['status'], status)
                self.assertEqual(event['outcome'], outcome)

    def test_sensitive_successful_read_keeps_explicit_audit_without_duplicate(self):
        response = self.client.get('/api/sensitive', headers=self.bearer('admin'))
        self.assertEqual(response.status_code, 200, response.text)
        events = db.query('SELECT * FROM admin_events WHERE request_id=?',
                          (response.headers['x-request-id'],))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['actor'], 'token:fixture-admin')
        self.assertEqual(events[0]['action'], 'fixture.sensitive_read')
        self.assertTrue(ledger.verify_chain())

    def test_anonymous_get_refusal_does_not_expand_permanent_admin_log(self):
        before = db.query_one('SELECT COUNT(*) n FROM admin_events')['n']
        response = self.client.get('/api/me')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM admin_events')['n'], before)
        self.assertIsNotNone(db.query_one('SELECT request_id FROM request_outcomes WHERE request_id=?',
                                         (response.headers['x-request-id'],)))

    def test_actor_snapshot_never_contains_credentials_or_mutable_user_store(self):
        request = Request({'type': 'http'})
        user = {'username': 'operator', 'role': 'admin', 'scope': '',
                'pwd_hash': 'synthetic-private-hash', 'secret': 'synthetic-private-secret'}
        auditidentity.bind(request, user)
        user['username'] = 'changed-later'
        self.assertEqual(request.state.audit_actor,
                         {'username': 'operator', 'role': 'admin', 'scope': ''})
        self.assertNotIn('private', json.dumps(request.state.audit_actor))

    def test_metrics_identity_uses_same_contract_without_collector_secret(self):
        credential = 'wbm_' + 'synthetic-collector-' * 3
        request = Request({'type': 'http', 'headers': [
            (b'authorization', ('Bearer ' + credential).encode())]})
        with mock.patch.object(metricsauth, '_collector_secret', return_value=credential):
            actor = metricsauth.require_metrics(request)
        self.assertEqual(request.state.audit_actor, actor)
        self.assertEqual(auditidentity.actor_name(actor), 'collector:metrics')
        self.assertNotIn(credential, json.dumps(request.state.audit_actor))


class RetiredPlatformBoundaryTest(unittest.TestCase):
    def setUp(self):
        isolate_database(self)
        self.key = keysvc.create_key('historical-platform-fixture',
                                     platforms=list(platformpolicy._ACCEPTED))
        self.headers = {'Authorization': 'Bearer ' + self.key['key']}
        app = FastAPI()
        app.include_router(gateway.router)
        app.include_router(anthropic.router)
        app.include_router(responses.router)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_historical_names_and_key_metadata_remain_readable(self):
        expected = list(platformpolicy._ACCEPTED)
        self.assertEqual(platformpolicy.normalize(expected), expected)
        self.assertEqual(keysvc.resolve(self.key['key'])['platforms'], expected)
        self.assertEqual(keysvc.list_keys()[0]['platforms'], expected)

    def test_disabled_platforms_cannot_be_reenabled_by_key_grant(self):
        for name in platformpolicy._ACCEPTED:
            with self.subTest(platform=name):
                rejected = keysvc.validate(self.key, '127.0.0.1', None,
                                           is_model_list=True, platform=name)
                if platformpolicy.is_enabled(name):
                    self.assertIsNone(rejected)
                else:
                    self.assertEqual(rejected.status, 503)
                    self.assertEqual(rejected.code, 'platform_disabled')

    def test_key_authority_stays_narrow_when_platform_is_enabled(self):
        key = keysvc.create_key('workbuddy-only', platforms=['workbuddy'])
        with mock.patch.dict(platformpolicy.PLATFORM_SPEC, {
                'trae': replace(platformpolicy.spec('trae'), enabled=True)}):
            self.assertIsNone(keysvc.validate(self.key, '127.0.0.1', None,
                                              is_model_list=True, platform='trae'))
            rejected = keysvc.validate(key, '127.0.0.1', None,
                                       is_model_list=True, platform='trae')
        self.assertEqual(rejected.code, 'platform_not_allowed')
        self.assertEqual(rejected.status, 403)

    def test_paths_headers_and_model_prefixes_refuse_before_upstream_resolution(self):
        cases = [
            ('GET', '/trae/v1/models', None, {}),
            ('POST', '/trae/v1/chat/completions',
             {'model': 'fixture', 'messages': [{'role': 'user', 'content': 'fixture'}]}, {}),
            ('POST', '/v1/chat/completions',
             {'model': 'qoder/fixture', 'messages': [{'role': 'user', 'content': 'fixture'}]}, {}),
            ('POST', '/v1/messages', {'model': 'fixture', 'max_tokens': 16,
             'messages': [{'role': 'user', 'content': 'fixture'}]}, {'X-QTG-Platform': 'trae'}),
            ('POST', '/v1/responses', {'model': 'doubaowork/fixture', 'input': 'fixture'}, {}),
        ]
        with mock.patch.object(gateway, '_upstream_for') as upstream, \
                mock.patch.object(config, 'http_client') as client:
            for method, path, body, headers in cases:
                with self.subTest(path=path, body=body):
                    response = self.client.request(method, path, json=body,
                                                   headers={**self.headers, **headers})
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(response.json()['error']['code'], 'platform_disabled')
        upstream.assert_not_called()
        client.assert_not_called()

    def test_known_retired_path_is_rejected_during_header_preauthorization(self):
        request = Request({'type': 'http', 'method': 'POST', 'path': '/trae/v1/chat/completions',
                           'headers': [(b'authorization', self.headers['Authorization'].encode())],
                           'query_string': b'', 'client': ('127.0.0.1', 1234)})
        result = gateway.preauthorize_body(request)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(json.loads(result.body)['error']['code'], 'platform_disabled')


class NativePreparationRefusalTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_adapter_never_runs_after_budget_or_authorization_refusal(self):
        body = {'model': 'fixture', 'max_tokens': 16,
                'messages': [{'role': 'user', 'content': 'fixture'}]}
        for status, code in ((403, 'key_revoked'), (429, 'quota_exhausted'),
                             (503, 'service_unavailable')):
            with self.subTest(status=status):
                request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/messages',
                                   'headers': [], 'query_string': b''})
                request.state.requested_platform = 'trae'
                refusal = JSONResponse({'error': {'message': 'synthetic refusal',
                                                  'type': 'permission_error', 'code': code}},
                                       status_code=status, headers={'Retry-After': '7'})
                with mock.patch.object(gateway, '_read_json_body',
                                       new=mock.AsyncMock(return_value=(dict(body), None))), \
                        mock.patch.object(gateway, '_map_model', return_value='fixture'), \
                        mock.patch.object(anthropic, '_authorize', return_value=({'id': 1}, '127.0.0.1', None)), \
                        mock.patch.object(gateway, '_inject_session_key'), \
                        mock.patch.object(gateway, '_upstream_for',
                                          return_value=({'base_url': 'http://127.0.0.1:9'}, None)), \
                        mock.patch.object(gateway, '_stamp_route'), \
                        mock.patch.object(gateway, '_prepare_forward_async',
                                          new=mock.AsyncMock(return_value=refusal)), \
                        mock.patch.object(anthropic, 'trae_native_messages', new=mock.AsyncMock()) as native, \
                        mock.patch.object(config, 'http_client') as client:
                    result = await anthropic.messages(request)
                self.assertEqual(result.status_code, status)
                self.assertEqual(json.loads(result.body)['error']['code'], code)
                self.assertEqual(result.headers['Retry-After'], '7')
                native.assert_not_awaited()
                client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
