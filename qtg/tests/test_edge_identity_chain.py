"""Actual authorization boundaries for the Manager-to-QTG identity bridge."""
from identity_support import GOOD_ORIGIN, IdentityTestBase


class EdgeIdentityChainTest(IdentityTestBase):
    settings_overrides = {'read_requires_auth': True}

    def test_verified_manager_viewer_overrides_stale_local_admin_cookie(self):
        headers = self.cookie_headers(self.session_cookie(role='admin', sub='old-local'))
        headers['X-QTG-Identity'] = self.signed_identity(sub='current-viewer', role='viewer')
        result = self.client.get('/api/v1/authz/identity', headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['identity']['sub'], 'current-viewer')
        denied = self.client.post('/api/v1/actions/limits', json={'enabled': True},
                                 headers={**headers, 'Origin': GOOD_ORIGIN})
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_bad_edge_assertion_cannot_fall_back_to_local_cookie(self):
        headers = self.cookie_headers(self.session_cookie())
        headers['X-QTG-Identity'] = 'v1.invalid.signature'
        result = self.client.get('/api/v1/authz/identity', headers=headers)
        self.assertEqual(result.status_code, 401, result.text)

    def test_missing_edge_assertion_cannot_fall_back_to_local_cookie(self):
        headers = self.cookie_headers(self.session_cookie())
        headers['X-QTG-Identity-Required'] = '1'
        result = self.client.get('/api/v1/authz/identity', headers=headers)
        self.assertEqual(result.status_code, 401, result.text)

    def test_ambient_manager_cookie_cannot_become_machine_identity(self):
        result = self.client.get('/api/v1/authz/identity', headers={
            **self.internal, 'Cookie': 'wb_session=expired-or-unverified'})
        self.assertEqual(result.status_code, 401, result.text)

    def test_unrecognized_identity_markers_are_rejected_even_with_internal_token(self):
        result = self.client.get('/api/v1/authz/identity', headers={
            **self.internal, 'X-User': 'forged-admin'})
        self.assertEqual(result.status_code, 401, result.text)

    def test_keys_service_scope_does_not_grant_general_admin_actions(self):
        headers = {'X-QTG-Identity': self.signed_identity(
            role='internal_service', auth_method='admin_token',
            sub='manager-token:7', scopes=['keys:read', 'keys:write'])}
        probe = self.client.get('/api/v1/authz/probe', params={
            'method': 'POST', 'path': '/api/keys'}, headers=headers)
        self.assertEqual(probe.status_code, 200, probe.text)
        for method, path in [('POST', '/api/v1/actions/limits'),
                             ('GET', '/api/v1/accounts'), ('GET', '/metrics')]:
            denied = self.client.get('/api/v1/authz/probe', params={
                'method': method, 'path': path}, headers=headers)
            self.assertEqual(denied.status_code, 403, denied.text)
        actual = self.client.post('/api/v1/actions/limits', json={'enabled': True}, headers=headers)
        self.assertEqual(actual.status_code, 403, actual.text)
        action_only = self.client.get('/api/v1/authz/probe', params={'action':'A42'}, headers=headers)
        self.assertEqual(action_only.status_code, 403, action_only.text)

    def test_read_scope_is_not_an_admin_alias(self):
        headers = {'X-QTG-Identity': self.signed_identity(role='admin', scopes=['read'])}
        result = self.client.post('/api/v1/actions/limits', json={'enabled': True}, headers=headers)
        self.assertEqual(result.status_code, 403, result.text)

    def test_viewer_passes_real_read_dependency(self):
        from fastapi import Depends
        @self.app.get('/api/test-read-dependency')
        async def endpoint(reader=Depends(self.rt.auth.require_read)):
            return {'reader': reader}
        # The production SPA/catch-all is already registered by create_app.
        self.app.router.routes.insert(0, self.app.router.routes.pop())
        headers = {'X-QTG-Identity': self.signed_identity(role='viewer')}
        result = self.client.get('/api/test-read-dependency', headers=headers)
        self.assertEqual(result.status_code, 200, result.text)

    def test_unknown_scope_is_not_silently_ignored(self):
        headers = {'X-QTG-Identity': self.signed_identity(role='admin', scopes=['root:everything'])}
        result = self.client.post('/api/v1/actions/limits', json={'enabled': True}, headers=headers)
        self.assertEqual(result.status_code, 403, result.text)

    def test_machine_service_token_cannot_sign_a_human_identity(self):
        import time
        from identity_support import INTERNAL_TOKEN
        from test_k05_identity import _craft_assertion
        forged = _craft_assertion({'sub':'invented-admin','role':'admin','scopes':['*'],
            'auth_method':'edge_session','aud':'qtg','exp':int(time.time()) + 60}, secret=INTERNAL_TOKEN)
        result = self.client.get('/api/v1/authz/identity', headers={'X-QTG-Identity':forged})
        self.assertEqual(result.status_code, 401, result.text)
        self.assertEqual(result.json()['code'], 'identity_tampered')

    def test_identity_key_cannot_reuse_machine_service_token(self):
        from pathlib import Path
        from identity_support import INTERNAL_TOKEN
        signed = self.signed_identity()
        Path(self.rt.settings.identity_signing_key_file).write_text(INTERNAL_TOKEN)
        result = self.client.get('/api/v1/authz/identity', headers={'X-QTG-Identity':signed})
        self.assertEqual(result.status_code, 401, result.text)
        self.assertEqual(result.json()['code'], 'identity_signing_disabled')

    def test_successful_legacy_action_audit_keeps_verified_user(self):
        headers = {'X-QTG-Identity': self.signed_identity(
            sub='current-manager-admin', scopes=['*']), 'X-Request-ID': 'identity-chain-audit'}
        result = self.client.post('/api/v1/actions/limits', json={'enabled': True}, headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        rows = self.audit_by_action('limits')
        self.assertTrue(rows)
        self.assertEqual(rows[-1]['actor'], 'admin:current-manager-admin')
        self.assertEqual(rows[-1]['detail']['identity']['auth_method'], 'edge_session')
        self.assertEqual(rows[-1]['request_id'], 'identity-chain-audit')
        # A request must not give an unrelated scheduler operation its identity.
        self.rt.record_audit(action='scheduler-test', actor='scheduler', result='ok')
        self.assertEqual(self.audit_by_action('scheduler-test')[-1]['actor'], 'scheduler')

    def test_verified_api_token_never_becomes_human_session(self):
        headers = self.cookie_headers(self.session_cookie())
        headers.update({'X-QTG-Identity': self.signed_identity(
            role='admin', auth_method='admin_token', scopes=['*']), 'Origin': GOOD_ORIGIN})
        result = self.client.post('/api/sessions/revoke', json={}, headers=headers)
        self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(result.json()['code'], 'human_session_required')


class DisabledAuthorizationBridgeTest(IdentityTestBase):
    settings_overrides = {'authz_enforce': False}

    def test_identity_bridge_requires_server_side_authorization(self):
        result = self.client.get('/api/v1/authz/identity', headers={
            'X-QTG-Identity': self.signed_identity(), 'X-QTG-Identity-Required': '1'})
        self.assertEqual(result.status_code, 503, result.text)
