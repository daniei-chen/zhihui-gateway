"""出站客户端要**复用**，且不能跨事件循环复用（issue #144）。

报障：Windows 原生部署上切换页面要 1~3 秒，根因是每次出站都新建 httpx.AsyncClient
——在那台机器上「仅构造 + aclose」就要约 1 秒（加载系统证书库建 SSL 上下文），
而账号/状态/模型这些接口每次都要问上游几次。

这里钉住四件事：
  1. 同一个循环 + 同一组超时/代理 → **同一传输连接池**（每次调用的状态独立）；
  2. 不同超时组合 → 各自一个（不能把 5 秒超时和 30 秒超时混成一个）；
  3. `async with` 退出只关闭调用状态，**不关闭可复用传输**；
  4. 跨事件循环不复用（httpx 的连接池绑在创建它的循环上；同步路由里
     `asyncio.run` 另起循环那条路——见 modelcatalog.fetch_ids_blocking——
     拿到旧循环的客户端就会炸）。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402


class TrackedTransport(__import__('httpx').MockTransport):
    def __init__(self):
        import httpx
        super().__init__(lambda request: httpx.Response(200, content=b'ok'))
        self.closes = 0
        self.owner = asyncio.get_running_loop()

    async def aclose(self):
        assert asyncio.get_running_loop() is self.owner
        self.closes += 1


class HttpClientReuseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await config.close_clients()
        self.proxy_patch = mock.patch.object(config, 'effective_http_proxy', return_value='')
        self.proxy_patch.start()
        self.addCleanup(self.proxy_patch.stop)

    async def asyncTearDown(self):
        await config.close_clients()

    async def fetch(self, **kwargs):
        async with config.http_client(10, connect=3, **kwargs) as client:
            response = await client.get('https://unit.invalid/')
            self.assertEqual(response.content, b'ok')
        return client

    async def test_same_config_reuses_transport_with_distinct_clients(self):
        transport = TrackedTransport()
        with mock.patch.object(config, '_build_transport', return_value=transport) as build:
            first, second = await self.fetch(), await self.fetch()
        self.assertIsNot(first, second, 'Cookie/headers must belong to each lease')
        self.assertEqual(build.call_count, 1, 'TLS transport construction must be reused')
        self.assertEqual(transport.closes, 0)

    async def test_lease_exit_closes_only_the_lease(self):
        transport = TrackedTransport()
        with mock.patch.object(config, '_build_transport', return_value=transport):
            client = await self.fetch()
        self.assertTrue(client.is_closed)
        self.assertEqual(transport.closes, 0, 'An idle cached transport remains reusable')

    async def test_different_timeouts_get_separate_transports(self):
        with mock.patch.object(config, '_build_transport', side_effect=lambda _: TrackedTransport()) as build:
            async with config.http_client(10, connect=3) as slow:
                await slow.get('https://unit.invalid/')
            async with config.http_client(5, connect=2) as quick:
                await quick.get('https://unit.invalid/')
        self.assertEqual(build.call_count, 2)
        self.assertEqual(slow.timeout.connect, 3)
        self.assertEqual(quick.timeout.connect, 2)

    async def test_different_proxy_gets_separate_transport(self):
        with mock.patch.object(config, '_build_transport', side_effect=lambda _: TrackedTransport()) as build:
            await self.fetch(proxy='')
            await self.fetch(proxy='http://127.0.0.1:7890')
        self.assertEqual(build.call_count, 2)

    async def test_no_repeated_transport_construction_on_second_borrow(self):
        transport = TrackedTransport()
        with mock.patch.object(config, '_build_transport', return_value=transport) as build:
            for _ in range(3):
                await self.fetch()
        self.assertEqual(build.call_count, 1)
        self.assertEqual(transport.closes, 0)

    async def test_close_clients_closes_idle_transports_and_clears_cache(self):
        transport = TrackedTransport()
        with mock.patch.object(config, '_build_transport', return_value=transport):
            await self.fetch()
        await config.close_clients()
        self.assertEqual(transport.closes, 1)
        self.assertEqual(config._CLIENTS, {})

    async def test_repeated_lease_close_does_not_close_another_lease(self):
        transport = TrackedTransport()
        with mock.patch.object(config, '_build_transport', return_value=transport):
            first = config.http_client(10, connect=3)
            second = config.http_client(10, connect=3)
            try:
                await first.get('https://unit.invalid/')
                await first.aclose()
                await first.aclose()
                self.assertFalse(second.is_closed)
                self.assertEqual((await second.get('https://unit.invalid/')).content, b'ok')
            finally:
                await first.aclose()
                await second.aclose()
        self.assertEqual(transport.closes, 0)

    async def test_client_surface_without_context_manager(self):
        client = config.http_client(10, connect=3)
        try:
            self.assertTrue(hasattr(client, 'post') and hasattr(client, 'stream'))
            self.assertEqual(client.timeout.connect, 3)
            self.assertIn('user-agent', client.headers)
        finally:
            await client.aclose()


class LoopOwnershipTest(unittest.TestCase):
    def test_loop_exit_cleans_transports_without_cross_loop_reuse(self):
        built = []
        def factory(_):
            transport = TrackedTransport()
            built.append(transport)
            return transport
        async def scenario():
            async with config.http_client(1, proxy='') as client:
                await client.get('https://unit.invalid/')
        with mock.patch.object(config, '_build_transport', side_effect=factory):
            asyncio.run(scenario())
            asyncio.run(scenario())
        self.assertEqual(len(built), 2)
        self.assertIsNot(built[0].owner, built[1].owner)
        self.assertEqual([value.closes for value in built], [1, 1])
        self.assertEqual(config._CLIENTS, {})

    def test_outside_event_loop_is_not_cached(self):
        direct = config.http_client(10, connect=3, proxy='')
        self.assertTrue(hasattr(direct, 'aclose'))
        self.assertEqual(config._CLIENTS, {})
        asyncio.run(direct.aclose())


async def _aclose(client) -> None:  # noqa: ANN001
    await client.aclose()




class ProxySchemeSupportTest(unittest.TestCase):
    """代理线路支持哪些协议：http / https / socks5（socks5 靠 socksio）。

    为什么单独钉：socks5 支持**完全依赖一个可选依赖** —— httpx 没装 socksio 时，
    **构造客户端就抛** `ImportError: Using SOCKS proxy, but the 'socksio' package is
    not installed`。而住宅代理服务商给凭据时常常直接给 socks5://（实测 ProxyLane
    就是），用户照抄就撞上这句「看起来像自己少装了东西」的报错。

    所以这里两条一起钉：依赖必须声明在 requirements 里，且三种协议都能构造出客户端。
    （只构造不发请求 —— 能不能连通由代理服务商决定，不是本用例的事。）
    """

    def setUp(self) -> None:
        config._CLIENTS.clear()
        self.addCleanup(config._CLIENTS.clear)

    def test_socksio_is_declared_in_requirements(self) -> None:
        req = (Path(__file__).resolve().parents[2]
               / 'server' / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('socksio', req,
                      'requirements 里没有 socksio —— 填 socks5:// 的用户会看到 '
                      '「socksio 未安装」而不是「代理不通」')

    def test_all_three_schemes_are_accepted(self) -> None:
        async def scenario():
            transports = []
            for scheme in ('http://user:pass@127.0.0.1:7890',
                           'https://user:pass@127.0.0.1:7890',
                           'socks5://user:pass@127.0.0.1:1080'):
                transport = config._build_transport(scheme)
                transports.append(transport)
                await transport.aclose()
            return transports
        transports = asyncio.run(scenario())
        self.assertEqual(len({id(value) for value in transports}), 3)


class InternalHostBypassTest(unittest.TestCase):
    """配了出口代理时，内网 / 本机目标必须**直连**。

    为什么要有：WB_HTTP_PROXY 此前作用于**全部**出站请求，包括「面板 → 上游」那一跳。
    上游在容器内网（`workbuddy2api` / `host.docker.internal`）或本机（`127.0.0.1:7863`）
    时，把这一跳交给外部代理必然连不上 —— 用户按说明配好代理，结果面板读不到上游状态、
    账号页全空，报错还看不出原因（`.env.example` 里那句警告只能靠用户自己躲）。
    """

    def setUp(self) -> None:
        config._CLIENTS.clear()
        self.addCleanup(config._CLIENTS.clear)

    def test_internal_targets_are_recognized(self) -> None:
        for host in ('127.0.0.1', 'localhost', '::1', 'host.docker.internal',
                     'workbuddy2api', 'myhost.lan', '10.1.2.3', '172.16.0.9',
                     '192.168.1.5', '169.254.0.1'):
            with self.subTest(host=host):
                self.assertTrue(config._is_internal_host(host),
                                f'{host} 没被当成内网目标 —— 这一跳会被交给代理')

    def test_public_targets_are_not_internal(self) -> None:
        for host in ('8.8.8.8', '93.184.216.34', 'api.tencent.com', '172.32.0.1',
                     'proxy.example:7890', '', None):
            with self.subTest(host=host):
                self.assertFalse(config._is_internal_host(host),
                                 f'{host} 被误判成内网目标 —— 配了代理却不走代理了')

    def test_internal_target_does_not_touch_the_proxy(self) -> None:
        """真发两次请求，看**谁被连上了**：内网目标连目标，公网目标连代理。

        异常文本靠不住（httpx 会把它包装成「All connection attempts failed」），
        所以起两个本地监听口当靶子：一个是「代理」，一个是「内网目标」。
        路由正确时 —— 内网那次只敲目标口，公网那次只敲代理口。
        """
        hits = {'proxy': 0, 'target': 0}
        servers: dict[str, socket.socket] = {}
        for name in ('proxy', 'target'):
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', 0))
            srv.listen(5)
            servers[name] = srv

        def serve(name: str) -> None:
            while True:
                try:
                    conn, _ = servers[name].accept()
                except OSError:
                    return
                hits[name] += 1
                try:
                    conn.close()
                except OSError:
                    pass

        threads = [threading.Thread(target=serve, args=(n,), daemon=True) for n in servers]
        for t in threads:
            t.start()
        proxy_port = servers['proxy'].getsockname()[1]
        target_port = servers['target'].getsockname()[1]

        async def scenario():
            async with config.http_client(
                    3, proxy=f'http://127.0.0.1:{proxy_port}') as client:
                for url in (f'http://127.0.0.1:{target_port}/x',   # 内网 → 直连
                            'http://93.184.216.34/x'):             # 公网 → 走代理
                    try:
                        await client.get(url)
                    except Exception:  # noqa: BLE001 —— 靶子会立刻关连接，报错是预期的
                        pass

        try:
            asyncio.run(scenario())
        finally:
            for srv in servers.values():
                srv.close()
            time.sleep(0.2)

        self.assertGreaterEqual(hits['target'], 1,
                                '内网目标没被直连 —— 请求跑代理去了（容器部署里就是读不到上游）')
        self.assertGreaterEqual(hits['proxy'], 1,
                                '公网目标没走代理 —— 配了 WB_HTTP_PROXY 却被绕过')
        self.assertNotIn('proxy', [k for k, v in hits.items() if v == 0],
                         f'两个靶子的命中数：{hits}')

if __name__ == '__main__':
    unittest.main()
