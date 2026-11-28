"""部署空窗重试：后端重启期间连不上要等它回来，而不是直接把失败甩给调用方。

背景：`systemctl restart` 实测有约 6 秒空窗（停旧进程 → 起新解释器 → 校验整份
发布签名 → 初始化应用）。这段时间里从 nginx 转发过来的推理请求 connect 直接被拒。
旧实现立刻回 502 —— 2026-11-24 19:19 那次部署因此丢掉了 4 个真实调用。

这里固定住边界：只重试 connect 阶段的失败（上游一个字节都没收到），读取阶段一旦
开始就绝不重试（那时可能已经在生成答案并计费了）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from server import drain, rollingrelay
from server.rollingrelay import StreamingRelay


class BytesBody(httpx.AsyncByteStream):
    def __init__(self, content=b'data: [DONE]\n\n'):
        self.content = content

    async def __aiter__(self):
        yield self.content


class OneShot(httpx.AsyncByteStream):
    """只能迭代一次的真流：`BytesBody` 可以重启，真 socket 不行。

    预读把流读到一半后，只有把迭代器接回去才能继续透传；重启式 fake 会掩盖
    「没有接回」的 bug。
    """

    def __init__(self, *chunks):
        self.chunks = list(chunks)
        self.used = False

    async def __aiter__(self):
        if self.used:
            raise httpx.StreamConsumed()
        self.used = True
        for chunk in self.chunks:
            yield chunk



class Truncated(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"error":'
        raise httpx.ReadError('backend truncated the 503 body')


# 排空应答的真实形状：server/drain.py 的 DrainMiddleware → protocolerrors.response。
DRAIN_BODY = json.dumps({'error': {'type': 'server_error', 'code': 'gateway_draining',
                                  'message': 'Gateway is draining for maintenance; retry later.'}},
                        ensure_ascii=False).encode()
# 准入拒绝的真实形状：server/observability.py 的容量拒绝，同样是 503 + Retry-After: 1。
CAPACITY_BODY = json.dumps({'error': {'type': 'server_error', 'code': 'gateway_capacity_exceeded',
                                     'message': '网关请求收尾容量已满，请稍后重试'}},
                           ensure_ascii=False).encode()


class StartupRetryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.staging = tempfile.TemporaryDirectory()
        self.route = Path(self.staging.name) / 'relay-route.json'
        self.route.write_text(json.dumps({'epoch': 1, 'target': 'old'}), encoding='utf-8')
        os.chmod(self.staging.name, 0o700)
        os.chmod(self.route, 0o600)
        self.relay = StreamingRelay(
            {'old': 'http://127.0.0.1:7866', 'new': 'http://127.0.0.1:7865'},
            self.route, maximum=8)

    def tearDown(self):
        self.staging.cleanup()

    def _use(self, provider):
        self.relay.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    async def _post(self, body=None):
        transport = httpx.ASGITransport(app=self.relay)
        async with httpx.AsyncClient(transport=transport, base_url='http://relay') as client:
            return await client.post('/v1/chat/completions', json=body or {'model': 'x'})

    async def test_connect_failure_waits_for_the_backend_to_come_back(self):
        attempts = []

        async def provider(request):
            attempts.append(request)
            if len(attempts) == 1:
                raise httpx.ConnectError('backend is still restarting', request=request)
            return httpx.Response(200, stream=BytesBody(),
                                  headers={'content-type': 'text/event-stream'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.relay.transport_errors, 0)

    async def test_retry_replays_the_same_body(self):
        seen = []

        async def provider(request):
            seen.append(request.content)
            if len(seen) == 1:
                raise httpx.ConnectError('down', request=request)
            return httpx.Response(200, stream=BytesBody())

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post({'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0], seen[1])

    async def test_gives_up_after_the_startup_budget(self):
        attempts = []

        async def provider(request):
            attempts.append(request)
            raise httpx.ConnectError('never comes back', request=request)

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(attempts), rollingrelay.STARTUP_RETRIES)
        self.assertEqual(self.relay.transport_errors, 1)

    async def test_oversized_body_is_refused_before_any_forwarding(self):
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(200, stream=BytesBody())

        self._use(provider)
        with mock.patch.object(rollingrelay, 'MAX_BUFFERED_BODY', 8):
            response = await self._post({'model': 'far too large for the probe limit'})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(attempts, [])

    async def test_draining_503_is_retried_instead_of_passed_through(self):
        """排空期的 503 不是业务失败：等它起来就行。

        2026-11-26 08:59:05 的真实丢单：重启窗口里旧实例正在排空，对每一条新请求
        回「503 + Retry-After」，中继原样透传，客户端看到失败 —— 而六秒后新实例
        其实已经能服务。这类应答里没有任何业务数据，重试是安全的。
        """
        attempts = []

        async def provider(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(503, stream=BytesBody(DRAIN_BODY),
                                      headers={'retry-after': '1', 'connection': 'close',
                                               'content-type': 'application/json'})
            return httpx.Response(200, stream=BytesBody(),
                                  headers={'content-type': 'text/event-stream'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(attempts), 2, '排空期的 503 被原样透传了')
        self.assertEqual(self.relay.transport_errors, 0)

    async def test_plain_503_without_retry_after_is_not_retried(self):
        """上游 503 不带 Retry-After：重试只会放大故障。"""
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(503, stream=BytesBody(),
                                  headers={'content-type': 'application/json'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(attempts), 1, '没有 Retry-After 的 503 不该被重试')

    async def test_exhausted_draining_retries_still_answer_503(self):
        """重试用尽时把最后那条 503 交给客户端，不换成 502、也不伪造成功。"""
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(503, stream=BytesBody(DRAIN_BODY),
                                  headers={'retry-after': '1',
                                           'content-type': 'application/json'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(attempts), rollingrelay.STARTUP_RETRIES)
        self.assertEqual(response.headers['retry-after'], '1')
        self.assertEqual(response.json()['error']['code'], 'gateway_draining')

    async def test_capacity_rejection_is_answered_once_and_left_intact(self):
        """准入拒绝是 503 + Retry-After: 1，但它不是排空：只能发一次。

        server/observability.py 的容量拒绝与 server/drain.py 的排空应答头一模一样
        （503、Retry-After: 1、Connection: close），旧判据因此把一条**已经被明确
        拒绝**的 POST 重放 6 次、约 22.5 秒，过载被放大 6 倍。判据必须看 body 的
        code；同时客户端看到的还是原来那条 503 和它的 code，不改成 502。
        """
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(503, stream=OneShot(CAPACITY_BODY),
                                  headers={'retry-after': '1', 'connection': 'close',
                                           'content-type': 'application/json'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(len(attempts), 1, '容量拒绝被当成排空重放了')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['retry-after'], '1')
        self.assertEqual(response.json()['error']['code'], 'gateway_capacity_exceeded')
        self.assertEqual(self.relay.transport_errors, 0)

    async def test_unreadable_503_body_is_not_treated_as_drain(self):
        """读不出 code 的 503 一律不重试：宁可透传，也不凭猜测重放 POST。"""
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(503, stream=OneShot(b'<html>busy</html>'),
                                  headers={'retry-after': '1',
                                           'content-type': 'text/html'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b'<html>busy</html>')
        self.assertEqual(len(attempts), 1)

    async def test_oversized_503_body_is_forwarded_intact(self):
        """超过判据上限的 503 body 一个字节都不多读，透传时也必须完整。

        判据只认几千字节的错误信封；声明就超长的应答直接放弃判断，交给透传。
        """
        body = b'{"error": {"code": "gateway_capacity_exceeded", "message": "' + b'x' * 6000 + b'"}}'
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(503, stream=OneShot(body),
                                  headers={'retry-after': '1', 'content-type': 'application/json',
                                           'content-length': str(len(body))})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, body)

    async def test_oversized_chunked_503_body_is_forwarded_intact(self):
        """没有 content-length 时边读边数：超限立即停手，已读字节接回去。"""
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(503, stream=OneShot(b'{"error":', b' ' + b'y' * 5000,
                                                      b' ' + b'z' * 1000 + b'}'),
                                  headers={'retry-after': '1', 'content-type': 'application/json'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(response.content), len(b'{"error": ' + b'y' * 5000 + b' ' + b'z' * 1000 + b'}'))

    async def test_truncated_503_body_is_transport_uncertain_not_a_passthrough(self):
        """预读失败既不能透传也不能重放：按传输不确定回 502，绝不伪装成功。"""
        attempts = []

        async def provider(request):
            attempts.append(request)
            return httpx.Response(503, stream=Truncated(),
                                  headers={'retry-after': '1', 'content-type': 'application/json'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error']['code'], 'relay_upstream_transport_uncertain')
        self.assertEqual(self.relay.transport_errors, 1)

    async def test_the_real_drain_and_admission_envelopes_are_told_apart(self):
        """判据对的必须是两个真实的错误信封，而不是测试里编的字符串。

        drain 的应答由 `server/drain.py` 的中间件产出；准入拒绝由
        `server/observability.py` 走 `protocolerrors.response` 产出。两者状态码和
        `Retry-After` 完全相同，只有 body 的 code 不同 —— 这里把两条真实路径都跑
        一遍，防止某一侧的 code 字符串漂移后判据静默失效。
        """
        from server import protocolerrors

        def envelope(code):
            return protocolerrors.response(
                {'error': {'code': code, 'message': code}}, 503,
                headers={'Retry-After': '1', 'Connection': 'close'})

        drain.reset_for_tests()
        self.addCleanup(drain.reset_for_tests)
        drain.begin(persist=False)

        async def unreachable(scope, receive, send):
            raise AssertionError('draining middleware must not reach the app')

        messages = []

        async def collect(message):
            messages.append(message)

        await drain.DrainMiddleware(unreachable)(
            {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': '/v1/chat/completions',
             'raw_path': b'/v1/chat/completions', 'query_string': b'', 'headers': [],
             'server': ('127.0.0.1', 8000), 'client': ('127.0.0.1', 10000)},
            unreachable, collect)
        start = next(message for message in messages if message['type'] == 'http.response.start')
        drained = httpx.Response(start['status'], content=b''.join(
            message.get('body', b'') for message in messages
            if message['type'] == 'http.response.body'),
            headers=[(key.decode(), value.decode()) for key, value in start['headers']])
        self.assertEqual(drained.json()['error']['code'], rollingrelay.DRAIN_ERROR_CODE)
        self.assertTrue(await rollingrelay.is_draining_response(drained))

        rejected = envelope('gateway_capacity_exceeded')
        self.assertEqual(rejected.status_code, 503)
        self.assertEqual(rejected.headers['retry-after'], '1')
        self.assertFalse(await rollingrelay.is_draining_response(
            httpx.Response(rejected.status_code, content=rejected.body,
                           headers=dict(rejected.headers))))

    async def test_read_phase_failure_is_not_replayed(self):
        attempts = []

        async def provider(request):
            attempts.append(request)
            raise httpx.ReadError('upstream dropped after accepting', request=request)

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(attempts), 1)

    async def test_draining_503_follows_the_new_route_after_switch(self):
        """排空期的 503 不仅要重试，还要跟到已经就绪的新实例上。

        2026-11-26 08:59 那次真实丢单：旧实例进入排空回「503 + Retry-After」，
        发版脚本随后把路由切到新实例；但中继只认第一次捕获的地址，六次重试
        全打在旧实例上，最后仍把 503 交给调用方 —— 新实例其实早就绪了。
        """
        seen = []

        async def provider(request):
            seen.append(f'{request.url.host}:{request.url.port}')
            if len(seen) == 1:
                # 旧实例排空中；同一时刻发版脚本把路由切到新实例。
                self.route.write_text(json.dumps({'epoch': 2, 'target': 'new'}),
                                      encoding='utf-8')
                os.chmod(self.route, 0o600)
                return httpx.Response(503, stream=BytesBody(DRAIN_BODY),
                                      headers={'retry-after': '1',
                                               'content-type': 'application/json'})
            return httpx.Response(200, stream=BytesBody(),
                                  headers={'content-type': 'text/event-stream'})

        self._use(provider)
        with mock.patch.object(rollingrelay, 'STARTUP_RETRY_DELAY', 0):
            response = await self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 2)
        self.assertIn('7866', seen[0])
        self.assertIn('7865', seen[1])
        self.assertEqual(self.relay.transport_errors, 0)
