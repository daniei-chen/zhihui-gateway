"""v1.2.32：结算写库不再占用事件循环。

一次结算是一个事务 + 5～7 条语句 + 至少一次 fsync。它在事件循环上跑的时候，
所有在途 SSE 流会一起卡住。这两个用例锁住"确实换了线程"和"异常不被吞掉"
——只测"能跑通"会漏掉"其实还在主线程上"。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from server.routers import gateway


class SettlementOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_settlement_body_runs_on_a_worker_thread(self):
        main = threading.current_thread().name
        seen = []

        def fake_record(*args, **kwargs):
            seen.append(threading.current_thread().name)

        with mock.patch.object(gateway, '_record', fake_record):
            await gateway._record_async(object(), 'ip', 'model', '', 200, 1, 1, 5, None, None, False)

        self.assertEqual(len(seen), 1, '结算必须被执行一次')
        self.assertNotEqual(seen[0], main, '结算不能在事件循环所在线程上执行')

    async def test_settlement_errors_still_propagate(self):
        """`_record` 内部已经落 spool 并吞掉写库异常；它真抛出来时不能在这里被吃掉。"""
        def boom(*args, **kwargs):
            raise RuntimeError('settlement exploded')

        with mock.patch.object(gateway, '_record', boom):
            with self.assertRaises(RuntimeError):
                await gateway._record_async()

    async def test_arguments_are_forwarded_verbatim(self):
        captured = {}

        def fake_record(*args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs

        with mock.patch.object(gateway, '_record', fake_record):
            await gateway._record_async('k', 'ip', 'm', 'mm', 201, 2, 3, 9, 'ua', 'err', True,
                                        credit=1.5, session_key='s')

        self.assertEqual(captured['args'],
                         ('k', 'ip', 'm', 'mm', 201, 2, 3, 9, 'ua', 'err', True))
        self.assertEqual(captured['kwargs'], {'credit': 1.5, 'session_key': 's'})


class StreamingSettlementStaysSynchronousTests(unittest.TestCase):
    """流式响应的 finally 不走线程：那里 await 会在取消/关闭时丢掉结算。"""

    def test_stream_generator_finally_keeps_the_synchronous_call(self):
        source = (__import__('pathlib').Path(__file__).parents[2] / 'server' / 'routers' /
                  'gateway.py').read_text(encoding='utf-8')
        # 生成器里那处必须是同步调用
        self.assertIn("_record(key,ip,requested_model or '',mapped or '',status_code,", source)
        self.assertNotIn("await _record_async(key,ip,requested_model or '',mapped or '',status_code,",
                         source)


if __name__ == '__main__':
    unittest.main()
