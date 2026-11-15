"""v1.2.44：阻塞工作分池，实例心跳不再和业务请求排同一个队列。

默认执行器在 2 vCPU 上是 `min(32, cpu+4) = 6` 个线程，而推理主路径一个请求要占
6 次线程跳转。心跳排在同一个队列里：队列积压十几秒，心跳就超时，租约失守后进程
进入「停止准入」且**不会自愈**，全站 503 只能重启。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from server import threadpools

SOURCE_MAIN = (Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')
SOURCE_RUNTIME = (Path(__file__).parents[1] / 'instance_runtime.py').read_text(encoding='utf-8')


class PoolShapeTests(unittest.TestCase):
    def setUp(self):
        threadpools.reset()
        self.addCleanup(threadpools.reset)

    def test_heartbeat_pool_is_a_single_thread(self):
        """心跳只需要一条毫秒级 SQL；给它一个线程就够，关键是别和别人共用。"""
        self.assertEqual(threadpools.heartbeat_pool()._max_workers, 1)

    def test_blocking_pool_is_wider_than_the_default_executor(self):
        # 2 vCPU 上默认执行器是 6；新的池必须明显更宽，否则改它没有意义。
        self.assertGreaterEqual(threadpools.blocking_pool()._max_workers, 8)

    def test_pools_are_singletons(self):
        self.assertIs(threadpools.blocking_pool(), threadpools.blocking_pool())
        self.assertIs(threadpools.heartbeat_pool(), threadpools.heartbeat_pool())

    def test_heartbeat_and_blocking_are_different_objects(self):
        self.assertIsNot(threadpools.heartbeat_pool(), threadpools.blocking_pool())

    def test_worker_count_is_clamped(self):
        import os
        from unittest import mock
        threadpools.reset()
        with mock.patch.dict(os.environ, {'WB_BLOCKING_THREADS': '1'}):
            self.assertGreaterEqual(threadpools.blocking_pool()._max_workers, 4)
        threadpools.reset()
        with mock.patch.dict(os.environ, {'WB_BLOCKING_THREADS': '9999'}):
            self.assertLessEqual(threadpools.blocking_pool()._max_workers, 64)


class WiringTests(unittest.TestCase):
    def test_startup_installs_the_wider_executor_before_any_offload(self):
        marker = 'set_default_executor(threadpools.blocking_pool())'
        self.assertIn(marker, SOURCE_MAIN)
        # 必须在 lifespan 的开头附近，不能等到某个请求之后才换
        head = SOURCE_MAIN[SOURCE_MAIN.index('async def lifespan'):][:1200]
        self.assertIn(marker, head)

    def test_instance_heartbeat_uses_the_dedicated_pool(self):
        self.assertIn('threadpools.heartbeat_pool()', SOURCE_RUNTIME)
        self.assertNotIn('joined_thread(instancelease.heartbeat', SOURCE_RUNTIME,
                         '心跳不能再走默认执行器')

    def test_instance_heartbeat_keeps_cancellation_semantics(self):
        """改成专用池后仍必须"取消也要等它做完"，否则 close() 会释放半路续期的租约。"""
        self.assertIn('joined_task(asyncio.ensure_future(', SOURCE_RUNTIME)

    def test_budget_heartbeat_uses_the_dedicated_pool(self):
        head = SOURCE_MAIN[SOURCE_MAIN.index('async def budget_heartbeat'):][:600]
        self.assertIn('threadpools.heartbeat_pool()', head)
        self.assertNotIn('joined_thread(budget.heartbeat)', head)


if __name__ == '__main__':
    unittest.main()
