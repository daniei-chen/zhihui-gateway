"""Verify the HTTP factory actually used by gateway/Anthropic/Responses."""
import asyncio
import threading
import unittest
from unittest import mock

import httpx

from server import config


class ActualHttpPoolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await config.close_clients()
        self.connections = 0
        self.requests = []
        self.writers = set()
        self.handlers = set()
        self.finish_body = asyncio.Event()
        self.clients = []
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = 'http://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        self.connections += 1
        try:
            while True:
                header = await reader.readuntil(b'\r\n\r\n')
                self.requests.append(header)
                if b'/hold ' in header:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nabc')
                    await writer.drain()
                    await self.finish_body.wait()
                    writer.write(b'def')
                elif b'/setcookie ' in header:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
                                 b'Set-Cookie: qa-session=synthetic; Path=/\r\n\r\nok')
                else:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.writers.discard(writer)
            self.handlers.discard(task)

    async def asyncTearDown(self):
        self.finish_body.set()
        for client in self.clients:
            await client.aclose()
        await config.close_clients()
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*list(self.handlers), return_exceptions=True), 5)

    def client(self, identity='same', timeout=5, lane=''):
        result = config.http_client(timeout, proxy='', identity=identity, lane=lane)
        self.clients.append(result)
        return result

    async def held(self, identity='first'):
        client = self.client(identity)
        response = await client.send(client.build_request('GET', self.url + '/hold'), stream=True)
        iterator = response.aiter_bytes()
        self.assertEqual(await anext(iterator), b'abc')
        return response, iterator

    async def finish(self, response, iterator):
        self.finish_body.set()
        remaining = b''.join([chunk async for chunk in iterator])
        await response.aclose()
        self.assertEqual(remaining, b'def', 'Cache pressure must not truncate an admitted response')

    async def test_distinct_leases_isolate_cookies_and_default_headers(self):
        first, second = self.client(), self.client()
        first.cookies.set('qa-session', 'synthetic')
        first.headers['X-QA-Private'] = 'synthetic'
        self.assertNotIn('qa-session', second.cookies)
        self.assertNotIn('X-QA-Private', second.headers)
        await second.get(self.url + '/ok')
        self.assertNotIn(b'qa-session', self.requests[-1])
        self.assertNotIn(b'X-QA-Private', self.requests[-1])

    async def test_serial_leases_reuse_one_real_tcp_connection(self):
        for _ in range(30):
            async with self.client() as client:
                self.assertEqual((await client.get(self.url + '/ok')).content, b'ok')
        self.assertEqual(len(self.requests), 30)
        self.assertEqual(self.connections, 1)

    async def test_upstream_set_cookie_stays_in_its_own_lease(self):
        first, second = self.client(), self.client()
        await first.get(self.url + '/setcookie')
        await first.get(self.url + '/ok')
        self.assertIn(b'qa-session=synthetic', self.requests[-1])
        await second.get(self.url + '/ok')
        self.assertNotIn(b'qa-session=synthetic', self.requests[-1])
        self.assertEqual(self.connections, 1)

    async def test_response_body_survives_early_client_release(self):
        response, iterator = await self.held()
        try:
            await self.clients[-1].aclose()
            await self.finish(response, iterator)
        finally:
            await response.aclose()

    async def test_global_capacity_and_cleanup_across_live_event_loops(self):
        ready = threading.Event()
        async def in_other_loop():
            async with config.http_client(5, proxy='', identity='thread-owner') as client:
                async with client.stream('GET', self.url + '/hold') as response:
                    iterator = response.aiter_bytes()
                    self.assertEqual(await anext(iterator), b'abc')
                    ready.set()
                    return b''.join([chunk async for chunk in iterator])
        with mock.patch.object(config, '_CLIENTS_MAX', 1):
            task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(in_other_loop())))
            try:
                self.assertTrue(await asyncio.to_thread(ready.wait, 5))
                with self.assertRaises(httpx.PoolTimeout):
                    await self.client('main-owner', httpx.Timeout(5, pool=.05)).get(self.url + '/ok')
                self.finish_body.set()
                self.assertEqual(await asyncio.wait_for(task, 10), b'def')
                self.assertEqual((await self.client('after-thread').get(self.url + '/ok')).content, b'ok')
            finally:
                self.finish_body.set()
                await asyncio.wait_for(asyncio.shield(task), 10)

    async def test_legacy_helper_shares_the_actual_business_pool(self):
        from server import pooledhttp
        async with pooledhttp.client(5, '', 'compat-owner') as client:
            await client.get(self.url + '/ok')
        await self.client('compat-owner').get(self.url + '/ok')
        self.assertEqual(self.connections, 1)

    async def test_failed_transport_construction_returns_its_capacity(self):
        with mock.patch.object(config, '_CLIENTS_MAX', 1), mock.patch.object(
                config, '_build_transport', side_effect=ValueError('synthetic bad configuration')):
            with self.assertRaises(ValueError):
                await self.client('bad').get(self.url + '/ok')
        with mock.patch.object(config, '_CLIENTS_MAX', 1):
            self.assertEqual((await self.client('recovered').get(self.url + '/ok')).content, b'ok')

    async def test_cleanup_failure_cannot_open_unbounded_replacement_pools(self):
        transport = config._build_transport('')
        original_close = transport.aclose
        allow_close = False
        async def failing_close():
            if not allow_close:
                raise OSError('synthetic cleanup failure')
            await original_close()
        transport.aclose = failing_close
        try:
            with mock.patch.object(config, '_CLIENTS_MAX', 1), mock.patch.object(
                    config, '_build_transport', return_value=transport) as build:
                await self.client('first').get(self.url + '/ok')
                with self.assertRaises(OSError):
                    await self.client('second').get(self.url + '/ok')
                with self.assertRaises(httpx.PoolTimeout):
                    await self.client('third', httpx.Timeout(5, pool=.02)).get(self.url + '/ok')
                self.assertEqual(build.call_count, 1, 'Failed cleanup must retain its capacity slot')
                allow_close = True
                await config.close_clients()
        finally:
            allow_close = True
            await config.close_clients()
        with mock.patch.object(config, '_CLIENTS_MAX', 1):
            self.assertEqual((await self.client('recovered').get(self.url + '/ok')).content, b'ok')

    async def test_idle_eviction_does_not_close_a_different_active_stream(self):
        with mock.patch.object(config, '_CLIENTS_MAX', 2):
            response, iterator = await self.held()
            try:
                await self.client('idle').get(self.url + '/ok')
                await self.client('third').get(self.url + '/ok')
                await self.finish(response, iterator)
            finally:
                await response.aclose()

    async def test_full_active_pool_times_out_new_identity_without_cutting_old_stream(self):
        with mock.patch.object(config, '_CLIENTS_MAX', 1):
            response, iterator = await self.held()
            try:
                other = self.client('second', httpx.Timeout(5, pool=.05))
                with self.assertRaises(httpx.PoolTimeout):
                    await other.get(self.url + '/ok')
                self.assertEqual(len(self.requests), 1, 'No second upstream request may escape the cap')
                await self.finish(response, iterator)
            finally:
                await response.aclose()

    async def test_cancelled_waiter_does_not_leak_capacity(self):
        with mock.patch.object(config, '_CLIENTS_MAX', 1):
            response, iterator = await self.held()
            task = asyncio.create_task(self.client('second').get(self.url + '/ok'))
            try:
                await asyncio.sleep(.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.finish(response, iterator)
                result = await self.client('third').get(self.url + '/ok')
                self.assertEqual(result.content, b'ok')
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await response.aclose()

    async def test_graceful_pool_cleanup_waits_for_response_body(self):
        response, iterator = await self.held()
        try:
            await config.close_clients()
            await self.finish(response, iterator)
        finally:
            await response.aclose()

    async def test_identity_is_not_overridden_by_lane(self):
        for identity in ('tenant-one', 'tenant-two'):
            await self.client(identity, lane='generation').get(self.url + '/ok')
        self.assertEqual(self.connections, 2, 'A lane cannot merge two upstream identities')


if __name__ == '__main__':
    unittest.main()
