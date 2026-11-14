"""v1.2.38：转发层不再有永远不超时的读。

`read=None` 在 httpx 里是"相邻两次读之间永不超时"，不是"总时长不设"。上游半开
连接、被挂起的上游进程，都会让一条连接永久占住容量名额，而背后没有任何计时器
回收它 —— 128 个这样的流就能把转发层打满。

改成 600 秒：正常 SSE 每隔几秒就有数据或心跳，十分钟没有字节的上游已经没救了；
它比主服务的上游读超时（300 秒）更宽松，所以不会替后端先动手杀一条还活着的流。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SOURCE = (Path(__file__).parents[2] / 'server' / 'rollingrelay.py').read_text(encoding='utf-8')


class RelayReadTimeoutTests(unittest.TestCase):
    def test_read_timeout_is_bounded(self):
        self.assertNotIn('read=None', SOURCE, '转发层不允许永不超时的读')

    def test_read_timeout_value_is_generous_but_finite(self):
        marker = 'httpx.Timeout(connect=3, read='
        index = SOURCE.index(marker)
        tail = SOURCE[index + len(marker):]
        value = int(tail.split(',')[0])
        # 至少要能覆盖主服务的上游读超时（300 秒），否则会替后端先杀活流
        self.assertGreaterEqual(value, 300)
        self.assertLessEqual(value, 1800)

    def test_other_timeouts_are_unchanged(self):
        self.assertIn('connect=3', SOURCE)
        self.assertIn('write=30', SOURCE)
        self.assertIn('pool=5', SOURCE)

    def test_module_still_parses(self):
        ast.parse(SOURCE, filename='rollingrelay.py')


if __name__ == '__main__':
    unittest.main()
