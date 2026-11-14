"""v1.2.100：Trae 原生协议适配器没有测试。

## 为什么补

静态检查发现 `server/routers/trae_native.py` 的模块名**从未出现在任何测试里**。

它是 Anthropic 原始请求到 Trae 引擎 `/v1/messages` 的**薄转发层**（89 行），
但它做几件必须正确的事：

| 职责 | 错了会怎样 |
| --- | --- |
| 拼上游地址并校验 | 把请求发到空地址 |
| 头构造与默认值 | 上游拒鉴权，或版本不匹配 |
| 流式 / 非流式分流 | 非流式请求挂住不返回 |
| **每条路径都关客户端** | 连接慢慢泄漏，最后池子枯竭 |
| 上游 4xx 的记录与透传 | 错误被吞掉，排查时无线索 |

**最后一条最容易漏**：这段代码在四条不同路径上各自负责关闭客户端，
少关一条就是慢性泄漏，而它只在长时间运行后才显现。

本测试守的就是这四条路径的关闭与记录。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from server.routers import trae_native


class _Client:
    """记录 post/send/aclose 的最小客户端。"""

    def __init__(self, *, response=None, raise_on_send=None, chunks=()):
        self.response = response
        self.raise_on_send = raise_on_send
        self.chunks = list(chunks)
        self.closed = 0
        self.posted = []
        self.sent = []
        self.built = []

    async def post(self, url, content=None, headers=None):
        self.posted.append((url, content, headers))
        if self.raise_on_send is not None:
            raise self.raise_on_send
        return self.response

    def build_request(self, method, url, content=None, headers=None):
        self.built.append((method, url, content, headers))
        return ('REQ', method, url)

    async def send(self, request, stream=False):
        self.sent.append((request, stream))
        if self.raise_on_send is not None:
            raise self.raise_on_send
        return self.response

    async def aclose(self):
        self.closed += 1


def _response(status=200, content=b'{"ok":true}', *, chunks=(), headers=None):
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    resp = httpx.Response(
        status,
        content=content,
        headers=headers or {'content-type': 'application/json'},
    )
    resp._request = httpx.Request('POST', 'http://upstream/v1/messages')

    async def aiter_raw():
        for chunk in chunks:
            yield chunk

    resp.aiter_raw = aiter_raw

    async def aclose():
        resp.is_closed = True

    resp.aclose = aclose
    return resp


class _Upstream(dict):
    pass


def _body():
    return {'model': 'trae/x', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': False}


def _request(headers=None):
    scope = {'type': 'http', 'headers': []}
    req = mock.Mock()
    req.headers = headers or {}
    return req


class MissingUpstreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_base_url_returns_502(self):
        result = await trae_native.trae_native_messages(
            _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
            'ua', _Upstream(base_url='', api_key='k'), False, None)
        self.assertEqual(result.status_code, 502)
        payload = json.loads(result.body)
        self.assertEqual(payload['type'], 'error')

    async def test_relative_base_url_is_rejected(self):
        """不是 http(s) 开头的地址不能拼成 request —— 那会打到本机。"""
        result = await trae_native.trae_native_messages(
            _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
            'ua', _Upstream(base_url='127.0.0.1:8080', api_key='k'), False, None)
        self.assertEqual(result.status_code, 502)


class NonStreamingTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, response, *, raise_on_post=None):
        client = _Client(response=response, raise_on_send=raise_on_post)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record') as rec:
            return client, rec

    async def test_successful_non_streaming_returns_body_and_closes(self):
        resp = _response(200, b'{"content":"hello"}')
        client = _Client(response=resp)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record'):
            result = await trae_native.trae_native_messages(
                _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
                'ua', _Upstream(base_url='http://up', api_key='k'), False, None)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.closed, 1, '非流式路径必须关闭客户端')

    async def test_upstream_4xx_is_passed_through_and_recorded(self):
        resp = _response(429, b'rate limited')
        client = _Client(response=resp)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record') as rec:
            result = await trae_native.trae_native_messages(
                _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
                'ua', _Upstream(base_url='http://up', api_key='k'), False, None)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(client.closed, 1)
        self.assertTrue(rec.called)
        # 第 7 个位置参数是错误摘要：非 2xx 必须带上它
        args = rec.call_args.args
        self.assertEqual(args[4], 429)
        self.assertTrue(args[9], '4xx/5xx 必须记录错误摘要')

    async def test_headers_carry_key_and_default_anthropic_version(self):
        resp = _response(200, b'{}')
        client = _Client(response=resp)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record'):
            await trae_native.trae_native_messages(
                _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
                'ua', _Upstream(base_url='http://up', api_key='secret'), False, None)
        _url, _content, headers = client.posted[0]
        self.assertEqual(headers['x-api-key'], 'secret')
        self.assertEqual(headers['anthropic-version'], '2023-06-01',
                         '客户端没给版本时必须补默认值')
        self.assertEqual(headers['accept'], 'application/json')

    async def test_client_version_header_overrides_default(self):
        resp = _response(200, b'{}')
        client = _Client(response=resp)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record'):
            await trae_native.trae_native_messages(
                _request({'anthropic-version': '2024-01-01'}), _body(), {'id': 1},
                '1.2.3.4', 'trae/x', 'x', 'ua',
                _Upstream(base_url='http://up', api_key='k'), False, None)
        _url, _content, headers = client.posted[0]
        self.assertEqual(headers['anthropic-version'], '2024-01-01')


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_closes_both_response_and_client(self):
        resp = _response(200, b'', chunks=[b'data: a\n', b'data: b\n'],
                         headers={'content-type': 'text/event-stream'})
        client = _Client(response=resp)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record') as rec:
            result = await trae_native.trae_native_messages(
                _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
                'ua', _Upstream(base_url='http://up', api_key='k'), True, None)
            # 消费完整个流，触发 finally
            chunks = [chunk async for chunk in result.body_iterator]
        self.assertEqual(chunks, [b'data: a\n', b'data: b\n'])
        self.assertEqual(client.closed, 1, '流式路径也必须关闭客户端')
        self.assertTrue(rec.called)

    async def test_streaming_upstream_error_does_not_start_a_stream(self):
        """上游在流开始前就报错时，必须返回普通响应而不是半截流。"""
        resp = _response(503, b'upstream down', headers={'content-type': 'application/json'})
        client = _Client(response=resp)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record'):
            result = await trae_native.trae_native_messages(
                _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
                'ua', _Upstream(base_url='http://up', api_key='k'), True, None)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(client.closed, 1)

    async def test_transport_failure_while_opening_stream_closes_client(self):
        """开流就失败时不能把客户端漏掉 —— 那是纯泄漏。"""
        boom = httpx.ConnectError('refused')
        client = _Client(raise_on_send=boom)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record'):
            with self.assertRaises(httpx.ConnectError):
                await trae_native.trae_native_messages(
                    _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
                    'ua', _Upstream(base_url='http://up', api_key='k'), True, None)
        self.assertEqual(client.closed, 1, '开流失败也要关客户端')

    async def test_accept_header_announces_event_stream(self):
        resp = _response(200, b'', chunks=[], headers={'content-type': 'text/event-stream'})
        client = _Client(response=resp)
        with mock.patch.object(trae_native.config, 'http_client', return_value=client), \
                mock.patch.object(trae_native.gateway, '_record'):
            result = await trae_native.trae_native_messages(
                _request(), _body(), {'id': 1}, '1.2.3.4', 'trae/x', 'x',
                'ua', _Upstream(base_url='http://up', api_key='k'), True, None)
            [chunk async for chunk in result.body_iterator]
        _method, _url, _content, headers = client.built[0]
        self.assertEqual(headers['accept'], 'text/event-stream')


if __name__ == '__main__':
    unittest.main()
