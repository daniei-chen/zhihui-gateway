"""`/v1/messages/count_tokens` 必须对**任何** content block 都能算出数字。

## 为什么单列

2026-11-24 复查发现：`count_tokens` 走的是 `_text_of`，而 `_text_of` 只接受
`text` / `image`，遇到 `tool_use` / `tool_result` / `thinking` 一律抛
`UnsupportedContentError`，外层没有 try/except —— 结果是 **500 Internal Server
Error**。

这条路径的触发条件不是边角情况：Claude Code 这类客户端**每轮**发消息前都会先
调 `count_tokens`，而带工具调用历史的对话里必然出现 `tool_use` / `tool_result`。
也就是说「用过一次工具之后的第二轮」就再也发不出去了，而客户端只会看到
`Internal Server Error`，完全无法自救。

`_text_of` 本身**不该**放宽：它走在真实转译路径上，遇到不认识的块必须报错
（静默丢掉用户内容比报错更糟）。所以拆成两个函数：转译严格、估算容错。
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import budget, budgetowner, config, db, keysvc
from server.inputcontent import UnsupportedContentError
from server.routers import anthropic


#: 真实客户端会发过来的各种块。`text`/`image` 之外的每一个都曾经触发 500。
BLOCKS = {
    'text': {'type': 'text', 'text': 'synthetic counting only'},
    'tool_use': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read_file',
                 'input': {'path': '/tmp/synthetic'}},
    'tool_result': {'type': 'tool_result', 'tool_use_id': 'toolu_1',
                    'content': [{'type': 'text', 'text': 'file body'}]},
    'thinking': {'type': 'thinking', 'thinking': 'internal reasoning here',
                 'signature': 'sig'},
    'document': {'type': 'document',
                 'source': {'type': 'base64', 'media_type': 'application/pdf',
                            'data': 'JVBERi0xLjQK'}},
    'image': {'type': 'image',
              'source': {'type': 'base64', 'media_type': 'image/png',
                         'data': 'iVBORw0KGgo='}},
}


class CountTokensContentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR, db._conn)
        root = Path(self.temporary.name)
        config.DB_PATH = root / 'fixture.db'
        config.USERS_FILE = root / 'users.json'
        config.STATIC_DIR = root / 'no-static'
        config.USERS_FILE.write_text(json.dumps({'secret': 'synthetic-count-only', 'users': [
            {'username': 'admin', 'role': 'admin', 'sv': 1, 'pwd_hash': 'synthetic'}]}),
            encoding='utf-8')
        db._conn = None
        db.connect()
        budget.initialize()
        from server.main import app
        self.client = TestClient(app)
        self.key = keysvc.create_key(name='synthetic-count-content', quota=1000000)

    def tearDown(self):
        self.client.close()
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR, db._conn = self.previous
        self.temporary.cleanup()

    def count(self, messages):
        return self.client.post(
            '/v1/messages/count_tokens',
            json={'model': 'm', 'messages': messages},
            headers={'x-api-key': self.key['key']})

    def test_every_block_type_counts_instead_of_failing(self):
        for kind, block in BLOCKS.items():
            with self.subTest(kind=kind):
                response = self.count([{'role': 'user', 'content': [block]}])
                self.assertEqual(response.status_code, 200, response.text)
                self.assertGreater(response.json()['input_tokens'], 0, kind)

    def test_tool_history_round_trip_is_countable(self):
        """完整一轮带工具结果的对话 —— 这是客户端第二轮真实会发的东西。"""
        response = self.count([
            {'role': 'user', 'content': [BLOCKS['text']]},
            {'role': 'assistant', 'content': [BLOCKS['tool_use']]},
            {'role': 'user', 'content': [BLOCKS['tool_result']]},
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()['input_tokens'], 0)

    def test_tool_definitions_are_counted(self):
        response = self.client.post(
            '/v1/messages/count_tokens',
            json={'model': 'm',
                  'messages': [{'role': 'user', 'content': 'synthetic'}],
                  'tools': [{'name': 'read_file', 'description': 'read a file',
                             'input_schema': {'type': 'object',
                                              'properties': {'path': {'type': 'string'}}}}]},
            headers={'x-api-key': self.key['key']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()['input_tokens'], 0)

    def test_unknown_block_flavour_is_still_countable(self):
        """将来客户端加了新块类型，也不该让整个端点 500。"""
        response = self.count([{'role': 'user', 'content': [
            {'type': 'brand_new_block', 'payload': {'k': 'v'}}]}])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()['input_tokens'], 0)

    def test_estimation_failure_degrades_instead_of_500(self):
        """兜底：估算内部抛异常也只降级成偏大的数字。"""
        with patch.object(anthropic, '_tokens_of_content',
                          side_effect=RuntimeError('synthetic estimator failure')):
            response = self.count([{'role': 'user', 'content': 'synthetic'}])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()['input_tokens'], 0)

    def test_translation_path_stays_strict(self):
        """`_text_of` 不许跟着放宽 —— 转译时丢内容比报错更糟。"""
        for kind in ('tool_use', 'tool_result', 'thinking'):
            with self.subTest(kind=kind):
                with self.assertRaises(UnsupportedContentError):
                    anthropic._text_of([BLOCKS[kind]])

    def test_tool_content_costs_more_than_bare_text(self):
        """工具块不会白算：它应当比纯文本计数更高（偏大是安全方向）。"""
        plain = self.count([{'role': 'user', 'content': 'synthetic counting only'}])
        with_tool = self.count([{'role': 'user', 'content': [BLOCKS['tool_use']]}])
        self.assertGreater(with_tool.json()['input_tokens'],
                           plain.json()['input_tokens'])


if __name__ == '__main__':
    unittest.main()
