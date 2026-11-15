"""Stable HTTP/1 streaming relay behind the existing TLS/HTTP2 terminator.

Each request captures a backend exactly once. Changing a private atomic route
file affects subsequent requests; it neither reloads Nginx nor replays a POST.
The separate control ASGI app must be bound to a private Unix socket, never to
the public listener. This component has no production launcher in this change.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat

import httpx

from .services.asyncwork import joined_task

HOP = {b'connection', b'proxy-connection', b'keep-alive', b'transfer-encoding',
       b'te', b'trailer', b'upgrade', b'proxy-authenticate', b'proxy-authorization'}
INFERENCE_PATHS = frozenset({'/v1/chat/completions', '/v2/chat/completions',
    '/v1/responses', '/responses', '/v1/messages', '/v1/messages/count_tokens', '/v1/models'})


def end_to_end(headers):
    headers = [(key.lower(), value) for key, value in headers]
    named = set()
    for key, value in headers:
        if key == b'connection':
            named.update(item.strip().lower() for item in value.split(b','))
    return [(key, value) for key, value in headers if key not in HOP | named]


async def json_response(send, status, code):
    body = json.dumps({'error': {'type': 'server_error', 'code': code,
                     'message': code}}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'),
                            (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})


class RouteError(RuntimeError):
    pass


class StreamingRelay:
    def __init__(self, backends, route_file, *, maximum=128, client=None, allowed_paths=INFERENCE_PATHS):
        if not 1 <= maximum <= 1024 or not 1 <= len(backends) <= 8:
            raise ValueError('invalid_relay_bounds')
        self.backends = {}
        for name, address in backends.items():
            url = httpx.URL(address)
            if (not isinstance(name, str) or not 1 <= len(name) <= 96
                    or url.scheme != 'http' or url.host not in ('127.0.0.1', '::1')
                    or not url.port or url.userinfo or url.path != '/' or url.query or url.fragment):
                raise ValueError('only_declared_loopback_backends_allowed')
            self.backends[name] = url
        self.path = Path(route_file)
        self.maximum = maximum
        self.allowed_paths = frozenset(allowed_paths)
        self.active = {name: 0 for name in self.backends}
        self.epoch, self.target = 0, None
        self.accepted, self.transport_errors = 0, 0
        # read 原先给的是 None（永不超时）。对 SSE 来说"不设总时长"是对的 ——
        # 一次回答可以连续流几分钟甚至更久。但 read 在 httpx 里是**相邻两次读之间**
        # 的间隔，不是总时长：给 None 意味着上游只要不发也不关（半开连接、被挂起的
        # 上游进程），这条连接就一直占着 `maximum` 里的一个名额，谁也不回收。
        # 128 个这样的流就能把转发层永久打满，而它背后没有任何计时器。
        #
        # 600 秒是"十分钟没有任何字节"的判据：正常 SSE 每隔几秒就有数据或心跳，
        # 真到十分钟没动静时那条上游已经没救了。它比主服务的上游读超时（300 秒）
        # 更宽松，所以不会比后端先动手杀死一条还活着的流。
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0,
                limits=httpx.Limits(max_connections=maximum, max_keepalive_connections=min(16, maximum))),
            timeout=httpx.Timeout(connect=3, read=600, write=30, pool=5))

    def route(self):
        parent = self.path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode) or os.name == 'posix' and (
                parent.st_uid not in (0, os.geteuid()) or parent.st_mode & 0o022):
            raise RouteError('unsafe_route_directory')
        fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096
                    or os.name == 'posix' and (
                        info.st_uid not in (0, os.geteuid())
                        or (os.geteuid() != 0 and info.st_gid not in (0, os.getegid()))
                        or info.st_mode & 0o022
                        or not os.access(self.path, os.R_OK))):
                raise RouteError('unsafe_route_file')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = json.loads(stream.read(4097))
        finally:
            os.close(fd)
        if (set(data) != {'epoch', 'target'} or type(data['epoch']) is not int
                or data['epoch'] < 1 or data['target'] not in self.backends
                or data['epoch'] < self.epoch
                or data['epoch'] == self.epoch and data['target'] != self.target):
            raise RouteError('invalid_or_stale_route')
        self.epoch, self.target = data['epoch'], data['target']
        return self.target

    def snapshot(self):
        try:
            self.route()
            ready = True
        except Exception:
            ready = False
        return {'scope': 'streaming-relay/v1', 'ready': ready, 'epoch': self.epoch,
                'target': self.target, 'active': dict(self.active),
                'accepted': self.accepted, 'transport_errors': self.transport_errors}

    async def close(self):
        if any(self.active.values()):
            raise RouteError('relay_still_owns_requests')
        await self.client.aclose()

    async def control_app(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self._lifespan(receive, send)
        if scope['type'] != 'http':
            return
        if scope.get('method') != 'GET' or scope.get('path') != '/snapshot':
            return await json_response(send, 404, 'not_found')
        body = json.dumps(self.snapshot(), separators=(',', ':')).encode()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/json'),
                                (b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})

    async def _lifespan(self, receive, send):
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self._lifespan(receive, send)
        if scope['type'] != 'http':
            # Upgrade/WebSocket is outside this HTTP inference-only contract.
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            return
        if scope.get('path') not in self.allowed_paths:
            return await json_response(send, 404, 'not_found')
        if sum(self.active.values()) >= self.maximum:
            return await json_response(send, 503, 'relay_capacity_exceeded')
        try:
            target = self.route()
        except Exception:
            return await json_response(send, 503, 'deployment_route_unavailable')
        incoming = list(scope.get('headers', []))
        lengths = [value for key, value in incoming if key.lower() == b'content-length']
        encodings = [value for key, value in incoming if key.lower() == b'transfer-encoding']
        if len(lengths) > 1 or lengths and encodings:
            return await json_response(send, 400, 'ambiguous_request_framing')
        headers = end_to_end(incoming)
        # HTTPX URL.copy_with preserves the declared authority even for // paths.
        url = self.backends[target].copy_with(raw_path=scope.get('raw_path', scope['path'].encode()),
                                              query=scope.get('query_string', b''))
        body_done = asyncio.Event()
        disconnected = False
        response_started = False
        self.active[target] += 1
        self.accepted += 1

        async def incoming_body():
            nonlocal disconnected
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    disconnected = True
                    raise asyncio.CancelledError()
                if event['type'] != 'http.request':
                    continue
                if event.get('body'):
                    yield event['body']
                if not event.get('more_body', False):
                    body_done.set()
                    return

        async def forward():
            nonlocal response_started
            response = None
            try:
                request = self.client.build_request(scope.get('method', 'GET'), url,
                    headers=headers, content=incoming_body())
                response = await self.client.send(request, stream=True)
                response_started = True
                await send({'type': 'http.response.start', 'status': response.status_code,
                            'headers': end_to_end(response.headers.raw)})
                async for chunk in response.aiter_raw():
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            except asyncio.CancelledError:
                raise
            except Exception:
                self.transport_errors += 1
                if response_started:
                    # Do not forge SSE success or replay an uncertain upstream POST.
                    raise RuntimeError('relay_response_interrupted') from None
                if not disconnected:
                    await json_response(send, 502, 'relay_upstream_transport_uncertain')
            finally:
                if response is not None:
                    await joined_task(asyncio.create_task(response.aclose()))

        task = asyncio.create_task(forward())
        async def watch_disconnect():
            nonlocal disconnected
            await body_done.wait()
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    disconnected = True
                    task.cancel()
                    return
        watcher = asyncio.create_task(watch_disconnect())
        try:
            await task
        except asyncio.CancelledError:
            if not disconnected:
                raise
        finally:
            async def cleanup():
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                # On outer/repeated cancellation, join owned HTTP close work.
                if not task.done():
                    task.cancel()
                    try:
                        await joined_task(task)
                    except asyncio.CancelledError:
                        pass
                self.active[target] -= 1
            await joined_task(asyncio.create_task(cleanup()))
