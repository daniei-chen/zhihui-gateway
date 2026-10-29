"""U13 交付 1：三条协议共用同一套预算对象（连接 / 首包 / 空闲 / 总时长 / 取消）。

改动前 Anthropic messages 与 Responses 两个兼容层只建了
`http_client(UPSTREAM_TIMEOUT, connect=5)`：连接有 5s 预算，其余全压在 httpx 的
read timeout（每次 read 的间隔）上 —— 首包没有单独口径，**总时长完全没有上界**，
上游只要持续吐字节，流就能一直续命。

本文件把这个缺口变成断言：

  · 三条路由都从 `deadline` 取预算（source 守卫，防止回退到老写法）；
  · 首包 / 总时长预算在三条路径上**真的**会中断请求（端到端，假上游）；
  · 超时被正确归类为传输层事实（`transport_error` + `kind`），不冒充协议错误；
  · 取消关闭窗口来自同一份预算对象，不再各写一个常数。

假上游只在本地替身里「变慢」，不发起任何真实上游调用。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, deadline, keysvc, observability  # noqa: E402

ROUTERS = Path(__file__).resolve().parents[1] / 'routers'
ANTHROPIC_HEADERS = {'anthropic-version': '2023-06-01'}


def _src(name: str) -> str:
    return (ROUTERS / name).read_text(encoding='utf-8')


def _fake_client(*, send_delay: float = 0.0, chunk_delay: float = 0.0,
                 status: int = 200, chunks=(), headers=None):
    """假 httpx 客户端：可慢在「响应头之前」或「首包之后」。"""
    class _Resp:
        def __init__(self) -> None:
            self.status_code = status
            self.headers = dict(headers or {'content-type': 'text/event-stream'})
            self.text = b''.join(chunks).decode('utf-8', 'replace')

        async def aiter_bytes(self):
            for chunk in chunks:
                if chunk_delay:
                    await asyncio.sleep(chunk_delay)
                yield chunk

        async def aread(self):
            return self.text.encode()

        async def aclose(self):
            return None

    class _Client:
        def build_request(self, *a, **kw):
            return object()

        async def send(self, req, **kw):
            if send_delay:
                await asyncio.sleep(send_delay)
            return _Resp()

        async def post(self, url, **kw):
            if send_delay:
                await asyncio.sleep(send_delay)
            return _Resp()

        async def aclose(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return lambda *a, **k: _Client()


class SharedBudgetSourceTest(unittest.TestCase):
    """三条路由必须从同一个模块取预算，不能各建各的客户端。"""

    def test_every_protocol_takes_budgets_from_deadline(self) -> None:
        for name in ('gateway.py', 'anthropic.py', 'responses.py'):
            with self.subTest(router=name):
                self.assertIn('deadline_mod.request_budgets(', _src(name),
                              f'{name} 没有走共享预算入口')

    def test_no_router_builds_its_own_timeout_client(self) -> None:
        """禁止回退到「只给 read timeout、没有总时长」的老写法。"""
        for name in ('gateway.py', 'anthropic.py', 'responses.py'):
            with self.subTest(router=name):
                src = _src(name)
                self.assertNotIn('http_client(config.UPSTREAM_TIMEOUT', src)
                self.assertIn('deadline_mod.transport_timeouts(', src)

    def test_compat_layers_cover_all_five_phases(self) -> None:
        for name in ('anthropic.py', 'responses.py'):
            with self.subTest(router=name):
                src = _src(name)
                for needle in ('deadline_mod.await_headers(',
                               'deadline_mod.await_total(',
                               'deadline_mod.iterate_with_budgets(',
                               'deadline_mod.BudgetExceeded',
                               'deadline_mod.close_timeout('):
                    self.assertIn(needle, src, f'{name} 缺 {needle}')


class ClassificationTest(unittest.TestCase):
    """超时是传输层事实：终态一律 transport_error，kind 指明是哪一类预算。"""

    def test_every_budget_kind_is_classified(self) -> None:
        for kind in ('connect', 'send_request', 'first_byte', 'stream_idle', 'total'):
            with self.subTest(kind=kind):
                failure = deadline.classify_budget_exceeded(
                    deadline.BudgetExceeded(kind, 'detail'))
                self.assertEqual(failure.kind, kind)
                self.assertEqual(failure.terminal_state, 'transport_error')
                self.assertEqual(failure.error_stage, 'stream_read')
                self.assertIn('时间预算', failure.message)
                self.assertIn(kind, failure.message)

    def test_unknown_kind_still_classifies_as_transport(self) -> None:
        failure = deadline.classify_budget_exceeded(deadline.BudgetExceeded('weird'))
        self.assertEqual(failure.terminal_state, 'transport_error')
        self.assertEqual(failure.kind, 'weird')

    def test_budget_exceeded_is_not_an_upstream_business_rejection(self) -> None:
        """超时不能被归成 upstream_error（那是「上游业务拒绝」的口径）。"""
        for kind in ('first_byte', 'stream_idle', 'total'):
            failure = deadline.classify_budget_exceeded(deadline.BudgetExceeded(kind))
            self.assertNotEqual(failure.terminal_state, 'upstream_error')
            self.assertNotEqual(failure.error_stage, 'upstream')


class BudgetEnforcedTest(unittest.TestCase):
    """端到端：三条路径的首包 / 总时长预算真的会中断请求。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = directory / 'u13-budget.db'
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
        return keysvc.create_key(name='u13-budget')['key']

    def _tight(self, **kwargs):
        """把总时长预算压到 50ms（其余预算被 normalized 夹到同一上界）。"""
        return mock.patch.dict(os.environ, {'WB_TOTAL_TIMEOUT_S': '0.05'}, clear=False)

    # ── Anthropic messages ──────────────────────────────────────────────
    def test_messages_waits_for_headers_inside_budget(self) -> None:
        body = {'model': 'm', 'max_tokens': 16, 'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]}
        with self._tight(), mock.patch.object(
                config, 'http_client', _fake_client(send_delay=0.4)):
            r = self.c.post('/v1/messages', json=body,
                            headers={'x-api-key': self._key(), **ANTHROPIC_HEADERS})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()['type'], 'error')
        self.assertIn('时间预算', r.json()['error']['message'])

    def test_messages_stream_stops_at_the_total_budget(self) -> None:
        chunk = b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'
        body = {'model': 'm', 'max_tokens': 16, 'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]}
        with self._tight(), mock.patch.object(
                config, 'http_client',
                _fake_client(chunk_delay=0.4, chunks=(chunk,))):
            r = self.c.post('/v1/messages', json=body,
                            headers={'x-api-key': self._key(), **ANTHROPIC_HEADERS})
        self.assertEqual(r.status_code, 200, r.text)
        text = r.content.decode()
        self.assertIn('event: error', text, '预算用尽必须以 error 事件收尾')
        self.assertIn('时间预算', text)

    # ── Responses ───────────────────────────────────────────────────────
    def test_responses_waits_for_headers_inside_budget(self) -> None:
        body = {'model': 'm', 'input': 'hi', 'stream': True}
        with self._tight(), mock.patch.object(
                config, 'http_client', _fake_client(send_delay=0.4)):
            r = self.c.post('/v1/responses', json=body,
                            headers={'Authorization': f'Bearer {self._key()}'})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()['error']['code'], 'upstream_timeout')
        self.assertIn('时间预算', r.json()['error']['message'])

    def test_responses_stream_stops_at_the_total_budget(self) -> None:
        chunk = b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'
        body = {'model': 'm', 'input': 'hi', 'stream': True}
        with self._tight(), mock.patch.object(
                config, 'http_client',
                _fake_client(chunk_delay=0.4, chunks=(chunk,))):
            r = self.c.post('/v1/responses', json=body,
                            headers={'Authorization': f'Bearer {self._key()}'})
        self.assertEqual(r.status_code, 200, r.text)
        text = r.content.decode()
        self.assertIn('response.failed', text, '预算用尽必须以 response.failed 收尾')
        self.assertIn('时间预算', text)

    # ── OpenAI chat（改动前就有预算，这里守住它没被统一动作改坏）──────────
    def test_chat_nonstream_respects_total_budget(self) -> None:
        body = {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}
        with self._tight(), mock.patch.object(
                config, 'http_client', _fake_client(send_delay=0.4)):
            r = self.c.post('/v1/chat/completions', json=body,
                            headers={'Authorization': f'Bearer {self._key()}'})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn('时间预算', r.text)

    def test_chat_stream_respects_first_byte_budget(self) -> None:
        body = {'model': 'm', 'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]}
        chunk = b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'
        with self._tight(), mock.patch.object(
                config, 'http_client',
                _fake_client(chunk_delay=0.4, chunks=(chunk,))):
            r = self.c.post('/v1/chat/completions', json=body,
                            headers={'Authorization': f'Bearer {self._key()}'})
        self.assertEqual(r.status_code, 200, r.text)
        text = r.content.decode()
        self.assertIn('stream_incomplete', text)
        self.assertIn('时间预算', text)


class CancelWindowTest(unittest.TestCase):
    """取消关闭窗口来自预算对象（`client_cancel_s`），不是三处各写一个常数。"""

    def test_close_stream_honours_explicit_window(self) -> None:
        closed: list[str] = []

        class _Resp:
            async def aclose(self):
                closed.append('response')

        class _Client:
            async def aclose(self):
                closed.append('client')

        asyncio.run(observability.close_stream(_Resp(), _Client(), timeout=0.01))
        self.assertEqual(closed, ['response', 'client'])

    def test_close_window_comes_from_budgets(self) -> None:
        budgets = deadline.Budgets(client_cancel_s=3)
        self.assertEqual(deadline.close_timeout(budgets), 3.0)
        self.assertEqual(deadline.close_timeout(deadline.Budgets()), 5.0,
                         '默认值必须仍是 5s（不改变既有行为）')

    def test_all_three_routers_pass_the_budget_window(self) -> None:
        for name in ('gateway.py', 'anthropic.py', 'responses.py'):
            with self.subTest(router=name):
                src = _src(name)
                # 一处「生成器收尾」+ 一处「cleanup 回调」，两条关闭路径都要带窗口。
                self.assertEqual(
                    src.count('timeout=deadline_mod.close_timeout(budgets)'), 2,
                    f'{name} 的关闭窗口没有全部取自预算对象')


class BudgetsParityTest(unittest.TestCase):
    """五类预算的取值口径在三条协议上必须一致。"""

    def test_shared_budgets_are_one_object_type(self) -> None:
        budgets = deadline.request_budgets(120)
        self.assertIsInstance(budgets, deadline.Budgets)
        self.assertEqual(budgets.total_s, 120.0)
        self.assertEqual(budgets.first_byte_s, 120.0)
        self.assertEqual(budgets.stream_idle_s, 120.0)
        self.assertEqual(deadline.transport_timeouts(budgets),
                         {'timeout': 120.0, 'connect': 5.0})

    def test_open_request_returns_a_deadline(self) -> None:
        request = deadline.open_request(30)
        self.assertIsInstance(request, deadline.Deadline)
        self.assertEqual(request.budgets.total_s, 30.0)


if __name__ == '__main__':
    unittest.main()
