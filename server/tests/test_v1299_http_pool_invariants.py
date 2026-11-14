"""v1.2.99：连接池是 290 行并发代码，此前一行测试都没有。

## 为什么要补

静态检查发现 `server/http_pool.py` **从未被任何测试 import** ——
它的名字只出现在注释和 `HTTP_POOL_*` 常量名里。

而它是**所有上游请求共用**的连接池：引用计数、retire、容量上限、跨事件循环清理
全在这里。**这类代码出错的表现不是抛异常，而是连接泄漏、池子慢慢失效、
或者某个 loop 关闭后留下一堆没关的 transport。**

代码本身写得很细（超时保护、取消处理、双重 close 防护都在），但**没有测试守住
这些不变量** —— 以后一次重构就可能悄悄破坏它们。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 引用计数配对 | 池子容量被虚占，请求开始排队 |
| 重复 release 必须报错 | 计数下溢，容量失控 |
| 容量不超过上限 | 上游连接数暴涨 |
| retire 后不复用 | 向已下线的主机继续发请求 |
| close_loop 清理干净 | loop 关闭后 transport 悬挂 |
| 工厂失败不留残骸 | 失败的重试占掉名额 |
"""
from __future__ import annotations

import asyncio
import unittest

import httpx

from server import http_pool


class _Transport(httpx.AsyncBaseTransport):
    """记录自己被关了几次的最小 transport。"""

    def __init__(self, *, fail_on_close=False):
        self.closed = 0
        self.fail_on_close = fail_on_close

    async def aclose(self):
        self.closed += 1
        if self.fail_on_close:
            raise RuntimeError('close failed')

    async def handle_async_request(self, request):        # pragma: no cover
        raise NotImplementedError


def _factory(bucket):
    def make():
        transport = _Transport()
        bucket.append(transport)
        return transport
    return make


class ReferenceCountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = http_pool.TransportPool(cache={})
        self.made = []

    async def asyncTearDown(self):
        await self.pool.close_loop(force=True)

    async def test_acquire_returns_an_entry_and_counts_references(self):
        entry = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        self.assertIsInstance(entry, http_pool.Entry)
        self.assertEqual(entry.references, 1)

    async def test_second_acquire_of_the_same_key_reuses_the_transport(self):
        first = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        second = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        self.assertIs(first, second, '同一键必须复用同一个 transport')
        self.assertEqual(second.references, 2)
        self.assertEqual(len(self.made), 1, '只该建一个 transport')

    async def test_release_returns_the_slot(self):
        entry = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        self.pool.release(entry)
        self.assertEqual(entry.references, 0)

    async def test_double_release_is_rejected(self):
        """计数下溢会让容量失控，必须当场报错而不是静默变成负数。"""
        entry = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        self.pool.release(entry)
        with self.assertRaises(RuntimeError):
            self.pool.release(entry)


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = http_pool.TransportPool(cache={})
        self.made = []

    async def asyncTearDown(self):
        await self.pool.close_loop(force=True)

    async def test_owned_never_exceeds_the_limit(self):
        entries = []
        for index in range(3):
            entries.append(await self.pool.acquire(
                f'k{index}', _factory(self.made), lambda: 3, 5, lambda: False))
        self.assertEqual(len(self.pool.owned), 3)
        # 所有都在用：下一次必须等，而不是继续建。
        with self.assertRaises(httpx.PoolTimeout):
            await self.pool.acquire('k9', _factory(self.made), lambda: 3, 0.15, lambda: False)
        self.assertLessEqual(len(self.pool.owned), 3)

    async def test_limit_of_zero_still_allows_one(self):
        """`max(1, limit())` —— 上限配成 0 不能让池子完全不可用。"""
        entry = await self.pool.acquire('k', _factory(self.made), lambda: 0, 5, lambda: False)
        self.assertIsNotNone(entry)
        self.assertEqual(len(self.pool.owned), 1)

    async def test_cancelled_request_is_rejected_before_allocating(self):
        with self.assertRaises(RuntimeError):
            await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: True)
        self.assertEqual(self.made, [], '取消的请求不该建出 transport')


class RetireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = http_pool.TransportPool(cache={})
        self.made = []

    async def asyncTearDown(self):
        await self.pool.close_loop(force=True)

    async def test_retired_entry_is_not_reused(self):
        first = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        self.pool.retire(first)
        self.pool.release(first)
        second = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        self.assertIsNot(second, first, 'retire 之后必须换新的 transport')
        # 新 entry 用的是**同一个 key**（loop + 逻辑键），所以这里不能断言 key 不存在，
        # 只能断言该 key 现在指向的不是那个已退休的 entry。
        self.assertIsNot(self.pool.cache.get(first.key), first,
                         'cache 里不能再指向已退休的 entry')
        self.assertIs(self.pool.cache.get(second.key), second)

    async def test_retire_while_in_use_closes_only_after_release(self):
        """还在用的连接不能立刻关掉 —— 那会把进行中的请求掐断。"""
        entry = await self.pool.acquire('k', _factory(self.made), lambda: 4, 5, lambda: False)
        self.pool.retire(entry)
        self.assertEqual(entry.retired, True)
        self.assertEqual(self.made[0].closed, 0, '还有引用时不该关')
        self.pool.release(entry)
        await asyncio.sleep(0.05)
        self.assertEqual(self.made[0].closed, 1, '最后一个引用释放后才关')


class LoopCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = http_pool.TransportPool(cache={})
        self.made = []

    async def test_close_loop_closes_every_transport_of_that_loop(self):
        for index in range(3):
            await self.pool.acquire(f'k{index}', _factory(self.made), lambda: 8, 5, lambda: False)
        self.assertEqual(len(self.made), 3)
        await self.pool.close_loop(force=True)
        await asyncio.sleep(0.05)
        self.assertEqual([t.closed for t in self.made], [1, 1, 1])
        self.assertEqual(self.pool.owned, set(), '关完之后不该还留着 owned 记录')


class FactoryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = http_pool.TransportPool(cache={})

    async def asyncTearDown(self):
        await self.pool.close_loop(force=True)

    async def test_factory_failure_leaves_no_residue(self):
        """建的失败不能占住名额，否则重试几次池子就废了。"""
        def boom():
            raise RuntimeError('cannot build')

        with self.assertRaises(RuntimeError):
            await self.pool.acquire('k', boom, lambda: 4, 5, lambda: False)
        self.assertEqual(self.pool.owned, set())
        self.assertEqual(self.pool.cache, {})


if __name__ == '__main__':
    unittest.main()
