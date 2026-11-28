"""Messages 非流式：上游「正常结束」却没给正文时，必须响亮报错。

## 问题

`to_anthropic_response` 原来是「有正文就放一个 text 块，没有就什么都不放」。
`content` 为 `[]` / `['']` / `null` 配 `finish_reason=stop` 时，客户端拿到的是
`content: []` + `end_turn` —— **一个看起来完全正常的空答案**，症状与 D-20 相同
（D-20 是丢了数组形态的正文，这里是上游真的什么都没给）。

`_content_text` 的 docstring 写的是「数组里没有可交付的正文 → 报错而不是回空」，
但实现里 `if texts or not value` 把空数组显式放行了。本模块把口径统一到
**消息层**：`finish=stop` + 无文本 + 无工具调用 = 失败。

## 为什么判据放在消息层、而不是 `_content_text` 里

只带 `tool_calls` 的消息 `content` 本来就是 `null` 或 `[]`（各上游都这么发），
在文本提取那一层报错会把**合法的纯工具调用**变成 502。所以 `_content_text`
对空数组返回空串，由 `to_anthropic_response` 结合 `finish_reason` 与
`tool_calls` 判定——`test_tool_only_message_is_not_empty` 与
`test_empty_content_with_tool_calls_is_not_empty` 守着这条边界。

调用方核查（本 PR 要求的证据）：`to_anthropic_response` 在仓库里只有
**一个**构造点（`server/routers/anthropic.py:1389`，非流式 Messages 入口），
两侧分别是 `_valid_chat_response` 与 `JSONResponse`；抛 `ValueError` 会落到
该调用点的 `except Exception`，回 **502 `upstream_invalid_body`**，
不是 200 空答案。
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

from server import config, db, keysvc, security  # noqa: E402
from server.routers import anthropic  # noqa: E402


def _message(content, finish='stop', **extra):
    message = {'role': 'assistant', 'content': content}
    message.update(extra)
    return {'choices': [{'message': message, 'finish_reason': finish}]}


class EmptyAnswerTests(unittest.TestCase):
    def test_empty_array_is_an_error(self):
        with self.assertRaises(ValueError):
            anthropic.to_anthropic_response(_message([]), 'claude-x')

    def test_array_of_blank_strings_is_an_error(self):
        """`['']` 与 `[]` 等价：客户端看到的都是空答案。"""
        with self.assertRaises(ValueError):
            anthropic.to_anthropic_response(_message([{'type': 'text', 'text': ''}]), 'claude-x')

    def test_null_content_is_an_error(self):
        with self.assertRaises(ValueError):
            anthropic.to_anthropic_response(_message(None), 'claude-x')

    def test_whitespace_only_text_is_an_error(self):
        with self.assertRaises(ValueError):
            anthropic.to_anthropic_response(_message('   \n'), 'claude-x')

    def test_tool_only_message_is_not_empty(self):
        """纯工具调用是合法的空正文，不能被这条判据误伤。"""
        out = anthropic.to_anthropic_response(_message(
            None, finish='tool_calls',
            tool_calls=[{'id': 'c1', 'function': {'name': 'f', 'arguments': '{}'}}]), 'claude-x')
        self.assertEqual(out['stop_reason'], 'tool_use')
        self.assertEqual([block['type'] for block in out['content']], ['tool_use'])

    def test_empty_content_with_tool_calls_is_not_empty(self):
        """上游用 `content: []`（而不是 null）配工具调用时同样合法。"""
        out = anthropic.to_anthropic_response(_message(
            [], finish='stop',
            tool_calls=[{'id': 'c1', 'function': {'name': 'f', 'arguments': '{}'}}]), 'claude-x')
        self.assertEqual([block['type'] for block in out['content']], ['tool_use'])

    def test_normal_answer_is_unchanged(self):
        out = anthropic.to_anthropic_response(_message('答案'), 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': '答案'}])
        self.assertEqual(out['stop_reason'], 'end_turn')

    def test_missing_finish_reason_stays_tolerated(self):
        """`finish_reason` 缺失（空上游体）仍按原样容忍，不当成「空答案」。"""
        out = anthropic.to_anthropic_response({}, 'claude-x')
        self.assertEqual(out['content'], [])


class _UpstreamResponse:
    status_code = 200
    headers = {'content-type': 'application/json'}

    def __init__(self, raw: bytes):
        self.raw = raw

    async def aiter_bytes(self):
        yield self.raw

    async def aclose(self):
        return None


class _Client:
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


class EmptyAnswerEndpointTests(unittest.TestCase):
    """客户端看到的是 502，不是一个 200 的空答案。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self._orig_db, self._orig_users = config.DB_PATH, config.USERS_FILE
        self.addCleanup(self._restore)
        config.DB_PATH = Path(self._tmp.name) / 'empty.db'
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
        self.token = keysvc.create_key('empty-answer')['key']

    def _restore(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = self._orig_db, self._orig_users

    def _post(self, payload: dict):
        from server.routers import responses as R
        upstream = {'base_url': 'http://mock-upstream.invalid',
                    'api_key': 'test-only', 'is_default': False}
        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client(payload)), \
             mock.patch.object(R.upstreamsvc, 'resolve_for_key', return_value=upstream):
            return self.client.post('/v1/messages',
                                    headers={'Authorization': f'Bearer {self.token}',
                                             'x-api-key': self.token},
                                    json={'model': 'claude-x', 'max_tokens': 64,
                                          'messages': [{'role': 'user', 'content': 'hi'}]})

    def test_empty_answer_becomes_502_not_empty_200(self):
        body = self._post(_message(''))
        self.assertEqual(body.status_code, 502, body.text)
        self.assertEqual(body.json()['error']['type'], 'api_error')
        self.assertNotIn('end_turn', body.text)

    def test_normal_answer_still_200(self):
        body = self._post(_message('答案'))
        self.assertEqual(body.status_code, 200, body.text)
        self.assertEqual(body.json()['content'], [{'type': 'text', 'text': '答案'}])


if __name__ == '__main__':
    unittest.main()
