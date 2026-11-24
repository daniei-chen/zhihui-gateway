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

from server import rollingrelay
from server.rollingrelay import StreamingRelay


class BytesBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: [DONE]\n\n'


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
