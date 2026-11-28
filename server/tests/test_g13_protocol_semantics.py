"""G-13（SWO-64）：三条协议语义错——客户端会重试、会拿到空答案、会多付钱。

## D-20 `server/routers/anthropic.py:818`

Messages 非流式只认 `isinstance(content, str)`。OpenAI 兼容上游的 `content`
还有第二种合法形态：`[{'type': 'text', 'text': '...'}]`（多段正文）。数组被
**静默丢弃** → 客户端拿到 `content: []` + `end_turn`：看不到答案，也不会重试。

## D-21 `server/routers/responses.py:1945`

Responses 非流式在「`finish_reason=length` + 什么都没交付」时照样回 200
（`status: incomplete` + 空 output），而 Chat 同输入回 400。客户端把 200 当
正常完成、原样重试，每次重试都计费（`observability.budget_exhausted_response`
本来就是三条协议共用的判据，只是 responses 没接）。

## D-22 `server/routers/anthropic.py:651`

`thinking.type = 'disabled'`（客户端明确说不要思考）仍会按残留的
`budget_tokens` 发 `reasoning_effort`。上游收到档位就思考：用户拿到没请求过的
思考，还按 token 付费。
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

from server import config, db, keysvc, observability, security  # noqa: E402
from server.routers import anthropic, responses as R  # noqa: E402


class MessagesContentShapeTests(unittest.TestCase):
    """D-20：`content` 的两种合法形态都必须交付成文本块。"""

    def test_content_parts_array_becomes_text_block(self):
        out = anthropic.to_anthropic_response({
            'choices': [{'message': {'role': 'assistant', 'content': [
                {'type': 'text', 'text': '你好，'},
                {'type': 'text', 'text': '这是分段正文。'},
            ]}, 'finish_reason': 'stop'}],
        }, 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': '你好，这是分段正文。'}])
        self.assertEqual(out['stop_reason'], 'end_turn')

    def test_string_content_is_unchanged(self):
        out = anthropic.to_anthropic_response({
            'choices': [{'message': {'role': 'assistant', 'content': '普通答案'},
                         'finish_reason': 'stop'}],
        }, 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': '普通答案'}])

    def test_parts_without_any_text_fail_loudly(self):
        """只有图片块之类的数组：Anthropic 的 text 块表达不了，不能回空。"""
        with self.assertRaises(ValueError):
            anthropic.to_anthropic_response({
                'choices': [{'message': {'role': 'assistant', 'content': [
                    {'type': 'image_url', 'image_url': {'url': 'http://x/y.png'}},
                ]}, 'finish_reason': 'stop'}],
            }, 'claude-x')

    def test_budget_exhausted_uses_parts_text_too(self):
        """分段正文也算交付：不能因为形态是数组就误判成「预算耗尽」。"""
        out = anthropic.to_anthropic_response({
            'choices': [{'message': {'role': 'assistant', 'content': [
                {'type': 'text', 'text': '答案'},
            ]}, 'finish_reason': 'length'}],
        }, 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': '答案'}])
        self.assertEqual(out['stop_reason'], 'max_tokens')


class ThinkingDisabledTests(unittest.TestCase):
    """D-22：`thinking.type=disabled` 不发任何档位。"""

    def test_disabled_ignores_leftover_budget_tokens(self):
        self.assertIsNone(anthropic._reasoning_effort(
            {'thinking': {'type': 'disabled', 'budget_tokens': 8192}}))

    def test_disabled_beats_output_config_effort(self):
        self.assertIsNone(anthropic._reasoning_effort(
            {'thinking': {'type': 'disabled'},
             'output_config': {'effort': 'high'}}))

    def test_enabled_still_maps_budget_to_effort(self):
        self.assertEqual(anthropic._reasoning_effort(
            {'thinking': {'type': 'enabled', 'budget_tokens': 32768}}), 'high')

    def test_output_config_effort_still_wins_without_disabled(self):
        self.assertEqual(anthropic._reasoning_effort(
            {'output_config': {'effort': 'max'}}), 'max')


class _UpstreamResponse:
    """上游 HTTP 的替身：`buffered_post` 只需要 headers/status/aiter_bytes。"""

    status_code = 200
    headers = {'content-type': 'application/json'}

    def __init__(self, raw: bytes):
        self.raw = raw

    async def aiter_bytes(self):
        yield self.raw

    async def aclose(self):
        return None


class _Client:
    """只把上游 HTTP 换成本地对象；请求/响应转换全走真实代码。"""

    def __init__(self, payload: dict):
        self.raw = json.dumps(payload).encode()

    def build_request(self, method, url, **kwargs):
        return object()

    async def send(self, request, stream=True):
        return _UpstreamResponse(self.raw)

    async def aclose(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class ResponsesBudgetExhaustedEndpointTests(unittest.TestCase):
    """D-21：非流式 Responses 预算耗尽必须回 400，不能回 200 incomplete。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self._orig_db, self._orig_users = config.DB_PATH, config.USERS_FILE
        self.addCleanup(self._restore)
        config.DB_PATH = Path(self._tmp.name) / 'g13.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        self.token = keysvc.create_key('g13')['key']

    def _restore(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = self._orig_db, self._orig_users

    def _post(self, payload: dict):
        upstream = {'base_url': 'http://mock-upstream.invalid',
                    'api_key': 'test-only', 'is_default': False}
        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client(payload)), \
             mock.patch.object(R.upstreamsvc, 'resolve_for_key', return_value=upstream):
            return self.client.post('/v1/responses',
                                    headers={'Authorization': f'Bearer {self.token}'},
                                    json={'model': 'glm-5.2', 'input': 'hi'})

    def test_budget_exhausted_is_400_not_200_incomplete(self):
        body = self._post({
            'choices': [{'message': {'role': 'assistant', 'content': ''},
                         'finish_reason': 'length'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 0},
        })
        self.assertEqual(body.status_code, 400, body.text)
        detail = body.json()
        self.assertEqual(detail['error']['code'], 'output_budget_exhausted')
        self.assertEqual(detail['error']['message'], observability.BUDGET_EXHAUSTED_MESSAGE)

    def test_normal_answer_still_200_completed(self):
        """对照组：正常回答不能因为这条判定被误伤。"""
        body = self._post({
            'choices': [{'message': {'role': 'assistant', 'content': '答案'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 2},
        })
        self.assertEqual(body.status_code, 200, body.text)
        payload = body.json()
        self.assertEqual(payload['status'], 'completed')
        self.assertEqual(payload['output_text'], '答案')


if __name__ == '__main__':
    unittest.main()
