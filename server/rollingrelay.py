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

# 部署空窗重试：`systemctl restart` 要停旧进程、起新解释器、校验整份发布签名、
# 再初始化应用，实测约 6 秒。这段时间里 connect 直接被拒。此时请求还没发出去、
# 上游也没见过它，所以重试是安全的 —— 不会重复计费，也不会重放一条已经开始的流。
#
# 2026-11-25：原来写的是 4 次、累计 9 秒。实测不够。
# 日志复原了一次真实的部署窗口（19:19:39 停、19:19:44 进程起来、19:19:54 才接住请求）：
#
#   19:19:39  503          ← 网关刚停
#   19:19:40  502  retry1  ← 端口未监听
#   19:19:42  502  retry2
#   19:19:44  502  retry3  ← 进程已 Started，但应用还没 ready
#   19:19:54  200          ← 第 5 次才成功
#
# 也就是说「进程起来」不等于「能服务」：签名校验 + 应用初始化还要约 10 秒。
# 9 秒的窗口在「进程起来」那一刻就用完了，后面 10 秒的初始化没有任何重试保护。
# 6 次累计 22.5 秒（1.5+3+4.5+6+7.5），覆盖实测 15 秒并留 7.5 秒余量。
#
# 代价：后端真的挂了时，要等 22.5 秒才报错。可以接受 —— 这里只重试 connect 阶段，
# 意味着后端完全不可达；不可达 22 秒确实该判定失败了。
STARTUP_RETRIES = 6
STARTUP_RETRY_DELAY = 1.5
# 重试要求请求体可重放，所以先整块读进内存。现实里的推理请求体只有几十 KB
# （对话 + 工具定义），这个上限是防止有人把 relay 当文件上传口。
MAX_BUFFERED_BODY = 16 * 1024 * 1024


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


def is_draining_response(response) -> bool:
    """后端是不是只在「排空、没接单」。

    判据要窄：503 且带 `Retry-After`。drain 中间件（`server/drain.py`）的应答固定
    带这个头，容量拒绝和上游 503 都不带 —— 那两类重试只会放大故障。
    """
    return response.status_code == 503 and 'retry-after' in response.headers


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

    async def _buffer_body(self, receive):
        """把请求体读进内存，让重试可以重放它。

        代价是失去流式上传能力，但 relay 只转发 JSON 推理请求，本来就没有流式
        上传的用法；换来的是部署空窗期可以安全重试。
        """
        chunks, total = [], 0
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                raise asyncio.CancelledError()
            if event['type'] != 'http.request':
                continue
            chunk = event.get('body') or b''
            total += len(chunk)
            if total > MAX_BUFFERED_BODY:
                raise RouteError('relay_body_too_large')
            if chunk:
                chunks.append(chunk)
            if not event.get('more_body', False):
                return b''.join(chunks)

    async def _send_with_startup_retry(self, build):
        """后端正在重启或排空时等它回来，而不是直接把失败甩给调用方。

        重试的两种情形都意味着「上游一个字节都没收到」：

        1. connect 阶段失败 —— 旧进程已停、新解释器还没开始 listen；
        2. 503 且带 `Retry-After` —— 旧进程收到 SIGTERM 后进入排空，drain 中间件
           对每一条新请求都回这个固定应答，它明确表示没有接单。

        2026-11-26 08:59 实测：重启窗口里唯一落空的那条请求就是第 2 种。中继把它
        原样透传，客户端看到 503；而新实例在几秒后就已经能服务了。

        一旦进入读取阶段就不再重试 —— 那时可能已经在生成答案并计费了。
        """
        last = None
        draining = None
        for attempt in range(STARTUP_RETRIES):
            try:
                response = await self.client.send(build(), stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                last = error
            else:
                if not is_draining_response(response):
                    return response
                # 排空应答里没有业务数据；关掉它再重试是安全的。
                if draining is not None:
                    await draining.aclose()
                draining = response
                last = None
            if attempt + 1 >= STARTUP_RETRIES:
                break
            await asyncio.sleep(STARTUP_RETRY_DELAY * (attempt + 1))
        if draining is not None:
            # 重试用尽：把最后那条 503 原样交给客户端，不伪造成功、也不换成 502。
            return draining
        raise last if last is not None else RuntimeError('relay_startup_retry_exhausted')

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
        try:
            payload = await self._buffer_body(receive)
        except RouteError as error:
            return await json_response(send, 413, str(error))
        body_done = asyncio.Event()
        body_done.set()
        disconnected = False
        response_started = False
        self.active[target] += 1
        self.accepted += 1

        async def forward():
            nonlocal response_started
            response = None
            try:
                def build():
                    # 每轮重试都重新解析路由。发版时路由会从旧实例切到新实例，
                    # 而旧实例正处在排空期；只认第一次捕获的 url，等于对着那扇
                    # 已经关上的门重试满 6 次（约 31 秒），最后仍把 503 交给调用方。
                    target_now = self.route()
                    url_now = self.backends[target_now].copy_with(
                        raw_path=scope.get('raw_path', scope['path'].encode()),
                        query=scope.get('query_string', b''))
                    return self.client.build_request(scope.get('method', 'GET'), url_now,
                        headers=headers, content=payload)
                response = await self._send_with_startup_retry(build)
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
