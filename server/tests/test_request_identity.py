"""RV01：请求身份拆分（服务端执行 ID vs 调用方关联 ID）的回归测试。

缺陷背景（独立复核已复现，见 evidence/independent-review-20261025/）：
外部 `X-Request-ID` 被 `observability._request_id_from_headers` 原样采纳成
`RequestAudit.request_id`，进而成为 `budget_reservations` 主键、
`request_logs.request_id`、`request_outcomes.request_id`。三个可复现后果：
同 key 重发同一 ID → 第二个 500（预算主键冲突，上游只收到一笔）；不同 key
用同一 ID → 同样 500，跨租户共用全局命名空间；匿名 401 先占 ID → 后续合法
200 的 outcome 被 `INSERT OR IGNORE` 丢弃，终态停在 401/rejected。

修复契约（见 observability 模块 docstring 与 migrations._request_identity）：
执行 ID 一律服务端生成、调用方不可控制；关联 ID 单独存 `client_request_id`
（可空、不参与主键、不回填猜测）；响应头/转发头分别表达两种 ID。

本文件覆盖的 12 类断言：
  1. 同 key 重复同一合法 ID → 两个 200，各自独立账本；
  2. 跨 key 同一 ID → 都 200，不共用命名空间；
  3. 匿名 401 先行、随后合法 200 → 两笔 outcome 各自独立，不串号；
  4. 并发碰撞（8 线程同 ID/不同 ID 混合）→ 无 500，执行 ID 互不相同；
  5. SSE 流式路径的服务端 ID / 关联 ID / terminal_state；
  6. 客户端取消路径的 terminal_state 与两个 ID；
  7. 上游 500 路径的账本与关联 ID；
  8. budget_reservations 主键为服务端 ID，重复外部 ID 各自预留/结算；
  9. 响应头（x-request-id = 执行 ID；x-client-request-id 仅合法值回显）；
 10. 非法/超长/CRLF 注入头被丢弃且不影响主键；
 11. 转发上游头（X-Request-ID = 关联 ID，X-Manager-Request-ID = 执行 ID）；
 12. 旧库升级：旧形状库 → db.connect → 列已补、旧行保留且为 NULL。

真实 ASGI 链路用 TestClient + 隔离运行器提供的 loopback fake provider
（与 test_live_http_platform_flow.py 同一套夹具风格）；出站 HTTP 不打桩，
但目标必须是 127.0.0.1，否则直接失败。不访问外网、不写越界路径。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import urlopen

from fastapi.testclient import TestClient

from server import (budget, budgetowner, config, db, migrations,
                    observability, security, tokensvc)
from server.routers import gateway

HEX32 = re.compile(r'[0-9a-f]{32}\Z')


def _is_hex32(value: str) -> bool:
    return bool(isinstance(value, str) and HEX32.fullmatch(value))


class _HttpCase(unittest.TestCase):
    """真实 ASGI + 真实 loopback 上游 + 独立 SQLite 的公共夹具。"""

    def setUp(self) -> None:
        base = os.environ['OPENAI_BASE_URL'].rstrip('/')
        self.base = base.removesuffix('/v1')
        self.assertEqual(urlsplit(self.base).hostname, '127.0.0.1',
                         'Integration tests must use the isolated loopback provider')
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_db = config.DB_PATH
        self.original_users = config.USERS_FILE
        budgetowner.close()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.root / 'request-identity.db'
        config.USERS_FILE = self.root / 'users.json'
        security.save_users({'secret': 'S' * 64, 'users': [], 'api_keys': []})
        self.base_patch = patch.object(config, 'WB2API_BASE', self.base)
        self.base_patch.start()
        self.key_patch = patch.object(config, 'WB2API_KEY', 'isolated-upstream-test')
        self.key_patch.start()
        secret = self.root / 'upstream-key'
        secret.write_text('isolated-upstream-test', encoding='utf-8')
        self.spec_patch = patch.object(gateway, '_THREEWAY_SPEC', {
            'qoder': (self.base, str(secret)), 'trae': (self.base, str(secret))})
        self.spec_patch.start()
        db.connect()
        budget.initialize()
        from server.main import app
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)
        token = tokensvc.create_token('request-identity', 'admin', None, 'test')
        self.admin = {'Authorization': 'Bearer ' + token['token']}

    def tearDown(self) -> None:
        self.client.close()
        budgetowner.close()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.original_db
        config.USERS_FILE = self.original_users
        self.spec_patch.stop()
        self.key_patch.stop()
        self.base_patch.stop()
        self.tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def new_key(self, label: str = 'identity', **extra) -> dict:
        payload = {'name': label, 'platforms': ['workbuddy']}
        payload.update(extra)
        created = self.client.post('/api/keys', headers=self.admin, json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        return created.json()

    def provider_total(self) -> int:
        with urlopen(self.base + '/__stats', timeout=3) as response:
            return json.load(response)['total']

    def generation(self, key: dict | None, *, client_id: str | None = None,
                   model: str = 'fake-model', stream: bool = False,
                   headers: dict | None = None, client: TestClient | None = None):
        body = {'model': model,
                'messages': [{'role': 'user', 'content': 'request identity'}],
                'max_tokens': 32}
        if stream:
            body['stream'] = True
        sent = dict(headers or {})
        if key is not None:
            sent['Authorization'] = 'Bearer ' + key['key']
        if client_id is not None:
            sent['X-Request-ID'] = client_id
        return (client or self.client).post('/v1/chat/completions', headers=sent, json=body)

    def rows(self, table: str, client_request_id: str) -> list[dict]:
        return [dict(r) for r in db.query(
            f'SELECT * FROM {table} WHERE client_request_id=? ORDER BY rowid', (client_request_id,))]

    def outcome_for(self, request_id: str) -> dict | None:
        row = db.query_one('SELECT * FROM request_outcomes WHERE request_id=?', (request_id,))
        return dict(row) if row is not None else None

    def log_for(self, request_id: str) -> dict | None:
        row = db.query_one('SELECT * FROM request_logs WHERE request_id=?', (request_id,))
        return dict(row) if row is not None else None


class ClientRequestIdExtractionTest(unittest.TestCase):
    """契约第 2 条：关联 ID 只做提取与校验，绝不生成执行 ID（断言 10 的纯函数部分）。"""

    def test_valid_caller_id_is_returned_verbatim(self) -> None:
        from server.observability import client_request_id_from_headers
        for good in ('abc-123', 'a.b:c_d', 'A' * 128, '0', 'k4792875202'):
            with self.subTest(good=good):
                self.assertEqual(
                    client_request_id_from_headers({b'x-request-id': good.encode()}), good)

    def test_invalid_caller_id_yields_none(self) -> None:
        from server.observability import client_request_id_from_headers
        cases = {
            'missing': {},
            'too_long': {b'x-request-id': b'A' * 300},
            'crlf_injection': {b'x-request-id': b'abc\r\nX-Injected: 1'},
            'space': {b'x-request-id': b'abc def'},
            'control_char': {b'x-request-id': b'abc\x01def'},
            'empty': {b'x-request-id': b''},
            'high_bytes': {b'x-request-id': b'\xff\xfe'},
            'slash': {b'x-request-id': b'abc/def'},
            'sql_quote': {b'x-request-id': b"abc'def"},
        }
        for label, headers in cases.items():
            with self.subTest(case=label):
                self.assertIsNone(client_request_id_from_headers(headers))

    def test_server_id_is_generated_and_unique(self) -> None:
        from server.observability import new_request_id
        values = {new_request_id() for _ in range(64)}
        self.assertEqual(len(values), 64)
        for value in values:
            self.assertTrue(_is_hex32(value), value)

    def test_server_id_does_not_depend_on_headers(self) -> None:
        """同一组头连续两次调用必须给出不同的执行 ID，且都不等于外部值。"""
        from server.observability import client_request_id_from_headers, new_request_id
        headers = {b'x-request-id': b'caller-fixed-id'}
        self.assertEqual(client_request_id_from_headers(headers), 'caller-fixed-id')
        first, second = new_request_id(), new_request_id()
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, 'caller-fixed-id')
        self.assertNotEqual(second, 'caller-fixed-id')


class InvalidHeaderMiddlewareTest(unittest.TestCase):
    """断言 10（ASGI 层）：非法头既不进响应头，也不影响执行 ID / 关联 ID 的归属。"""

    def _drive(self, raw_headers: list[tuple[bytes, bytes]]):
        seen: dict = {}

        async def app(scope, receive, send):
            seen['request_id'] = scope['state']['request_id']
            seen['client_request_id'] = scope['state']['client_request_id']
            # 下游路由故意伪造同名头：中间件必须剥掉后再写自己的值。
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'x-request-id', b'forged-by-route'),
                                    (b'x-client-request-id', b'forged-by-route')]})
            await send({'type': 'http.response.body', 'body': b'ok'})

        async def receive():
            return {'type': 'http.request', 'body': b''}

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        scope = {'type': 'http', 'path': '/offline', 'method': 'GET',
                 'headers': raw_headers}
        asyncio.run(observability.RequestAuditMiddleware(app)(scope, receive, send))
        start = messages[0]
        pairs = [(k.lower(), v) for k, v in start['headers']]
        return seen, pairs

    def test_crlf_header_is_dropped(self) -> None:
        seen, headers = self._drive([(b'x-request-id', b'abc\r\nX-Injected: 1')])
        self.assertIsNone(seen['client_request_id'])
        self.assertTrue(_is_hex32(seen['request_id']))
        self.assertEqual(dict(headers)[b'x-request-id'].decode(), seen['request_id'])
        self.assertNotIn(b'x-client-request-id', dict(headers))
        self.assertNotIn(b'x-injected', dict(headers))
        self.assertNotIn(b'forged-by-route', [v for _k, v in headers])

    def test_oversized_header_is_dropped(self) -> None:
        seen, headers = self._drive([(b'x-request-id', b'A' * 300)])
        self.assertIsNone(seen['client_request_id'])
        self.assertTrue(_is_hex32(seen['request_id']))
        self.assertNotIn(b'x-client-request-id', dict(headers))
        self.assertNotEqual(seen['request_id'], 'A' * 300)

    def test_valid_header_is_exposed_and_echoed(self) -> None:
        seen, headers = self._drive([(b'x-request-id', b'caller-ok')])
        self.assertEqual(seen['client_request_id'], 'caller-ok')
        self.assertTrue(_is_hex32(seen['request_id']))
        self.assertNotEqual(seen['request_id'], 'caller-ok')
        self.assertEqual(dict(headers)[b'x-request-id'].decode(), seen['request_id'])
        self.assertEqual(dict(headers)[b'x-client-request-id'], b'caller-ok')

    def test_missing_header_has_no_echo(self) -> None:
        seen, headers = self._drive([])
        self.assertIsNone(seen['client_request_id'])
        self.assertTrue(_is_hex32(seen['request_id']))
        self.assertNotIn(b'x-client-request-id', dict(headers))


class UpstreamForwardingHeaderTest(unittest.TestCase):
    """断言 11：转发上游头的两种 ID 语义（函数级；端到端见 _HttpCase）。"""

    def _headers(self, client_request_id: str | None):
        upstream = {'id': 7, 'base_url': 'http://127.0.0.1:9/v1',
                    'api_key': 'isolated-upstream-test'}
        state = observability.RequestAudit('server-execution-id', '/offline', 'POST',
                                           'openai', time.monotonic(),
                                           client_request_id=client_request_id)
        token = observability.current.set(state)
        try:
            return gateway._upstream_headers(upstream)
        finally:
            observability.current.reset(token)

    def test_client_id_wins_when_present(self) -> None:
        headers = self._headers('caller-abc')
        self.assertEqual(headers['X-Request-ID'], 'caller-abc')
        self.assertEqual(headers['X-Manager-Request-ID'], 'server-execution-id')

    def test_execution_id_falls_back_when_no_client_id(self) -> None:
        headers = self._headers(None)
        self.assertEqual(headers['X-Request-ID'], 'server-execution-id')
        self.assertEqual(headers['X-Manager-Request-ID'], 'server-execution-id')

    def test_both_headers_are_always_distinct_in_meaning(self) -> None:
        headers = self._headers('caller-abc')
        self.assertIn('X-Request-ID', headers)
        self.assertIn('X-Manager-Request-ID', headers)
        self.assertNotEqual(headers['X-Request-ID'], headers['X-Manager-Request-ID'])


class StreamCancellationTest(unittest.TestCase):
    """断言 5/6 的确定性部分：流状态机取消时保留两个 ID 且终态正确。"""

    def test_cancelled_stream_state_keeps_ids_separate(self) -> None:
        state = observability.RequestAudit(
            'srv-stream-1', '/v1/chat/completions', 'POST', 'openai',
            time.monotonic(), client_request_id='caller-stream-1')
        token = observability.current.set(state)
        try:
            observability.set_stream_state(observability.StreamAudit(), cancelled=True)
            self.assertEqual(state.terminal_state, 'client_cancelled')
            self.assertEqual(state.error_stage, 'client')
            self.assertEqual(state.request_id, 'srv-stream-1')
            self.assertEqual(state.client_request_id, 'caller-stream-1')
        finally:
            observability.current.reset(token)

    def test_transport_cancel_sets_client_cancelled(self) -> None:
        async def run() -> None:
            state = observability.RequestAudit(
                'srv-cancel-1', '/v1/chat/completions', 'POST', 'openai',
                time.monotonic(), client_request_id='caller-cancel-1')
            token = observability.current.set(state)
            try:
                async def body():
                    yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                    await asyncio.sleep(30)          # 保持流未完成，等待取消
                    yield b'data: [DONE]\n\n'

                response = observability.ManagedStreamingResponse(body())

                async def receive():
                    await asyncio.sleep(30)
                    return {'type': 'http.disconnect'}

                async def send(message):
                    return None

                task = asyncio.ensure_future(response({'type': 'http', 'method': 'POST'},
                                                     receive, send))
                await asyncio.sleep(0.1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(state.terminal_state, 'client_cancelled')
                self.assertEqual((state.request_id, state.client_request_id),
                                 ('srv-cancel-1', 'caller-cancel-1'))
            finally:
                observability.current.reset(token)

        asyncio.run(run())


class LiveRequestIdentityTest(_HttpCase):
    """断言 1-9、11 的端到端部分：真实 ASGI + 真实 loopback 上游 + 真实 SQLite。"""

    def test_same_key_repeated_client_id_is_two_independent_executions(self) -> None:
        key = self.new_key('dup-same-key')
        client_id = 'review-' + uuid.uuid4().hex
        before = self.provider_total()
        first = self.generation(key, client_id=client_id)
        second = self.generation(key, client_id=client_id)
        self.assertEqual((first.status_code, second.status_code), (200, 200),
                         (first.text, second.text))
        first_id = first.headers['x-request-id']
        second_id = second.headers['x-request-id']
        for response in (first, second):
            self.assertTrue(_is_hex32(response.headers['x-request-id']))
            self.assertNotEqual(response.headers['x-request-id'], client_id,
                                '执行 ID 不得等于外部 ID')
            self.assertEqual(response.headers['x-client-request-id'], client_id)
        self.assertNotEqual(first_id, second_id, '每笔执行必须有独立执行 ID')
        # 两笔都真的到达上游（修复前第二笔在预算主键冲突处 500，上游只收到一笔）
        self.assertEqual(self.provider_total() - before, 2)
        # 各自独立账本
        logs = self.rows('request_logs', client_id)
        outcomes = self.rows('request_outcomes', client_id)
        self.assertEqual(len(logs), 2)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual({row['request_id'] for row in logs}, {first_id, second_id})
        self.assertEqual({row['request_id'] for row in outcomes}, {first_id, second_id})
        self.assertTrue(all(row['terminal_state'] == 'completed' for row in outcomes))
        self.assertTrue(all(row['http_status'] == 200 for row in outcomes))
        # budget_reservations 主键是执行 ID，两笔各自预留并结算
        reservations = [dict(r) for r in db.query(
            'SELECT request_id, key_id, state FROM budget_reservations WHERE request_id IN (?,?)',
            (first_id, second_id))]
        self.assertEqual(len(reservations), 2)
        self.assertEqual({row['state'] for row in reservations}, {'settled'})
        self.assertIsNone(db.query_one('SELECT request_id FROM budget_reservations '
                                      'WHERE request_id=?', (client_id,)),
                          '外部 ID 绝不能成为预算主键')

    def test_different_keys_sharing_one_client_id_stay_isolated(self) -> None:
        key_a = self.new_key('cross-key-a')
        key_b = self.new_key('cross-key-b')
        client_id = 'shared-' + uuid.uuid4().hex
        before = self.provider_total()
        first = self.generation(key_a, client_id=client_id)
        second = self.generation(key_b, client_id=client_id)
        self.assertEqual((first.status_code, second.status_code), (200, 200),
                         (first.text, second.text))
        self.assertEqual(self.provider_total() - before, 2)
        first_id, second_id = first.headers['x-request-id'], second.headers['x-request-id']
        self.assertNotEqual(first_id, second_id)
        logs = self.rows('request_logs', client_id)
        self.assertEqual(len(logs), 2)
        self.assertEqual({row['key_id'] for row in logs}, {key_a['id'], key_b['id']},
                         '两个租户的账本必须各自归属自己的 key')
        self.assertEqual(len(self.rows('request_outcomes', client_id)), 2)

    def test_anonymous_401_does_not_poison_later_success(self) -> None:
        key = self.new_key('anonymous-first')
        client_id = 'anon-' + uuid.uuid4().hex
        anonymous = self.generation(None, client_id=client_id)
        self.assertEqual(anonymous.status_code, 401, anonymous.text)
        anon_id = anonymous.headers['x-request-id']
        self.assertTrue(_is_hex32(anon_id))
        legal = self.generation(key, client_id=client_id)
        self.assertEqual(legal.status_code, 200, legal.text)
        legal_id = legal.headers['x-request-id']
        self.assertNotEqual(anon_id, legal_id)
        outcomes = {row['request_id']: row for row in self.rows('request_outcomes', client_id)}
        self.assertEqual(set(outcomes), {anon_id, legal_id},
                         '两笔执行各自一行 outcome，不得被 INSERT OR IGNORE 合并')
        self.assertEqual(outcomes[anon_id]['http_status'], 401)
        self.assertEqual(outcomes[anon_id]['terminal_state'], 'rejected')
        self.assertEqual(outcomes[legal_id]['http_status'], 200)
        self.assertEqual(outcomes[legal_id]['terminal_state'], 'completed')
        logs = self.rows('request_logs', client_id)
        self.assertEqual(len(logs), 1, '匿名 401 不写请求日志；成功那笔必须存在')
        self.assertEqual(logs[0]['request_id'], legal_id)
        self.assertEqual(logs[0]['client_request_id'], client_id)

    def test_concurrent_collisions_never_produce_500(self) -> None:
        key = self.new_key('concurrent')
        shared = 'concurrent-' + uuid.uuid4().hex
        plan = [(shared, 0), (shared, 1), (shared, 2), (shared, 3)] + [
            (f'{shared}-{i}', i) for i in range(4)]
        results: list[tuple[int, str, str]] = []
        lock = threading.Lock()

        def worker(item):
            client_id, index = item
            client = TestClient(self.app, raise_server_exceptions=False)
            try:
                response = self.generation(key, client_id=client_id, client=client)
                with lock:
                    results.append((response.status_code,
                                    response.headers.get('x-request-id', ''),
                                    response.headers.get('x-client-request-id', '')))
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, plan))
        self.assertEqual(len(results), 8)
        statuses = [status for status, _sid, _cid in results]
        self.assertNotIn(500, statuses, f'并发碰撞不得出现 500：{statuses}')
        self.assertTrue(set(statuses) <= {200, 429}, statuses)
        execution_ids = [sid for _status, sid, _cid in results]
        self.assertTrue(all(_is_hex32(sid) for sid in execution_ids), execution_ids)
        self.assertEqual(len(set(execution_ids)), 8, '每笔执行必须有独立执行 ID')
        for status, sid, cid in results:
            self.assertIn(cid, {shared, *(f'{shared}-{i}' for i in range(4))})
        # 同一对外 ID 的 4 笔都留了各自 outcome（含被预算保护的 429）
        shared_outcomes = self.rows('request_outcomes', shared)
        self.assertEqual(len(shared_outcomes), 4, shared_outcomes)
        self.assertEqual(len({row['request_id'] for row in shared_outcomes}), 4)
        self.assertTrue(all(row['request_id'] != shared for row in shared_outcomes))

    def test_streaming_and_upstream_error_paths_keep_identity(self) -> None:
        key = self.new_key('stream-and-error')

        # ── SSE 成功路径 ──
        stream_id = 'stream-' + uuid.uuid4().hex
        streamed = self.generation(key, client_id=stream_id, stream=True)
        self.assertEqual(streamed.status_code, 200, streamed.text)
        self.assertIn('data: [DONE]', streamed.text)
        server_id = streamed.headers['x-request-id']
        self.assertTrue(_is_hex32(server_id))
        self.assertNotEqual(server_id, stream_id)
        self.assertEqual(streamed.headers['x-client-request-id'], stream_id)
        log = self.log_for(server_id)
        self.assertIsNotNone(log)
        self.assertEqual(log['client_request_id'], stream_id)
        self.assertEqual(log['stream'], 1)
        self.assertEqual(log['terminal_state'], 'completed')
        outcome = self.outcome_for(server_id)
        self.assertEqual(outcome['http_status'], 200)
        self.assertEqual(outcome['terminal_state'], 'completed')
        self.assertEqual(outcome['client_request_id'], stream_id)

        # ── 上游 500 路径 ──
        error_id = 'upstream-500-' + uuid.uuid4().hex
        failed = self.generation(key, client_id=error_id, model='fake-model:error_500')
        self.assertEqual(failed.status_code, 500, failed.text)
        failed_server_id = failed.headers['x-request-id']
        self.assertTrue(_is_hex32(failed_server_id))
        self.assertNotEqual(failed_server_id, error_id)
        self.assertEqual(failed.headers['x-client-request-id'], error_id)
        failed_log = self.log_for(failed_server_id)
        self.assertIsNotNone(failed_log)
        self.assertEqual(failed_log['client_request_id'], error_id)
        self.assertEqual(failed_log['http_status'], 500)
        self.assertEqual(failed_log['terminal_state'], 'upstream_error')
        failed_outcome = self.outcome_for(failed_server_id)
        self.assertEqual(failed_outcome['http_status'], 500)
        self.assertEqual(failed_outcome['terminal_state'], 'upstream_error')

    def test_logs_api_exposes_both_ids_without_confusing_them(self) -> None:
        """列表 API 必须能按外部 ID 找到执行 ID（否则 N40 的跨系统追踪不可用）。"""
        key = self.new_key('logs-api')
        client_id = 'lookup-' + uuid.uuid4().hex
        response = self.generation(key, client_id=client_id)
        self.assertEqual(response.status_code, 200, response.text)
        server_id = response.headers['x-request-id']
        listing = self.client.get('/api/logs?size=50', headers=self.admin)
        self.assertEqual(listing.status_code, 200, listing.text)
        items = listing.json()['items']
        matched = [item for item in items if item['client_request_id'] == client_id]
        self.assertEqual(len(matched), 1, items)
        self.assertEqual(matched[0]['request_id'], server_id)
        self.assertNotEqual(matched[0]['request_id'], matched[0]['client_request_id'])

    def test_response_headers_and_forwarded_headers(self) -> None:
        key = self.new_key('headers')
        captured: list[dict] = []
        original = gateway._upstream_headers

        def spy(upstream=None):
            headers = original(upstream)
            captured.append(dict(headers))
            return headers

        # ── 提供了合法关联 ID ──
        client_id = 'hdr-' + uuid.uuid4().hex
        with patch.object(gateway, '_upstream_headers', spy):
            response = self.generation(key, client_id=client_id)
        self.assertEqual(response.status_code, 200, response.text)
        server_id = response.headers['x-request-id']
        self.assertTrue(_is_hex32(server_id))
        self.assertNotEqual(server_id, client_id,
                            'x-request-id 必须是服务端执行 ID，不能是外部值')
        self.assertEqual(response.headers['x-client-request-id'], client_id)
        self.assertTrue(captured, '转发上游必须经过 _upstream_headers')
        forwarded = captured[-1]
        self.assertEqual(forwarded['X-Request-ID'], client_id)
        self.assertEqual(forwarded['X-Manager-Request-ID'], server_id)

        # ── 未提供关联 ID ──
        captured.clear()
        with patch.object(gateway, '_upstream_headers', spy):
            plain = self.generation(key)
        self.assertEqual(plain.status_code, 200, plain.text)
        plain_server_id = plain.headers['x-request-id']
        self.assertTrue(_is_hex32(plain_server_id))
        self.assertNotIn('x-client-request-id', plain.headers,
                         '没给合法关联 ID 时不得回显任何值')
        forwarded = captured[-1]
        self.assertEqual(forwarded['X-Request-ID'], plain_server_id,
                         '无关联 ID 时 X-Request-ID 回落为执行 ID')
        self.assertEqual(forwarded['X-Manager-Request-ID'], plain_server_id)

        # ── 非法头被丢弃：响应头干净，主键仍是执行 ID ──
        captured.clear()
        with patch.object(gateway, '_upstream_headers', spy):
            dirty = self.generation(key, headers={'X-Request-ID': 'A' * 300})
        self.assertEqual(dirty.status_code, 200, dirty.text)
        self.assertNotIn('x-client-request-id', dirty.headers)
        self.assertTrue(_is_hex32(dirty.headers['x-request-id']))
        self.assertEqual(captured[-1]['X-Manager-Request-ID'],
                         dirty.headers['x-request-id'])
        self.assertNotEqual(captured[-1]['X-Request-ID'], 'A' * 300)


class RequestIdentityMigrationTest(_HttpCase):
    """断言 12：旧形状库升级补列、旧行保留且不回填猜测。"""

    def test_old_database_upgrade_adds_columns_and_keeps_rows(self) -> None:
        # 先写入两行「历史数据」，然后把它降级成修复前的形状。
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at) '
                   "VALUES(1,'legacy','h','p',0)")
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,request_id,'
                   'terminal_state) VALUES(?,1,?,200,?,?)',
                   (int(time.time()), 'legacy-model', 'legacy-exec-id', 'completed'))
        db.execute('INSERT INTO request_outcomes(request_id,ts,http_status,terminal_state) '
                   "VALUES('legacy-exec-id',?,200,'completed')", (int(time.time()),))
        with db._lock:
            conn = db._conn
            for table in ('request_logs', 'request_outcomes'):
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
                self.assertIn('client_request_id', columns, f'{table} 新库应有该列')
                conn.execute(f'ALTER TABLE {table} DROP COLUMN client_request_id')
            conn.execute('DELETE FROM schema_migrations WHERE version=6')
            conn.execute('DROP TABLE schema_backfill_progress')
            conn.execute('PRAGMA user_version=0')
            conn.commit()
            for table in ('request_logs', 'request_outcomes'):
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
                self.assertNotIn('client_request_id', columns, '降级后的旧形状不应有该列')
            db._close_connection_locked()

        # 重新打开 = 真实升级路径（executescript(SCHEMA) → _migrate → migrations.ensure）
        db.connect()
        for table in ('request_logs', 'request_outcomes'):
            with self.subTest(table=table):
                columns = {row[1] for row in db.query(f'PRAGMA table_info({table})')}
                self.assertIn('client_request_id', columns, '升级必须补上可空列')
        self.assertEqual(int(db.query_one('SELECT MAX(version) v FROM schema_migrations '
                                          "WHERE state='applied'")['v']),
                         migrations.CODE_SCHEMA_VERSION)
        log = db.query_one('SELECT * FROM request_logs WHERE request_id=?',
                           ('legacy-exec-id',))
        self.assertIsNotNone(log, '升级不能丢历史行')
        self.assertIsNone(log['client_request_id'],
                          '历史行的关联 ID 未知 → NULL，不得用执行 ID 或其他值回填')
        outcome = db.query_one('SELECT * FROM request_outcomes WHERE request_id=?',
                               ('legacy-exec-id',))
        self.assertIsNotNone(outcome, '升级不能丢历史 outcome')
        self.assertIsNone(outcome['client_request_id'])
        # 补列后可正常写入新的关联 ID（同一张表两种形态共存）
        db.add_request_log(ts=int(time.time()), key_id=1, model='new-model',
                           status=200, request_id='new-exec-id',
                           client_request_id='new-client-id')
        fresh = db.query_one('SELECT client_request_id FROM request_logs '
                             'WHERE request_id=?', ('new-exec-id',))
        self.assertEqual(fresh['client_request_id'], 'new-client-id')


if __name__ == '__main__':
    unittest.main()
