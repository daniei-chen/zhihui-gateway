"""v1.2.68：把进程自身的占用暴露成指标。

## 为什么

网关是常驻进程。跑几天会不会慢慢胀、有没有泄漏 —— `/metrics` 此前只有「在不在
服务」和「跑了多久」两个进程级指标。也就是说：**只有崩了才发现，中间那段缓慢上升
完全不可见**。

对「长期稳定」这个目标来说，能看见趋势比事后告警更有用：先有曲线，才谈得上判断
哪一段斜率不正常。

## 为什么读 /proc 而不是引依赖

只需要两个数字。`/proc/self/statm` 是每页计数，乘页面大小即可。不为两行数据增加
一个第三方包。

取不到时（非 Linux、/proc 不可读）返回 None，指标**干脆不输出** —— 报一个假的 0
比没有更糟，因为看的人会以为真的没占用。
"""
from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from server import main

STATM = '10178 1204 305 1 0 500 0\n'      # 虚拟页 / 常驻页 / 共享页 / ...
PAGE = 4096


def _reader(text):
    class _Handle:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return text

    return lambda *a, **k: _Handle()


class ProcessMemoryTests(unittest.TestCase):
    def test_it_converts_pages_to_bytes(self):
        with mock.patch('builtins.open', _reader(STATM)), \
             mock.patch.object(main.os, 'sysconf', return_value=PAGE, create=True):
            result = main._process_memory_bytes()
        self.assertIsNotNone(result)
        resident, virtual = result
        self.assertEqual(resident, 1204 * PAGE)
        self.assertEqual(virtual, 10178 * PAGE)

    def test_missing_proc_yields_none_not_zero(self):
        """非 Linux 上不该编一个 0 出来 —— 那会被读成「没有占用」。"""
        with mock.patch('builtins.open', side_effect=OSError('no /proc')):
            self.assertIsNone(main._process_memory_bytes())

    def test_garbage_content_yields_none(self):
        for text in ('', 'not numbers', '1 2', 'a b c d e f g'):
            with mock.patch('builtins.open', _reader(text)), \
                 mock.patch.object(main.os, 'sysconf', return_value=PAGE, create=True):
                try:
                    result = main._process_memory_bytes()
                except Exception as exc:      # noqa: BLE001
                    self.fail(f'不该抛异常，应当返回 None：{exc!r}')
                if text.strip() == '1 2':
                    # 两列齐全但缺其它列，仍能解析出两个数字 —— 允许
                    continue
                self.assertIsNone(result, f'输入 {text!r} 时不该给出数字')

    def test_sysconf_failure_yields_none(self):
        with mock.patch('builtins.open', _reader(STATM)), \
             mock.patch.object(main.os, 'sysconf', side_effect=ValueError('unsupported'), create=True):
            self.assertIsNone(main._process_memory_bytes())


class MetricsExposeThemTests(unittest.TestCase):
    def setUp(self):
        self.source = (pathlib.Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')
        start = self.source.index('def prometheus_metrics')
        self.block = self.source[start:self.source.index('\n    return PlainTextResponse', start)]

    def test_resident_and_virtual_are_emitted(self):
        self.assertIn('workbuddy_process_resident_bytes', self.block)
        self.assertIn('workbuddy_process_virtual_bytes', self.block)

    def test_thread_count_is_emitted(self):
        self.assertIn('workbuddy_process_threads', self.block)

    def test_memory_metrics_are_conditional(self):
        """取不到时不要输出 0 —— 那会被读成「没有占用」。"""
        self.assertIn('if _memory is not None:', self.block)

    def test_thread_metric_is_unconditional(self):
        """线程数在任何平台都取得到，不该跟着 /proc 一起消失。

        判断方式是缩进：`_m('workbuddy_process_threads'` 那行必须和
        `_m('workbuddy_up'` 同级（都在函数体顶层 4 个空格），
        而不是缩进在 `if _memory is not None:` 里面。
        """
        def indent_of(name):
            for line in self.block.splitlines():
                if name in line:
                    return len(line) - len(line.lstrip())
            self.fail(f'找不到指标 {name}')

        self.assertEqual(indent_of("_m('workbuddy_process_threads'"),
                         indent_of("_m('workbuddy_up'"),
                         '线程指标被缩进到条件块里了')
        self.assertGreater(indent_of("_m('workbuddy_process_resident_bytes'"),
                           indent_of("_m('workbuddy_up'"),
                           '内存指标应当缩进在条件块内')


class ExistingMetricsAreUntouchedTests(unittest.TestCase):
    def test_up_and_uptime_still_emitted(self):
        source = (pathlib.Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')
        start = source.index('def prometheus_metrics')
        block = source[start:source.index('\n    return PlainTextResponse', start)]
        self.assertIn('workbuddy_up', block)
        self.assertIn('workbuddy_uptime_seconds', block)


class TheMetricsRouteStillPointsAtTheHandlerTests(unittest.TestCase):
    """2026-11-15 踩过的坑：插新函数时把装饰器带走了。

    我原本只想在 `prometheus_metrics` 前面加一个辅助函数，锚点选了
    `def prometheus_metrics(request: Request):` —— 但**装饰器在那一行之上**，
    于是 `@app.get('/metrics')` 落到了新函数头上。

    后果很隐蔽：路由还在、进程不报错、`/metrics` 也返回 200，只是处理函数变成了
    那个返回 None 的辅助函数，响应体是 `null`。八条鉴权检查因此全红，而它们看起来
    是在测鉴权，完全不像被指标代码影响。

    所以这里直接断言路由指向谁 —— 不看代码文本，看注册结果。
    """

    def test_route_endpoint_is_the_prometheus_handler(self):
        from fastapi.routing import APIRoute
        from server import main
        hits = [route for route in main.app.routes
                if isinstance(route, APIRoute) and getattr(route, 'path', '') == '/metrics']
        self.assertEqual(len(hits), 1, f'/metrics 注册了 {len(hits)} 条路由')
        self.assertEqual(hits[0].endpoint.__name__, 'prometheus_metrics',
                         '装饰器挂错函数了 —— 路由会返回 200 但内容不对')

    def test_helper_is_not_a_route(self):
        from fastapi.routing import APIRoute
        from server import main
        names = {route.endpoint.__name__ for route in main.app.routes
                 if isinstance(route, APIRoute)}
        self.assertNotIn('_process_memory_bytes', names,
                         '辅助函数被注册成路由了')


if __name__ == '__main__':
    unittest.main()
