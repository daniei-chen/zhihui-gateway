"""Header-only rejection must not read bodies, grant use, or change protocols."""
import asyncio
import json
import threading
import time
from unittest.mock import patch

import httpx

from server import db, keysvc, observability
from server.routers import gateway
from server.tests.test_audit_20261029_gateway_ledger import _Fixture, GOOD, PROTOCOLS


class PreauthBodyTests(_Fixture):
    def drive(self, path, headers=(), *, on_read=None, declared=None, body=b'{}'):
        reads, sent = [], []
        async def receive():
            reads.append(True)
            if on_read:
                on_read()
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'method': 'POST', 'path': path,
                 'raw_path': path.encode(), 'query_string': b'', 'scheme': 'http',
                 'server': ('fixture', 80), 'client': ('127.0.0.1', 12345),
                 'headers': [(name.lower().encode(), value.encode()) for name, value in headers]}
        if declared is not None:
            scope['headers'].append((b'content-length', str(declared).encode()))
        asyncio.run(self.app(scope, receive, send))
        status = next(row['status'] for row in sent if row['type'] == 'http.response.start')
        value = json.loads(b''.join(row.get('body', b'') for row in sent
                                   if row['type'] == 'http.response.body'))
        return status, value, len(reads)

    def test_missing_and_invalid_credentials_never_read_body_or_reserve_capacity(self):
        for prefix in ('', '/workbuddy', '/qoder', '/trae'):
            for protocol in (*PROTOCOLS, 'messages/count_tokens'):
                for auth in ((), (('Authorization', 'Bearer wbk_invalid'),)):
                    with self.subTest(prefix=prefix, protocol=protocol, auth=bool(auth)):
                        status, value, reads = self.drive(prefix + '/v1/' + protocol, auth,
                                                         declared=900 * 1024**2)
                        self.assertEqual(status, 401)
                        self.assertEqual(reads, 0)
                        self.assertIn(value['error']['code'], ('missing_api_key', 'invalid_api_key'))
                        if protocol.startswith('messages'):
                            self.assertEqual(value['type'], 'error')
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM budget_reservations')['n'], 0)
        self.assertEqual(observability.admission_snapshot()['active'], 0)
        self.assertEqual(observability.admission_snapshot()['inflight_body_bytes'], 0)

    def test_known_header_constraints_reject_without_body_read(self):
        cases = [({'enabled': False}, 403, 'key_disabled'),
                 ({'expires_at': int(time.time()) - 10}, 403, 'key_expired'),
                 ({'quota': 5, 'used_tokens': 5}, 429, 'quota_exhausted'),
                 ({'quota_credit': 1, 'used_credit': 1}, 429, 'credit_quota_exhausted'),
                 ({'ip_allowlist': ['192.0.2.0/24']}, 400, 'ip_not_allowed')]
        for fields, expected, code in cases:
            key = self.key()
            keysvc.update_key(key['id'], {name: value for name, value in fields.items()
                                         if name not in ('used_tokens', 'used_credit')})
            for name in ('used_tokens', 'used_credit'):
                if name in fields:
                    db.execute(f'UPDATE api_keys SET {name}=? WHERE id=?', (fields[name], key['id']))
            for protocol in PROTOCOLS:
                with self.subTest(fields=fields, protocol=protocol):
                    status, value, reads = self.drive('/v1/' + protocol,
                                                      (('Authorization', 'Bearer ' + key['key']),))
                    self.assertEqual((status, reads), (expected, 0))
                    self.assertEqual(value['error']['code'], code)

    def test_global_ip_policy_and_explicit_platform_are_checked_before_body(self):
        key = self.key(platforms=['qoder'])
        for protocol in PROTOCOLS:
            for headers in ((('Authorization', 'Bearer ' + key['key']), ('X-QTG-Platform', 'trae')),
                            (('Authorization', 'Bearer ' + key['key']),)):
                prefix = '' if len(headers) == 2 else '/trae'
                status, value, reads = self.drive(prefix + '/v1/' + protocol, headers)
                self.assertEqual((status, reads, value['error']['code']), (403, 0, 'platform_not_allowed'))
        with patch.object(gateway, '_global_ip_blocked', return_value=True):
            status, value, reads = self.drive('/v1/messages', (('x-api-key', key['key']),))
        self.assertEqual((status, reads, value['error']['code']), (403, 0, 'ip_blocked'))

    def test_anthropic_multi_candidate_priority_is_preserved(self):
        key = self.key()
        fields = (('x-api-key', 'sk_other_service'), ('Authorization', 'Bearer ' + key['key']))
        with self.mock_transport(lambda _: httpx.Response(200, json=GOOD)):
            status, _, reads = self.drive('/v1/messages', fields,
                                          body=json.dumps(self.payload('messages')).encode())
        self.assertEqual((status, reads), (200, 1))
        disabled = self.key()
        keysvc.update_key(disabled['id'], {'enabled': False})
        status, value, reads = self.drive('/v1/messages',
            (('x-api-key', disabled['key']), ('Authorization', 'Bearer ' + key['key'])))
        self.assertEqual((status, reads, value['error']['code']), (403, 0, 'key_disabled'))
        # OpenAI keeps Bearer priority: unlike Anthropic, it never falls through
        # an invalid Bearer token to an unrelated x-api-key.
        status, _, reads = self.drive('/v1/chat/completions',
            (('x-api-key', key['key']), ('Authorization', 'Bearer wbk_invalid')))
        self.assertEqual((status, reads), (401, 0))

    def test_model_platform_selection_and_alias_realm_checks_are_not_guessed_from_headers(self):
        for protocol in PROTOCOLS:
            key = self.key(platforms=['qoder'], models=['qoder/m'])
            with self.mock_transport(lambda _: httpx.Response(200, json=GOOD)):
                response = self.post(protocol, key=key, model='qoder/m')
            self.assertEqual(response.status_code, 200, response.text)
        key = self.key(models=['alias'], realm='global')
        db.set_setting('model_map', {'alias': 'global:m'})
        with self.mock_transport(lambda _: httpx.Response(200, json=GOOD)):
            response = self.post('chat/completions', key=key, model='alias')
        self.assertEqual(response.status_code, 200, response.text)

    def test_revocation_during_body_read_is_rechecked_before_provider_io(self):
        for protocol in PROTOCOLS:
            key = self.key()
            status, value, reads = self.drive('/v1/' + protocol,
                (('Authorization', 'Bearer ' + key['key']),),
                body=json.dumps(self.payload(protocol)).encode(),
                on_read=lambda: keysvc.update_key(key['id'], {'enabled': False}))
            self.assertEqual((status, reads, value['error']['code']), (403, 1, 'key_disabled'))
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM budget_reservations')['n'], 0)

    def test_preauth_never_double_consumes_rate_or_concurrency(self):
        for protocol in PROTOCOLS:
            key = self.key()
            rate = lambda _: (False, 1)
            with patch.object(gateway, '_rate_limited', side_effect=rate) as limited, \
                    patch.object(observability, 'admit_key', wraps=observability.admit_key) as admit, \
                    self.mock_transport(lambda _: httpx.Response(200, json=GOOD)):
                response = self.post(protocol, key=key)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((limited.call_count, admit.call_count), (1, 1))
        self.assertFalse(observability._key_slots)

    def test_header_precheck_does_not_bypass_model_constraint_or_resolve_only_on_cache(self):
        key = self.key(models=['allowed'])
        with patch.object(keysvc, 'resolve', wraps=keysvc.resolve) as resolve:
            response = self.post('messages', key=key, model='forbidden')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['error']['code'], 'model_not_allowed')
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM budget_reservations')['n'], 0)

    def test_slow_header_checks_are_bounded_and_cancellation_keeps_lease_until_thread_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture did not release header validator')
            return gateway._oai_error('invalid fixture credential', 401, 'authentication_error')
        async def run():
            async def app(scope, receive, send):
                self.fail('Rejected credentials must not enter the route')
            wrapper = observability.RequestAuditMiddleware(app)
            wrapper.maximum = 1
            async def drive():
                rows = []
                async def receive():
                    self.fail('Rejected or saturated header checks must not read the body')
                async def send(row):
                    rows.append(row)
                await wrapper({'type': 'http', 'method': 'POST', 'path': '/v1/messages',
                               'headers': [], 'query_string': b''}, receive, send)
                return rows
            task = asyncio.create_task(drive())
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                task.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(task.done())
                self.assertEqual(wrapper.preauth_active, 1)
                rows = await drive()
                self.assertEqual(rows[0]['status'], 503)
                self.assertEqual(wrapper.inflight_body_bytes, 0)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(wrapper.preauth_active, 0)
        with patch.object(gateway, 'preauthorize_body', side_effect=blocked):
            asyncio.run(run())
