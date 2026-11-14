"""v1.2.81：暴露 cgroup 内存峰值 —— 判断「有没有在慢慢胀」需要两条线。

## 为什么当前值不够

`workbuddy_process_resident_bytes` 是**此刻**的常驻内存，它来回波动：一次大请求把
内存顶上去、请求结束又落回来，曲线看不出异常。

**峰值只会涨，不会落。** 两条线一起看才有意义：

| 观察 | 含义 |
| --- | --- |
| 峰值与当前值同步缓慢上涨 | 可能是泄漏 |
| 峰值远高于当前值且不再变化 | 曾经有过一个大尖峰（大 body / 长上下文） |
| 当前值回落而峰值不动 | 正常 —— 那就是峰值该有的样子 |

## 为什么读 cgroup 而不是自己记账

`memory.peak` 由内核维护，包含匿名页、页缓存和内核开销 —— 那正是 OOM killer 看的
口径。自己记账只会漏掉一部分。

需要 cgroup v2 与内核 5.19+。取不到时返回 `None`，**指标不输出** —— 不报假的 0，
因为 0 会被读成「从没超过零」。
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from server import main


class ThePeakHelperDegradesHonestlyTests(unittest.TestCase):
    def test_missing_cgroup_file_returns_none(self):
        with mock.patch('builtins.open', side_effect=OSError('no such file')):
            self.assertIsNone(main._cgroup_memory_peak_bytes())

    def test_garbage_content_returns_none(self):
        handle = mock.mock_open(read_data='not a number\n')
        with mock.patch('builtins.open', handle):
            self.assertIsNone(main._cgroup_memory_peak_bytes())

    def test_a_real_number_is_parsed(self):
        handle = mock.mock_open(read_data='198672384\n')
        with mock.patch('builtins.open', handle):
            self.assertEqual(main._cgroup_memory_peak_bytes(), 198672384)

    def test_the_scope_helper_never_raises(self):
        with mock.patch('builtins.open', side_effect=OSError('nope')):
            try:
                scope = main._cgroup_scope()
            except Exception as exc:      # noqa: BLE001
                self.fail(f'作用域读取不该抛异常：{exc!r}')
        self.assertEqual(scope, '', '取不到时返回空串，让调用方拼根路径')

    def test_the_scope_helper_reads_the_unified_hierarchy(self):
        handle = mock.mock_open(read_data='0::/system.slice/workbuddy-web.service\n')
        with mock.patch('builtins.open', handle):
            scope = main._cgroup_scope()
        self.assertEqual(scope, '/system.slice/workbuddy-web.service')


class TheGaugeIsHonestAboutAvailabilityTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_it_only_emits_when_readable(self):
        start = self.SOURCE.index('workbuddy_cgroup_memory_peak_bytes')
        window = self.SOURCE[max(0, start - 400):start + 100]
        self.assertIn('if _peak is not None', window,
                      '取不到时不能输出 —— 假 0 会被读成「从没超过零」')

    def test_the_help_text_explains_the_two_line_reading(self):
        start = self.SOURCE.index('workbuddy_cgroup_memory_peak_bytes')
        window = self.SOURCE[start:start + 500]
        self.assertIn('never decreases', window,
                      '说明要写清它为什么值得单独存在')


class TheProbeRouteIsIntactTests(unittest.TestCase):
    """v1.2.68 在这个文件上踩过装饰器挂错的坑，这里直接看注册结果。"""

    def test_metrics_still_points_at_the_handler(self):
        from fastapi.routing import APIRoute
        hits = [r for r in main.app.routes
                if isinstance(r, APIRoute) and getattr(r, 'path', '') == '/metrics']
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].endpoint.__name__, 'prometheus_metrics')

    def test_the_new_helpers_are_not_routes(self):
        from fastapi.routing import APIRoute
        endpoints = {r.endpoint.__name__ for r in main.app.routes
                     if isinstance(r, APIRoute)}
        self.assertNotIn('_cgroup_memory_peak_bytes', endpoints)
        self.assertNotIn('_cgroup_scope', endpoints)


if __name__ == '__main__':
    unittest.main()
