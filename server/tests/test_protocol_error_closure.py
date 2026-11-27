"""Three real ASGI entrances share HTTP error/backoff and no-replay boundaries."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import budgetowner, config, db, deadline, keysvc, observability, protocolerrors
from server.routers import gateway
from server.qtgcore.errors import install_handlers


PATHS = ('/v1/chat/completions', '/v1/messages', '/v1/responses')
UPSTREAM_ID = 'fixture-provider-request'
OK = {'id': 'fixture-answer', 'object': 'chat.completion', 'model': 'm',
      'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                   'finish_reason': 'stop'}],
      'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}


def payload(path, stream=False):
    if path.endswith('/responses'):
        return {'model': 'm', 'input': 'hi', 'max_output_tokens': 8, 'stream': stream}
    return {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 8, 'stream': stream}


def sse(value):
    return ('data: ' + json.dumps(value) + '\n\n').encode()


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure = chunks, failure
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.failure is not None:
            raise self.failure

    async def aclose(self):
        self.closed = True


class PublicHandlerTests(unittest.TestCase):
    def test_http_exception_keeps_status_auth_challenge_and_retry_headers(self):
        for path in (*PATHS, '/api/fixture'):
            for status in (401, 429, 503):
                with self.subTest(path=path, status=status):
                    app = FastAPI()
                    install_handlers(app)

                    async def fail():
                        raise HTTPException(status, 'fixture denial', headers={
                            'Retry-After': '7', 'WWW-Authenticate': 'Bearer'})

                    app.add_api_route(path, fail, methods=['POST'])
                    result = TestClient(app).post(path)
                    self.assertEqual(result.status_code, status)
                    self.assertEqual(result.headers['retry-after'], '7')
                    self.assertEqual(result.headers['www-authenticate'], 'Bearer')
                    self.assertEqual(result.json()['error']['retry_after_seconds'], 7)
                    self.assertEqual(result.json()['detail'], 'fixture denial')
                    self.assertEqual(result.json()['error']['type'],
                                     protocolerrors.error_type(status))
                    if path.endswith('/messages'):
                        self.assertEqual(result.json()['type'], 'error')

    def test_invalid_retry_header_never_becomes_a_fabricated_zero_delay(self):
        for value in (None, '', '-1', '7201', '1,2', 'soon', True,
                      'Wed, 21 Oct 2026 07:28:00 GMT'):
            with self.subTest(value=value):
                self.assertIsNone(protocolerrors.retry_after_seconds({'retry-after': value}))
        for value, expected in (('0', 0), (' 30 ', 30), ('7200', 7200)):
            self.assertEqual(protocolerrors.retry_after_seconds({'Retry-After': value}), expected)

    def test_handler_validation_keeps_safe_details_and_native_messages_envelope(self):
        app = FastAPI()
        install_handlers(app)

        async def validate(count: int):
            return {'count': count}

        app.add_api_route('/v1/messages', validate, methods=['POST'])
        result = TestClient(app).post('/v1/messages?count=secret-fixture-input')
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()['type'], 'error')
        self.assertEqual(result.json()['error']['code'], 'validation_error')
        self.assertNotIn('secret-fixture-input', result.text)
        self.assertTrue(result.json()['detail'])


class ProtocolErrorEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.original = config.DB_PATH, config.USERS_FILE, config.STATIC_DIR
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = (
            root / 'protocol-errors.db', root / 'users.json', root / 'no-static')
        config.USERS_FILE.write_text(json.dumps({'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': []}))
        db._conn = None
        db.connect()
        from server.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls.original
        # 预算归属锁的文件句柄是模块级全局的，不关就删不掉临时目录：
        # Windows 上这是 WinError 32，POSIX 允许删打开中的文件所以从未暴露。
        budgetowner.close()
        cls.directory.cleanup()

    def setUp(self):
        self.key = keysvc.create_key(name='protocol-error-fixture')['key']

    def headers(self):
        return {'Authorization': 'Bearer ' + self.key, 'x-api-key': self.key}

    def post_upstream(self, path, response_factory, *, stream=False):
        calls = []

        def upstream(request):
            if request.method != 'POST':
                return httpx.Response(200, json={'data': []})
            calls.append(request)
            return response_factory()

        with mock.patch.object(config, 'http_client', lambda *a, **kw:
                               httpx.AsyncClient(transport=httpx.MockTransport(upstream))):
            result = self.client.post(path, json=payload(path, stream), headers=self.headers())
        return result, calls

    def assert_error(self, result, path, status, *, code=None, wait=None):
        self.assertEqual(result.status_code, status, result.text[:500])
        self.assertTrue(result.headers['content-type'].startswith('application/json'))
        body = result.json()
        self.assertIsInstance(body['error']['message'], str)
        self.assertTrue(body['error']['type'])
        self.assertEqual(body['request_id'], result.headers['x-request-id'])
        if path.endswith('/messages') or path.endswith('/count_tokens'):
            self.assertEqual(body['type'], 'error')
        if code is not None:
            self.assertEqual(body['error']['code'], code)
        if wait is None:
            self.assertNotIn('retry-after', result.headers)
            self.assertNotIn('retry_after_seconds', body['error'])
        else:
            self.assertEqual(result.headers['retry-after'], str(wait))
            self.assertEqual(body['error']['retry_after_seconds'], wait)
        return body

    def test_missing_and_invalid_credentials_share_native_status_and_codes(self):
        with mock.patch.object(config, 'http_client') as upstream:
            for path in PATHS:
                for token, code in (('', 'missing_api_key'), ('invalid-fixture', 'invalid_api_key')):
                    with self.subTest(path=path, code=code):
                        headers = {'Authorization': 'Bearer ' + token, 'x-api-key': token} if token else {}
                        result = self.client.post(path, json=payload(path), headers=headers)
                        body = self.assert_error(result, path, 401, code=code)
                        self.assertEqual(body['error']['type'], 'authentication_error')
            upstream.assert_not_called()

    def test_actual_oversize_body_is_rejected_before_provider(self):
        with mock.patch.object(gateway, 'max_body_bytes', return_value=64), \
                mock.patch.object(config, 'http_client') as upstream:
            for path in PATHS:
                with self.subTest(path=path):
                    result = self.client.post(path, content=b'{' + b'x' * 256,
                                              headers=self.headers())
                    body = self.assert_error(result, path, 413, code='payload_too_large')
                    if path.endswith('/messages'):
                        self.assertEqual(body['error']['type'], 'request_too_large')
            upstream.assert_not_called()

    def test_outer_platform_rejection_keeps_native_envelope_and_request_id(self):
        with mock.patch.object(config, 'http_client') as upstream:
            for path in PATHS:
                for prefix, hint, code in (('', 'unknown-fixture', 'invalid_platform'),
                                           ('/qoder', 'trae', 'platform_conflict')):
                    with self.subTest(path=path, code=code):
                        result = self.client.post(prefix + path, json=payload(path),
                            headers={**self.headers(), 'x-qtg-platform': hint})
                        self.assert_error(result, path, 400, code=code)
            upstream.assert_not_called()

    def test_key_concurrency_and_rate_rejections_keep_both_backoff_channels(self):
        cases = (('key_concurrency_exceeded', 1,
                  mock.patch.object(observability, 'admit_key', return_value=False)),
                 ('rate_limit_exceeded', gateway.RATE_WINDOW,
                  mock.patch.object(gateway, '_rate_limited', return_value=(True, 999))))
        for code, wait, limiter in cases:
            with limiter, mock.patch.object(config, 'http_client') as upstream:
                for path in PATHS:
                    with self.subTest(path=path, code=code):
                        result = self.client.post(path, json=payload(path), headers=self.headers())
                        body = self.assert_error(result, path, 429, code=code, wait=wait)
                        self.assertEqual(body['error']['type'], 'rate_limit_error')
                upstream.assert_not_called()

    def test_maintenance_drain_preserves_native_envelope_and_backoff(self):
        from server import drain
        with mock.patch.object(drain, 'is_draining', return_value=True), \
                mock.patch.object(config, 'http_client') as upstream:
            for path in PATHS:
                with self.subTest(path=path):
                    result = self.client.post(path, json=payload(path), headers=self.headers())
                    self.assert_error(result, path, 503, code='gateway_draining', wait=1)
                    self.assertEqual(result.headers['connection'], 'close')
            upstream.assert_not_called()

    def test_route_body_failures_do_not_collapse_408_415_or_503_into_400(self):
        for path in (*PATHS, '/v1/messages/count_tokens'):
            for status in (408, 413, 415, 503):
                with self.subTest(path=path, status=status):
                    async def denied(_request):
                        return None, gateway._oai_error('fixture body refusal', status,
                            protocolerrors.error_type(status), 'fixture_body_refusal',
                            retry_after=1 if status == 503 else None)
                    with mock.patch.object(gateway, '_read_json_body', denied), \
                            mock.patch.object(config, 'http_client') as upstream:
                        result = self.client.post(path, json=payload(path), headers=self.headers())
                    self.assert_error(result, path, status, code='fixture_body_refusal',
                                      wait=1 if status == 503 else None)
                    upstream.assert_not_called()

    def test_upstream_http_matrix_preserves_status_code_ids_and_retry_after(self):
        for path in PATHS:
            for stream in (False, True):
                for status in (400, 401, 403, 404, 413, 429, 500, 503):
                    with self.subTest(path=path, stream=stream, status=status):
                        rejected = {'error': {'message': 'fixture provider refusal',
                            'code': 'fixture_provider_refusal', 'gateway_hint': 'fixture advice',
                            'retry_after_seconds': 99}}
                        result, calls = self.post_upstream(path, lambda: httpx.Response(status,
                            json=rejected, headers={'Retry-After': '17', 'request-id': UPSTREAM_ID}),
                            stream=stream)
                        body = self.assert_error(result, path, status,
                            code='fixture_provider_refusal', wait=17)
                        self.assertEqual(body['error']['gateway_hint'], 'fixture advice')
                        self.assertEqual(body['upstream_request_id'], UPSTREAM_ID)
                        self.assertEqual(len(calls), 1, 'unmarked rejection must never replay')

    def test_non_json_and_invalid_retry_errors_still_use_protocol_json(self):
        for path in PATHS:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    result, calls = self.post_upstream(path, lambda: httpx.Response(503,
                        content=b'fixture plain provider refusal', headers={
                            'Content-Type': 'text/plain', 'Retry-After': '7201'}), stream=stream)
                    body = self.assert_error(result, path, 503)
                    self.assertIn('fixture plain provider refusal', body['error']['message'])
                    self.assertEqual(len(calls), 1)

    def test_known_http_rejection_survives_unreadable_timed_out_or_huge_error_body(self):
        cases = ((httpx.ReadError('private transport detail'), [b'{'], 'upstream_error_body_unreadable'),
                 (deadline.BudgetExceeded('stream_idle', 'private detail'), [b'{'], 'upstream_error_body_timeout'),
                 (None, [b'x' * 70000], 'upstream_error_body_too_large'))
        for path in PATHS:
            for stream in (False, True):
                for failure, chunks, code in cases:
                    with self.subTest(path=path, stream=stream, code=code):
                        transport = ScriptedStream(chunks, failure)
                        result, calls = self.post_upstream(path, lambda: httpx.Response(429,
                            stream=transport, headers={'Retry-After': '13'}), stream=stream)
                        self.assert_error(result, path, 429, code=code, wait=13)
                        self.assertNotIn('private', result.text)
                        self.assertLess(len(result.content), 4096)
                        self.assertTrue(transport.closed)
                        self.assertEqual(len(calls), 1)

    def test_transport_failure_before_headers_has_same_backoff_without_private_details(self):
        def fail():
            raise httpx.ReadError('private fixture upstream URL or credential')
        for path in PATHS:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    result, calls = self.post_upstream(path, fail, stream=stream)
                    self.assert_error(result, path, 502, code='upstream_unavailable', wait=5)
                    self.assertEqual(result.headers['x-upstream-blip'], '1')
                    self.assertNotIn('private fixture', result.text)
                    self.assertEqual(len(calls), 1)

    def test_parser_memory_refusal_is_503_before_any_success_body(self):
        original = gateway._response_json_async

        async def denied(response, **kwargs):
            state = observability.current.get()
            def full(_size):
                raise observability.CapacityError('fixture memory ceiling')
            state.reserve_output_body = full
            return await original(response, **kwargs)

        for path in PATHS:
            with self.subTest(path=path), mock.patch.object(gateway, '_response_json_async', denied):
                result, calls = self.post_upstream(path, lambda: httpx.Response(200, json=OK))
                self.assert_error(result, path, 503, code='gateway_capacity_exceeded', wait=1)
                self.assertNotIn('fixture-answer', result.text)
                self.assertEqual(len(calls), 1)

    def test_partial_output_then_provider_error_or_read_failure_never_replays(self):
        first = sse({'choices': [{'index': 0,
            'delta': {'role': 'assistant', 'content': 'visible-partial'}, 'finish_reason': None}]})
        failure_event = sse({'error': {'message': 'fixture midstream refusal',
            'code': 'fixture_midstream_refusal', 'type': 'api_error'}})
        for path in PATHS:
            for read_failure in (False, True):
                with self.subTest(path=path, read_failure=read_failure):
                    transport = ScriptedStream([first] if read_failure else [first, failure_event],
                        httpx.ReadError('fixture read failed') if read_failure else None)
                    result, calls = self.post_upstream(path, lambda: httpx.Response(200,
                        stream=transport, headers={'content-type': 'text/event-stream'}), stream=True)
                    self.assertEqual(result.status_code, 200)
                    self.assertIn('visible-partial', result.text)
                    self.assertEqual(len(calls), 1, 'partial output must never be replayed')
                    self.assertTrue(transport.closed)
                    if path.endswith('/messages'):
                        self.assertIn('event: error', result.text)
                        self.assertNotIn('event: message_stop', result.text)
                    elif path.endswith('/responses'):
                        self.assertIn('event: response.failed', result.text)
                        self.assertNotIn('event: response.completed', result.text)
                    else:
                        self.assertIn('"error"', result.text)
                    self.assertNotIn('retry-after', result.headers)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_rejection_does_not_swallow_cancel_as_diagnostic_json(self):
        stream = ScriptedStream([], asyncio.CancelledError())
        response = httpx.Response(429, stream=stream)
        with self.assertRaises(asyncio.CancelledError):
            await deadline.read_error_body(response, deadline.Deadline(deadline.request_budgets(10)),
                                           preserve_status=True)
        await response.aclose()


if __name__ == '__main__':
    unittest.main()
