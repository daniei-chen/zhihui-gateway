"""U05：`budget.prepare` 收缩 `max_tokens` 时，客户端必须看得见。

`server/budget.py` 的 `prepare()` 在 token 配额不足时会把
`body[output_field]` 收缩成 `remaining - estimated_input` —— 这是**硬预算**，
必须继续生效；但改动前它不留任何客户端可见痕迹：客户端以为自己有
`max_tokens` 个 token 的额度，实际只拿到更小的值，表现为「回答莫名被截断」。

本轮不改 budget.py，只在三条路由共用的转发入口
（`gateway._prepare_forward`）快照收缩前后的值，经 `observability` 的
request state 由中间件写进响应头：

    x-manager-max-tokens-requested: <客户端请求值>
    x-manager-max-tokens-effective: <实际发给上游的值>
    x-manager-max-tokens-shrink-reason: budget_quota

契约要点（本文件逐条断言）：
  1. 收缩时三个头都出现，且 effective == 真正发往上游的那个值；
  2. 未收缩时**一个头都没有**（"有没有这个头"本身就是信号）；
  3. 三协议（chat / messages / responses）都有；
  4. 硬预算仍然生效：配额连输入都覆盖不了时照样 429。
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

_OK = {
    'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'm',
    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                 'finish_reason': 'stop'}],
    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
}
REQUESTED = 50000
QUOTA = 10000

CONTRACT_HEADERS = ('x-manager-max-tokens-requested',
                    'x-manager-max-tokens-effective',
                    'x-manager-max-tokens-shrink-reason')


class MaxTokensContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = directory / 'u05.db'
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

    # ── 工具 ────────────────────────────────────────────────────────────
    def _key(self, *, quota: int = 0) -> str:
        return keysvc.create_key(name=f'u05-{quota}', quota=quota)['key']

    def _post(self, path: str, payload: dict, token: str,
              headers: dict | None = None) -> tuple[httpx.Response, list[dict]]:
        """发一次请求，返回 (响应, 真正发往上游的 body 列表)。"""
        sent: list[dict] = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == 'POST':
                sent.append(json.loads(request.content.decode()))
                return httpx.Response(200, json=_OK)
            return httpx.Response(200, json={'data': []})

        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: httpx.AsyncClient(
                                   transport=httpx.MockTransport(respond))):
            response = self.c.post(path, json=payload,
                                   headers={'Authorization': f'Bearer {token}',
                                            **(headers or {})})
        return response, sent

    def _assert_contract(self, response: httpx.Response, sent: list[dict],
                         field: str = 'max_tokens') -> None:
        self.assertEqual(len(sent), 1, '请求应该已经转发到（假）上游')
        requested = int(response.headers['x-manager-max-tokens-requested'])
        effective = int(response.headers['x-manager-max-tokens-effective'])
        self.assertEqual(response.headers['x-manager-max-tokens-shrink-reason'],
                         'budget_quota')
        self.assertEqual(requested, REQUESTED)
        self.assertLess(effective, requested, '收缩后的值必须更小')
        self.assertGreater(effective, 0)
        self.assertEqual(sent[0][field], effective,
                         '响应头里告诉客户端的值必须就是发往上游的值')

    # ── 1. 收缩时有标记（三协议）────────────────────────────────────────
    def test_chat_reports_shrink(self) -> None:
        r, sent = self._post('/v1/chat/completions', {
            'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': REQUESTED}, self._key(quota=QUOTA))
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_contract(r, sent)

    def test_anthropic_reports_shrink(self) -> None:
        r, sent = self._post('/v1/messages', {
            'model': 'm', 'max_tokens': REQUESTED,
            'messages': [{'role': 'user', 'content': 'hi'}]},
            self._key(quota=QUOTA),
            headers={'anthropic-version': '2023-06-01'})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_contract(r, sent)

    def test_responses_reports_shrink(self) -> None:
        r, sent = self._post('/v1/responses', {
            'model': 'm', 'input': 'hi', 'max_output_tokens': REQUESTED},
            self._key(quota=QUOTA))
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_contract(r, sent)

    def test_max_completion_tokens_is_also_reported(self) -> None:
        """OpenAI 新字段名走的是同一个契约（budget 也认这个字段）。"""
        r, sent = self._post('/v1/chat/completions', {
            'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_completion_tokens': REQUESTED}, self._key(quota=QUOTA))
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_contract(r, sent, field='max_completion_tokens')

    # ── 2. 未收缩时零标记 ───────────────────────────────────────────────
    def test_unlimited_key_has_no_marker(self) -> None:
        r, sent = self._post('/v1/chat/completions', {
            'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 4096}, self._key(quota=0))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0]['max_tokens'], 4096)
        for name in CONTRACT_HEADERS:
            self.assertNotIn(name, r.headers, f'未收缩时不该出现 {name}')

    def test_quota_key_within_budget_has_no_marker(self) -> None:
        """有配额但没触发收缩时同样不能出现标记（否则标记就没有信息量）。"""
        r, sent = self._post('/v1/chat/completions', {
            'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 512}, self._key(quota=QUOTA))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0]['max_tokens'], 512)
        for name in CONTRACT_HEADERS:
            self.assertNotIn(name, r.headers, f'未收缩时不该出现 {name}')

    def test_marker_survives_streaming_response(self) -> None:
        """流式响应在开流那一刻就能带上头（收缩发生在转发之前）。"""
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=b'data: {"choices":[{"delta":{"content":"x"},'
                                          b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: httpx.AsyncClient(
                                   transport=httpx.MockTransport(respond))):
            r = self.c.post('/v1/chat/completions',
                            json={'model': 'm', 'stream': True,
                                  'messages': [{'role': 'user', 'content': 'hi'}],
                                  'max_tokens': REQUESTED},
                            headers={'Authorization': f'Bearer {self._key(quota=QUOTA)}'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get('x-manager-max-tokens-shrink-reason'),
                         'budget_quota')

    # ── 3. 硬预算仍然生效 ───────────────────────────────────────────────
    def test_hard_budget_still_rejects(self) -> None:
        """配额连输入估算都覆盖不了 → 仍然 429，不因为「要加标记」而放行。"""
        r, sent = self._post('/v1/chat/completions', {
            'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 4096}, self._key(quota=1000))
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()['error']['code'], 'insufficient_quota')
        self.assertEqual(sent, [], '被预算拒绝的请求不该发到上游')
        for name in CONTRACT_HEADERS:
            self.assertNotIn(name, r.headers)

    def test_contract_does_not_change_the_wire_value_budget_chose(self) -> None:
        """标记是**只读**的：不能反过来改 budget 收缩出来的值。"""
        r, sent = self._post('/v1/chat/completions', {
            'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': REQUESTED}, self._key(quota=QUOTA))
        effective = int(r.headers['x-manager-max-tokens-effective'])
        self.assertEqual(sent[0]['max_tokens'], effective)
        self.assertLessEqual(effective, QUOTA)


class RecordContractUnitTest(unittest.TestCase):
    """state 层的纯逻辑：只在「真的收缩」时才让中间件写头。"""

    def _state(self):
        from server.observability import RequestAudit
        return RequestAudit('rid', '/v1/x', 'POST', 'openai', 0.0)

    def test_records_both_values(self) -> None:
        from server import observability
        state = self._state()
        observability.record_max_tokens_contract(state, 'max_tokens', 50000, 7800)
        self.assertEqual(state.max_tokens_requested, 50000)
        self.assertEqual(state.max_tokens_effective, 7800)
        self.assertEqual(state.max_tokens_field, 'max_tokens')

    def test_headers_only_when_shrunk(self) -> None:
        from server import observability
        state = self._state()
        observability.record_max_tokens_contract(state, 'max_tokens', 100, 100)
        self.assertEqual(observability._max_tokens_headers(state), [])
        observability.record_max_tokens_contract(state, 'max_tokens', 100, 99)
        names = [name for name, _ in observability._max_tokens_headers(state)]
        self.assertEqual(names, list(observability.MAX_TOKENS_HEADERS))

    def test_missing_or_bogus_values_are_ignored(self) -> None:
        from server import observability
        state = self._state()
        for requested, effective in ((None, 10), (10, None), (True, 10), (10, 0),
                                     ('10', 5), (10, '5')):
            with self.subTest(requested=requested, effective=effective):
                observability.record_max_tokens_contract(
                    state, 'max_tokens', requested, effective)
                self.assertIsNone(state.max_tokens_requested)
                self.assertEqual(observability._max_tokens_headers(state), [])

    def test_none_state_is_a_noop(self) -> None:
        from server import observability
        observability.record_max_tokens_contract(None, 'max_tokens', 10, 5)


if __name__ == '__main__':
    unittest.main()
