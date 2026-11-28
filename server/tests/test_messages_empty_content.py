"""Messages 非流式：两种「空」必须分开——空回合是合法的，渲染不出是错误。

## 边界（大队长 2026-11-28 定，起因 #83）

* **本来就没有内容** = 合法空回合：`content` 为 `None` / `''` / `[]` 配
  `finish_reason=stop` → **200** + `content: []` + `end_turn`。模型就是没说话，
  客户端拿到空答案是可以理解的；把它换成 502 只是把静默降级换成响亮失败，
  而且 `thinking` 开、只回思考块的响应也会被误判进来。
* **有内容但渲染不出** = 错误：`content` 是数组、里面有 part、却一个可交付的
  `text` 都没有（例如只有图片块）→ 报错，端点回 502 `upstream_invalid_body`。
  这才是 D-20 的症状：答案本来有东西，我们没交付。

第三类是**混合数组**（`[text, image]`）：只交付文本块，图片 URL 绝不许混进
正文（K2，`test_g13_protocol_semantics.py` 的 `test_mixed_parts_deliver_text_only`
已封口）。本模块不重复它，只在文档里写成三条一组，免得下次又只读到其中一条。

**空回合必须可观测**：合法空回合照常 200，但它同时意味着一次静默降级。所以
`to_anthropic_response` 每次把「`stop` + 无正文 + 无工具调用」判成空回合时都会
调 `observability.note_empty_turn('messages')`，读数在
`GET /api/health/upstream`（只回环，与 `/api/health/alerts`、`/api/health/inflight`
同一套守卫）。这段**不改行为**，只让它可见。

这两类各在仓库里各有既有的守护者，本模块只补**缺的那一侧**并把手口钉死：

* 合法空回合：`server/tests/test_upstream_alignment_20261123.py` 的
  `test_non_length_finish_untouched`（`text=None`，单测层）与
  `server/tests/test_audit_20261029_gateway_ledger.py` 的
  `test_valid_tool_only_and_empty_text_responses_stay_supported`（`content:''`
  与纯工具调用，端点层）。
* 有内容但渲染不出：`server/tests/test_g13_protocol_semantics.py` 的
  `test_parts_without_any_text_fail_loudly`（单测层）。
* `thinking` 开、只回思考块：空答案判据只认 `text`，思考块不算交付，所以这条
  的终态与 `[]` / `''` / `None` 相同——**200 空答案**。本模块用
  `test_thinking_only_response_is_an_empty_turn` 把它钉死：这条判据刚被讨论出来
  过两次（先想改成 502，后撤回），不钉住就只剩记忆。

原来这里曾要求 `finish=stop` + 空正文一律报错（`#83` 的第一版），与上面两条
既有测试正相反，CI 当场判红——那是把「合法空回合」错当成了「有内容却丢」。
本模块的判据按上表重划：空数组、空串、`null` 三种**没有内容**的形态全部
保持 200，只有「数组里有 part 但没有正文」才响亮报错。
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
from server.routers import anthropic  # noqa: E402

#: 「没有内容」的三种形态。空串与 `null` 是上游常见的两种写法，空数组是
#: content-parts 上游在「没有正文」时发的形态。
NO_CONTENT_FORMS = (None, '', [])


def _message(content, finish='stop', **extra):
    message = {'role': 'assistant', 'content': content}
    message.update(extra)
    return {'choices': [{'message': message, 'finish_reason': finish}]}


class ContentTextSignalTests(unittest.TestCase):
    """`_content_text` 必须把「有没有 parts」告诉调用方，而不是自己决定报不报错。"""

    def test_no_content_carries_no_parts_flag(self):
        for content in NO_CONTENT_FORMS:
            with self.subTest(content=content):
                self.assertEqual(anthropic._content_text(content), ('', False))

    def test_deliverable_parts_carry_text_and_no_flag(self):
        self.assertEqual(
            anthropic._content_text([{'type': 'text', 'text': '答案'}]),
            ('答案', False))

    def test_undeliverable_parts_carry_the_flag(self):
        """只有图片块的数组：正文为空，但第二个值必须为 True。"""
        self.assertEqual(
            anthropic._content_text([{'type': 'image_url',
                                      'image_url': {'url': 'http://x/y.png'}}]),
            ('', True))


class EmptyTurnUnitTests(unittest.TestCase):
    """单测层：没有内容 → 空回合；有 part 没正文 → 报错（两条路径分别断言）。"""

    def test_no_content_forms_render_as_empty_turn(self):
        for content in NO_CONTENT_FORMS:
            with self.subTest(content=content):
                out = anthropic.to_anthropic_response(_message(content), 'claude-x')
                self.assertEqual(out['content'], [], f'{content!r} 应回空 content，不是报错')
                self.assertEqual(out['stop_reason'], 'end_turn')

    def test_no_content_is_not_mistaken_for_budget_exhaustion(self):
        """`stop` + 空正文不是预算耗尽：那条判据只归 `length`。"""
        out = anthropic.to_anthropic_response(_message(None, finish='stop'), 'claude-x')
        self.assertEqual(out['stop_reason'], 'end_turn')

    def test_tool_only_turn_is_not_an_empty_turn(self):
        """纯工具调用是合法的空正文，不能被「空回合」这条线误伤成报错。"""
        out = anthropic.to_anthropic_response(_message(
            None, finish='tool_calls',
            tool_calls=[{'id': 'c1', 'function': {'name': 'f', 'arguments': '{}'}}]), 'claude-x')
        self.assertEqual([block['type'] for block in out['content']], ['tool_use'])
        self.assertEqual(out['stop_reason'], 'tool_use')

    def test_empty_array_with_tool_calls_is_not_an_empty_turn(self):
        """上游用 `content: []`（而不是 `null`）配工具调用时同样合法。"""
        out = anthropic.to_anthropic_response(_message(
            [], finish='stop',
            tool_calls=[{'id': 'c1', 'function': {'name': 'f', 'arguments': '{}'}}]), 'claude-x')
        self.assertEqual([block['type'] for block in out['content']], ['tool_use'])

    def test_parts_without_deliverable_text_still_fail(self):
        """有 part 但一个正文都没有（例如只有图片块）：这是「渲染不出」，必须报错。

        G-13 已有同一条（`test_parts_without_any_text_fail_loudly`）；这里放在
        「空回合」旁边，是为了让一对判据写在同一处，改一边能被另一边提醒。
        """
        with self.assertRaises(ValueError):
            anthropic.to_anthropic_response(_message([
                {'type': 'image_url', 'image_url': {'url': 'http://x/y.png'}},
            ]), 'claude-x')

    def test_answer_is_unchanged(self):
        out = anthropic.to_anthropic_response(_message('答案'), 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': '答案'}])
        self.assertEqual(out['stop_reason'], 'end_turn')

    def test_thinking_only_response_is_an_empty_turn(self):
        """`thinking` 开、只回思考块：思考块不算交付，终态是 200 空答案，不是 502。

        这条边界被讨论过两轮（第一版改动曾把它判成「空正文」报错），所以必须
        有一条测试钉住：思考块只证明上游想过，不证明它说了什么。
        """
        payload = {'choices': [{
            'message': {'role': 'assistant', 'content': None,
                        'reasoning_content': '想了很久但没有输出'},
            'finish_reason': 'stop'}]}
        out = anthropic.to_anthropic_response(payload, 'claude-x', thinking=True)
        self.assertEqual([block['type'] for block in out['content']], ['thinking'])
        self.assertEqual(out['stop_reason'], 'end_turn',
                         '只回思考块的 stop 是合法空回合，不是失败')
        self.assertFalse([block for block in out['content'] if block['type'] == 'text'])


class EmptyTurnCounterTests(unittest.TestCase):
    """空回合必须可观测：每次「上游什么都没说」都进计数，正常答案不进。"""

    def setUp(self):
        observability.reset_empty_turn_counts()
        self.addCleanup(observability.reset_empty_turn_counts)

    def test_every_no_content_form_is_counted(self):
        for content in NO_CONTENT_FORMS:
            anthropic.to_anthropic_response(_message(content), 'claude-x')
        snapshot = observability.empty_turn_snapshot()
        self.assertEqual(snapshot['total'], len(NO_CONTENT_FORMS))
        self.assertEqual(snapshot['by_protocol'], {'messages': len(NO_CONTENT_FORMS)})
        self.assertIn('messages', snapshot['last_ts'])

    def test_thinking_only_response_is_counted(self):
        anthropic.to_anthropic_response({'choices': [{
            'message': {'role': 'assistant', 'content': None,
                        'reasoning_content': '只想了没说话'},
            'finish_reason': 'stop'}]}, 'claude-x', thinking=True)
        self.assertEqual(observability.empty_turn_snapshot()['total'], 1)

    def test_answer_and_tool_turn_are_not_counted(self):
        anthropic.to_anthropic_response(_message('答案'), 'claude-x')
        anthropic.to_anthropic_response(_message(
            None, finish='tool_calls',
            tool_calls=[{'id': 'c1', 'function': {'name': 'f', 'arguments': '{}'}}]), 'claude-x')
        self.assertEqual(observability.empty_turn_snapshot()['total'], 0)


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


class EmptyTurnEndpointTests(unittest.TestCase):
    """端点层：客户端拿到的是 200 空回合 / 502 报错，两者不能互换。"""

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
        # 回环客户端：`/api/health/upstream` 只认回环（与 alerts/inflight 同一套守卫），
        # 这条用例要同时打 Messages 端点和这个探针。
        self.client = TestClient(app, client=('127.0.0.1', 40000))
        self.token = keysvc.create_key('empty-turn')['key']

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

    def test_no_content_forms_stay_200_empty_turn(self):
        for content in NO_CONTENT_FORMS:
            with self.subTest(content=content):
                response = self._post(_message(content))
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body['content'], [])
                self.assertEqual(body['stop_reason'], 'end_turn')

    def test_undeliverable_parts_become_502_not_empty_200(self):
        response = self._post(_message([
            {'type': 'image_url', 'image_url': {'url': 'http://x/y.png'}},
        ]))
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()['error']['type'], 'api_error')
        self.assertEqual(response.json()['error']['code'], 'upstream_invalid_body')
        self.assertNotIn('end_turn', response.text)

    def test_answer_still_200(self):
        response = self._post(_message('答案'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['content'], [{'type': 'text', 'text': '答案'}])

    def test_empty_turn_is_visible_in_health_probe(self):
        """一次真实空回合之后，`/api/health/upstream` 的计数必须看得见地加了 1。"""
        observability.reset_empty_turn_counts()
        self.addCleanup(observability.reset_empty_turn_counts)
        self.assertEqual(observability.empty_turn_snapshot()['total'], 0)
        self.assertEqual(self._post(_message('')).status_code, 200)
        probe = self.client.get('/api/health/upstream')
        self.assertEqual(probe.status_code, 200, probe.text)
        self.assertEqual(probe.json()['empty_turns']['total'], 1)
        self.assertEqual(probe.json()['empty_turns']['by_protocol'], {'messages': 1})
        self.assertEqual(self._post(_message('答案')).status_code, 200)
        self.assertEqual(self.client.get('/api/health/upstream').json()['empty_turns']['total'], 1,
                         '正常答案不许被算成空回合')


class EmptyTurnProbeTests(unittest.TestCase):
    """`GET /api/health/upstream`：空回合计数必须能被真实读到（不是只写内存）。"""

    PATH = '/api/health/upstream'

    def setUp(self):
        observability.reset_empty_turn_counts()
        self.addCleanup(observability.reset_empty_turn_counts)
        from server.main import app
        self.client = TestClient(app, client=('127.0.0.1', 40000))
        self.external = TestClient(app, client=('203.0.113.7', 40000))

    def test_loopback_reads_the_counts(self):
        anthropic.to_anthropic_response(_message(''), 'claude-x')
        response = self.client.get(self.PATH)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['service'], 'zhihui-gateway')
        self.assertEqual(body['empty_turns']['total'], 1)
        self.assertEqual(body['empty_turns']['by_protocol'], {'messages': 1})
        self.assertIn('no-store', response.headers.get('cache-control', ''))

    def test_probe_stays_readable_during_drain(self):
        """排空期运维还要读这个数：探针必须在 DrainMiddleware 的控制路径名单里。

        与 `/api/health/alerts`、`/api/health/inflight` 同一条：不在名单里的路径
        排空期会被直接回 503 `gateway_draining`，「排空窗口里继续看计数」当场失效。
        """
        from server import drain
        drain.begin()
        self.addCleanup(drain.reset_for_tests)
        response = self.client.get(self.PATH)
        self.assertEqual(response.status_code, 200, response.text)

    def test_non_loopback_callers_are_refused(self):
        response = self.external.get(self.PATH)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {'error': 'loopback only'})


if __name__ == '__main__':
    unittest.main()
