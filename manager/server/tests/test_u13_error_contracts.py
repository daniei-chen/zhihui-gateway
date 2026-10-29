"""U13 交付 3：流前 JSON 错误与流中 SSE 错误各自契约化，且字段对齐。

三条协议的错误必须能被客户端**机械处理**，不能靠读文案：

| 契约要素 | 流前（HTTP JSON） | 流中（SSE 帧） |
|---|---|---|
| 原 HTTP 状态 | 保留上游状态码 | 已开流 → 200（协议要求） |
| 上游业务 code | `error.code` | `error.code` / `response.failed.error.code` |
| `gateway_hint` | `error.gateway_hint` | 同 |
| 本端执行 ID | `request_id`（= `x-request-id` 响应头） | 帧内 `request_id` |
| 上游请求 ID | `upstream_request_id` | 帧内 `upstream_request_id` |

同时钉住两条「不得」：
  · 不得把上游拒绝包装成 200（流前必须是非 2xx JSON）；
  · 不得因为拿不到某个字段就编造它（缺失即不写）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, observability  # noqa: E402

UPSTREAM_ID = 'upstream-req-123'
REFUSAL = {
    'error': {'type': 'permission_error', 'code': 'no_healthy_account',
              'message': 'no healthy account available in pool',
              'gateway_hint': 'check /status or retry later'},
}


def _sse(obj) -> bytes:
    return b'data: ' + json.dumps(obj).encode() + b'\n\n'


def _client(*, status: int = 200, body: bytes = b'', headers: dict | None = None,
            send_delay: float = 0.0):
    class _Resp:
        def __init__(self) -> None:
            self.status_code = status
            self.headers = dict(headers if headers is not None else {
                'content-type': 'application/json', 'request-id': UPSTREAM_ID})
            self.text = body.decode('utf-8', 'replace')

        def json(self):
            return json.loads(body)

        async def aiter_bytes(self):
            if body:
                yield body

        async def aread(self):
            return body

        async def aclose(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def build_request(self, *a, **kw):
            return object()

        async def send(self, req, **kw):
            return _Resp()

        async def post(self, url, **kw):
            return _Resp()

        async def aclose(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return lambda *a, **k: _Client()


def _error_event(text: str) -> dict:
    """从 SSE 文本里取出第一条错误帧的 JSON。

    三种协议的错误形状不同，这里都认：
      · chat / messages：`{"error": {...}}`（messages 外面还有一层 `type`）
      · responses：`{"type":"response.failed","response":{...,"error":{...}}}`
    """
    for block in text.split('\n\n'):
        for line in block.splitlines():
            if not line.startswith('data:'):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get('error'), dict):
                return payload
            response = payload.get('response')
            if isinstance(response, dict) and isinstance(response.get('error'), dict):
                return payload
    raise AssertionError(f'没找到错误帧：{text[:400]}')


class SseErrorUnitTest(unittest.TestCase):
    """`observability.sse_error` 是三条协议共用的错误帧构造器。"""

    def test_default_keeps_existing_semantics(self) -> None:
        raw = observability.sse_error('boom')
        self.assertIn(b'\n\ndata: [DONE]\n\n', raw)
        payload = json.loads(raw.split(b'\n\n')[0][len(b'data: '):])
        self.assertEqual(payload['error']['code'], 'stream_incomplete',
                         '缺省 code 不能变（既有客户端按它判断可重试）')
        self.assertEqual(payload['error']['type'], 'api_error')
        self.assertNotIn('gateway_hint', payload['error'], '没 hint 就不写字段')

    def test_explicit_fields_are_preserved(self) -> None:
        raw = observability.sse_error('boom', code='no_healthy_account',
                                      hint='check /status',
                                      upstream_request_id=UPSTREAM_ID)
        payload = json.loads(raw.split(b'\n\n')[0][len(b'data: '):])
        self.assertEqual(payload['error']['code'], 'no_healthy_account')
        self.assertEqual(payload['error']['gateway_hint'], 'check /status')
        self.assertEqual(payload['upstream_request_id'], UPSTREAM_ID)

    def test_detail_context_is_used_when_arguments_are_absent(self) -> None:
        observability.set_stream_error_detail(code='11102', hint='h',
                                              upstream_request_id='u1')
        try:
            payload = json.loads(observability.sse_error('x').split(b'\n\n')[0][len(b'data: '):])
        finally:
            observability.set_stream_error_detail()
        self.assertEqual(payload['error']['code'], '11102')
        self.assertEqual(payload['error']['gateway_hint'], 'h')
        self.assertEqual(payload['upstream_request_id'], 'u1')

    def test_unsafe_values_are_dropped_not_echoed(self) -> None:
        raw = observability.sse_error('boom', code='bad\ncode', hint='   ',
                                      upstream_request_id='é' * 4)
        payload = json.loads(raw.split(b'\n\n')[0][len(b'data: '):])
        self.assertEqual(payload['error']['code'], 'stream_incomplete')
        self.assertNotIn('gateway_hint', payload['error'])
        self.assertNotIn('upstream_request_id', payload)


class ErrorContractEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = directory / 'u13-errors.db'
        config.USERS_FILE = directory / 'users.json'
        config.STATIC_DIR = directory / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def _key(self) -> str:
        return keysvc.create_key(name='u13-errors')['key']

    # ── 流前：必须是非 2xx JSON，且字段齐全 ──────────────────────────────
    def test_messages_prestream_keeps_status_code_hint_and_ids(self) -> None:
        with mock.patch.object(config, 'http_client',
                               _client(status=403, body=json.dumps(REFUSAL).encode())):
            r = self.c.post('/v1/messages',
                            json={'model': 'm', 'max_tokens': 8, 'stream': True,
                                  'messages': [{'role': 'user', 'content': 'hi'}]},
                            headers={'x-api-key': self._key(),
                                     'anthropic-version': '2023-06-01'})
        self.assertEqual(r.status_code, 403, '上游拒绝不得被包装成 200')
        body = r.json()
        self.assertEqual(body['type'], 'error')
        self.assertEqual(body['error']['code'], 'no_healthy_account')
        self.assertEqual(body['error']['gateway_hint'], 'check /status or retry later')
        self.assertEqual(body['upstream_request_id'], UPSTREAM_ID)
        self.assertEqual(body['request_id'], r.headers['x-request-id'])

    def test_responses_prestream_keeps_status_code_hint_and_ids(self) -> None:
        with mock.patch.object(config, 'http_client',
                               _client(status=503, body=json.dumps(REFUSAL).encode())):
            r = self.c.post('/v1/responses',
                            json={'model': 'm', 'input': 'hi', 'stream': True},
                            headers={'Authorization': f'Bearer {self._key()}'})
        self.assertEqual(r.status_code, 503, '上游拒绝不得被包装成 200')
        body = r.json()
        self.assertEqual(body['error']['code'], 'no_healthy_account')
        self.assertEqual(body['error']['gateway_hint'], 'check /status or retry later')
        self.assertEqual(body['upstream_request_id'], UPSTREAM_ID)
        self.assertEqual(body['request_id'], r.headers['x-request-id'])
        self.assertNotIn('text/event-stream', r.headers.get('content-type', ''))

    # ── 流中：SSE 帧里同样要有 code / hint / 两个 ID ──────────────────────
    def test_messages_midstream_error_frame_is_contracted(self) -> None:
        upstream = (_sse({'choices': [{'delta': {'content': 'partial'},
                                       'finish_reason': None}]})
                    + _sse(REFUSAL))
        with mock.patch.object(config, 'http_client', _client(body=upstream)):
            r = self.c.post('/v1/messages',
                            json={'model': 'm', 'max_tokens': 8, 'stream': True,
                                  'messages': [{'role': 'user', 'content': 'hi'}]},
                            headers={'x-api-key': self._key(),
                                     'anthropic-version': '2023-06-01'})
        self.assertEqual(r.status_code, 200)
        payload = _error_event(r.content.decode())
        self.assertEqual(payload['error']['code'], 'no_healthy_account')
        self.assertEqual(payload['error']['gateway_hint'], 'check /status or retry later')
        self.assertEqual(payload['request_id'], r.headers['x-request-id'])
        self.assertEqual(payload['upstream_request_id'], UPSTREAM_ID)
        self.assertLess(r.content.decode().find('event: error'),
                        r.content.decode().find('event: message_stop'),
                        'error 必须在 message_stop 之前')

    def test_responses_midstream_error_frame_is_contracted(self) -> None:
        upstream = (_sse({'choices': [{'delta': {'content': 'partial'},
                                       'finish_reason': None}]})
                    + _sse(REFUSAL))
        with mock.patch.object(config, 'http_client', _client(body=upstream)):
            r = self.c.post('/v1/responses',
                            json={'model': 'm', 'input': 'hi', 'stream': True},
                            headers={'Authorization': f'Bearer {self._key()}'})
        self.assertEqual(r.status_code, 200)
        text = r.content.decode()
        self.assertIn('response.failed', text)
        payload = _error_event(text)
        self.assertEqual(payload['type'], 'response.failed')
        self.assertEqual(payload['response']['error']['code'], 'no_healthy_account')
        self.assertEqual(payload['response']['error']['gateway_hint'],
                         'check /status or retry later')
        self.assertEqual(payload['request_id'], r.headers['x-request-id'])
        self.assertEqual(payload['upstream_request_id'], UPSTREAM_ID)

    def test_chat_midstream_error_frame_carries_ids(self) -> None:
        """chat 走裸透传：只有「流不完整」时才由网关补错误帧，同样要带 ID。"""
        upstream = _sse({'choices': [{'delta': {'content': 'partial'},
                                      'finish_reason': None}]})
        with mock.patch.object(config, 'http_client', _client(body=upstream)):
            r = self.c.post('/v1/chat/completions',
                            json={'model': 'm', 'stream': True,
                                  'messages': [{'role': 'user', 'content': 'hi'}]},
                            headers={'Authorization': f'Bearer {self._key()}'})
        self.assertEqual(r.status_code, 200)
        payload = _error_event(r.content.decode())
        self.assertEqual(payload['error']['code'], 'stream_incomplete',
                         '没有上游业务 code 时保持既有语义')
        self.assertEqual(payload['request_id'], r.headers['x-request-id'])
        self.assertEqual(payload['upstream_request_id'], UPSTREAM_ID)

    def test_missing_upstream_id_is_not_fabricated(self) -> None:
        upstream = _sse(REFUSAL)
        with mock.patch.object(config, 'http_client',
                               _client(body=upstream, headers={
                                   'content-type': 'text/event-stream'})):
            r = self.c.post('/v1/responses',
                            json={'model': 'm', 'input': 'hi', 'stream': True},
                            headers={'Authorization': f'Bearer {self._key()}'})
        payload = _error_event(r.content.decode())
        self.assertNotIn('upstream_request_id', payload,
                         '上游没给 ID 就不能编一个出来')
        self.assertEqual(payload['response']['error']['code'], 'no_healthy_account')


if __name__ == '__main__':
    unittest.main()
