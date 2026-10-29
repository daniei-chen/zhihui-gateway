"""U13 交付 2：Responses 的 `text.format` 映射矩阵与「不支持就显式拒绝」。

改动前 `text.format` 被**静默丢弃**：客户端要 JSON，拿到的却是自由文本，而且
连 400 都没有 —— 客户端只能靠「解析失败」自己猜。本文件钉住三条结论：

  · `json_object` / `json_schema` 映射成上游 Chat Completions 的
    `response_format`（而不是丢弃）；
  · 结构非法或 type 不支持时，在**转发前**回 400 + 稳定 code
    （`unsupported_text_format` / `invalid_text_format`）；
  · 没要求结构化输出时**不加**任何字段（不凭空塞 `{'type':'text'}`）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc  # noqa: E402
from server.routers import responses as R  # noqa: E402

_OK = {
    'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'm',
    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"a":1}'},
                 'finish_reason': 'stop'}],
    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
}


class MappingMatrixTest(unittest.TestCase):
    """矩阵：缺失 / text / json_object / json_schema / 其它。"""

    def test_absent_format_adds_nothing(self) -> None:
        self.assertIsNone(R.text_format_to_response_format({'input': 'x'}))
        out = R.to_chat_request({'model': 'm', 'input': 'x'})
        self.assertNotIn('response_format', out, '没要求结构化输出就不该塞字段')

    def test_text_format_is_plain_text(self) -> None:
        body = {'input': 'x', 'text': {'format': {'type': 'text'}}}
        self.assertIsNone(R.text_format_to_response_format(body))
        self.assertNotIn('response_format', R.to_chat_request(
            {'model': 'm', 'input': 'x', 'text': {'format': {'type': 'text'}}}))

    def test_json_object_maps_to_response_format(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': 'x',
                                 'text': {'format': {'type': 'json_object'}}})
        self.assertEqual(out['response_format'], {'type': 'json_object'})

    def test_json_schema_maps_all_supported_fields(self) -> None:
        schema = {'type': 'object', 'properties': {'a': {'type': 'integer'}},
                  'required': ['a'], 'additionalProperties': False}
        out = R.to_chat_request({'model': 'm', 'input': 'x', 'text': {'format': {
            'type': 'json_schema', 'name': 'answer', 'schema': schema,
            'description': '结构化答案', 'strict': True}}})
        self.assertEqual(out['response_format'], {
            'type': 'json_schema',
            'json_schema': {'name': 'answer', 'schema': schema,
                            'description': '结构化答案', 'strict': True}})

    def test_json_schema_minimal_is_accepted(self) -> None:
        out = R.text_format_to_response_format({'text': {'format': {
            'type': 'json_schema', 'name': 'n', 'schema': {'type': 'object'}}}})
        self.assertEqual(out['json_schema'], {'name': 'n', 'schema': {'type': 'object'}})

    def test_unsupported_type_is_rejected_not_dropped(self) -> None:
        with self.assertRaises(R.StructuredOutputError) as ctx:
            R.text_format_to_response_format(
                {'text': {'format': {'type': 'grammar', 'definition': 'start: /x/'}}})
        self.assertEqual(ctx.exception.code, 'unsupported_text_format')

    def test_malformed_shapes_are_rejected(self) -> None:
        cases = (
            {'text': 'json'},
            {'text': {'format': 'json_object'}},
            {'text': {'format': {'type': 42}}},
            {'text': {'format': {'type': 'json_schema', 'schema': {'type': 'object'}}}},
            {'text': {'format': {'type': 'json_schema', 'name': 'n'}}},
            {'text': {'format': {'type': 'json_schema', 'name': '  ',
                                 'schema': {'type': 'object'}}}},
            {'text': {'format': {'type': 'json_schema', 'name': 'n', 'schema': {}}}},
        )
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(R.StructuredOutputError) as ctx:
                    R.text_format_to_response_format(body)
                self.assertEqual(ctx.exception.code, 'invalid_text_format')

    def test_conversion_error_surfaces_through_to_chat_request(self) -> None:
        with self.assertRaises(R.StructuredOutputError):
            R.to_chat_request({'model': 'm', 'input': 'x',
                               'text': {'format': {'type': 'grammar'}}})


class EndpointStructuredOutputTest(unittest.TestCase):
    """真实 HTTP：不支持 → 转发前 400（稳定 code）；支持 → 真的到上游。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = directory / 'u13-structured.db'
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
        return keysvc.create_key(name='u13-structured')['key']

    def _post(self, payload: dict):
        return self.c.post('/v1/responses', json=payload,
                           headers={'Authorization': f'Bearer {self._key()}'})

    def test_unsupported_format_is_rejected_before_forwarding(self) -> None:
        sent: list[dict] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_OK)

        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: httpx.AsyncClient(
                                   transport=httpx.MockTransport(respond))):
            r = self._post({'model': 'glm-5.2', 'input': 'hi',
                            'text': {'format': {'type': 'grammar'}}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error']['code'], 'unsupported_text_format')
        self.assertEqual(sent, [], '不支持的 text.format 绝不能被转发到上游')

    def test_malformed_format_is_rejected_with_stable_code(self) -> None:
        r = self._post({'model': 'glm-5.2', 'input': 'hi',
                        'text': {'format': {'type': 'json_schema', 'name': 'n'}}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error']['code'], 'invalid_text_format')

    def test_json_schema_reaches_the_upstream_body(self) -> None:
        sent: list[dict] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_OK)

        schema = {'type': 'object', 'properties': {'a': {'type': 'integer'}}}
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: httpx.AsyncClient(
                                   transport=httpx.MockTransport(respond))):
            r = self._post({'model': 'glm-5.2', 'input': 'hi',
                            'text': {'format': {'type': 'json_schema',
                                                'name': 'answer', 'schema': schema,
                                                'strict': True}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['response_format'], {
            'type': 'json_schema',
            'json_schema': {'name': 'answer', 'schema': schema, 'strict': True}})

    def test_json_object_reaches_the_upstream_body(self) -> None:
        sent: list[dict] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_OK)

        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: httpx.AsyncClient(
                                   transport=httpx.MockTransport(respond))):
            r = self._post({'model': 'glm-5.2', 'input': 'hi',
                            'text': {'format': {'type': 'json_object'}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0]['response_format'], {'type': 'json_object'})

    def test_plain_text_request_carries_no_response_format(self) -> None:
        sent: list[dict] = []

        def respond(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_OK)

        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: httpx.AsyncClient(
                                   transport=httpx.MockTransport(respond))):
            r = self._post({'model': 'glm-5.2', 'input': 'hi'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn('response_format', sent[0])


if __name__ == '__main__':
    unittest.main()
