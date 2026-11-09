"""K12 发布排空与在途计数测试。

锁定：
1. 在途计数按类别分开、请求结束成对归零（异常也归零）；
2. 进入 draining 后 readiness 返回 ok=False（/api/readyz 503），healthz 不受影响；
3. wait_until_drained 会等在途归零，超时则返回剩余类别而不是杀流；
4. 分类函数把推理流式与普通读/写区分开。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import drain  # noqa: E402


class DrainCounterTest(unittest.TestCase):
    def setUp(self) -> None:
        drain.reset_for_tests()

    def tearDown(self) -> None:
        drain.reset_for_tests()

    def test_track_is_paired_and_nested(self) -> None:
        self.assertEqual(drain.total_in_flight(), 0)
        with drain.track('read'):
            self.assertEqual(drain.total_in_flight(), 1)
            with drain.track('inference_stream'):
                self.assertEqual(drain.total_in_flight(), 2)
                self.assertEqual(drain.in_flight().get('inference_stream'), 1)
            self.assertEqual(drain.total_in_flight(), 1)
        self.assertEqual(drain.total_in_flight(), 0)

    def test_track_recovers_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with drain.track('write'):
                raise RuntimeError('boom')
        self.assertEqual(drain.total_in_flight(), 0)

    def test_classify(self) -> None:
        self.assertEqual(drain.classify('POST', '/api/keys', {}), 'write')
        self.assertEqual(drain.classify('GET', '/api/keys', {}), 'read')
        self.assertEqual(
            drain.classify('POST', '/v1/chat/completions', {'Accept': 'text/event-stream'}),
            'inference_stream',
        )
        self.assertEqual(drain.classify('POST', '/v1/chat/completions', {}), 'inference')

    def test_begin_is_idempotent_and_visible(self) -> None:
        self.assertFalse(drain.is_draining())
        drain.begin()
        first = drain.started_at()
        drain.begin()
        self.assertTrue(drain.is_draining())
        self.assertEqual(drain.started_at(), first)


class DrainWaitTest(unittest.TestCase):
    def setUp(self) -> None:
        drain.reset_for_tests()

    def tearDown(self) -> None:
        drain.reset_for_tests()

    def test_wait_returns_when_idle(self) -> None:
        result = asyncio.run(drain.wait_until_drained(1.0, interval=0.01))
        self.assertTrue(result['drained'])
        self.assertEqual(result['remaining'], {})

    def test_wait_times_out_with_remaining(self) -> None:
        tracker = drain.track('inference_stream')
        tracker.__enter__()

        async def run():
            return await drain.wait_until_drained(0.15, interval=0.01)

        result = asyncio.run(run())
        self.assertFalse(result['drained'])
        self.assertEqual(result['remaining'].get('inference_stream'), 1)
        tracker.__exit__(None, None, None)

    def test_wait_drains_when_request_finishes(self) -> None:
        async def run():
            tracker = drain.track('write')
            tracker.__enter__()

            async def finish():
                await asyncio.sleep(0.05)
                tracker.__exit__(None, None, None)

            task = asyncio.create_task(finish())
            result = await drain.wait_until_drained(2.0, interval=0.01)
            await task
            return result

        result = asyncio.run(run())
        self.assertTrue(result['drained'])
        self.assertGreaterEqual(result['waited_sec'], 0.0)


class ReadinessDrainingTest(unittest.TestCase):
    def setUp(self) -> None:
        drain.reset_for_tests()

    def tearDown(self) -> None:
        drain.reset_for_tests()

    def test_readiness_reports_not_ok_while_draining(self) -> None:
        from server import operational

        original = operational.database_writable
        operational.database_writable = lambda: True
        try:
            self.assertTrue(operational.readiness()['ok'])
            drain.begin()
            status = operational.readiness()
            self.assertFalse(status['ok'])
            self.assertTrue(status['draining'])
        finally:
            operational.database_writable = original


if __name__ == '__main__':
    unittest.main()
