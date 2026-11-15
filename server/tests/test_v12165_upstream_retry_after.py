"""v1.2.165：把上游的 `Retry-After` 转发给客户端。

## 缺口从哪来

2026-11-15 顺着「限流没告诉客户端该等多久」查下去，发现同一条链上还有一环断着。

上游确实会给：它把排队/限流对应的时长映射成 `Retry-After` 响应头下发。
但网关处理上游错误的那个分支里，**body 原样透传、响应头没有人接**。

结果：客户端只知道「出错了」，不知道「等多久」，退避库按默认值猜（常见 1 秒），
而真实等待可能是几十秒。**它在窗口内反复撞，越撞越远。**

## 两个刻意的取舍

**只认整数秒，不解析 HTTP-date。** `Retry-After` 规范允许日期形式，但上游从不用它；
为那种形式写一套日期解析，只会多出一条没人验证的路径。

**超过两小时按脏数据丢弃**（与上游侧同一口径）。**拿不到就返回 None，不写 0** ——
一个假的等待时间比没有更糟，客户端会照着它去撞。

## 本测试守什么

**真的调用那个函数**，用最小的 stub 响应对象覆盖六种输入。这类「读一个头再判断」
的代码最容易在边界上出错，而源码看起来永远是对的。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

GATEWAY_PATH = Path(__file__).resolve().parents[1] / 'routers' / 'gateway.py'
SOURCE = GATEWAY_PATH.read_text(encoding='utf-8')


def _load_helper():
    """单独取出 `_upstream_retry_after` 执行，避免 import 整个路由模块。"""
    tree = ast.parse(SOURCE)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == '_upstream_retry_after':
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {}
            exec(compile(module, '<helper>', 'exec'), namespace)   # noqa: S102
            return namespace['_upstream_retry_after']
    raise AssertionError('没找到 _upstream_retry_after')


class _Resp:
    """最小 stub：只提供 headers.get。"""

    def __init__(self, value=None, *, raises=False):
        self._value = value
        self._raises = raises

    @property
    def headers(self):
        outer = self

        class _H:
            def get(self, name, default=None):
                if outer._raises:
                    raise RuntimeError('header read failed')
                return outer._value
        return _H()


class ItReadsTheHeaderTests(unittest.TestCase):
    def test_plain_seconds(self):
        self.assertEqual(_load_helper()(_Resp('42')), 42)

    def test_surrounding_whitespace(self):
        self.assertEqual(_load_helper()(_Resp('  30 ')), 30)

    def test_zero_is_allowed(self):
        """0 是合法的「立刻重试」，不该被当成缺失。"""
        self.assertEqual(_load_helper()(_Resp('0')), 0)


class ItRefusesWhatItCannotTrustTests(unittest.TestCase):
    def test_missing_header(self):
        self.assertIsNone(_load_helper()(_Resp(None)))

    def test_empty_string(self):
        self.assertIsNone(_load_helper()(_Resp('')))

    def test_non_numeric(self):
        self.assertIsNone(_load_helper()(_Resp('soon')))

    def test_http_date_form_is_not_parsed(self):
        """规范允许日期形式，但上游从不用；解析它等于多一条没人验证的路径。"""
        self.assertIsNone(_load_helper()(_Resp('Wed, 21 Oct 2026 07:28:00 GMT')))

    def test_negative(self):
        self.assertIsNone(_load_helper()(_Resp('-5')))

    def test_over_two_hours_is_discarded(self):
        self.assertIsNone(_load_helper()(_Resp('7201')))

    def test_exactly_two_hours_is_kept(self):
        """边界值本身是合法的，不能被上限误杀。"""
        self.assertEqual(_load_helper()(_Resp('7200')), 7200)

    def test_header_read_failure_is_swallowed(self):
        """读头失败不能让整个请求崩 —— 这条路径在错误分支上，再崩就没救了。"""
        self.assertIsNone(_load_helper()(_Resp(raises=True)))


class TheErrorBranchForwardsItTests(unittest.TestCase):
    def test_helper_is_called_on_the_error_path(self):
        self.assertIn('_upstream_retry_after(resp)', SOURCE)

    def test_header_is_written(self):
        self.assertIn("out.headers['Retry-After']", SOURCE)

    def test_body_field_is_written(self):
        self.assertIn("'retry_after_seconds'", SOURCE)

    def test_it_does_not_overwrite_the_upstream_field(self):
        """上游若已给出自己的字段值，保留它的，别覆盖成头部那个。"""
        self.assertIn("'retry_after_seconds' not in err", SOURCE)


class ItSaysWhyTests(unittest.TestCase):
    def test_it_records_the_shape(self):
        idx = SOURCE.find('def _upstream_retry_after')
        block = SOURCE[idx:idx + 2200]
        self.assertIn('丢掉', block)

    def test_it_says_why_no_date_form(self):
        idx = SOURCE.find('def _upstream_retry_after')
        block = SOURCE[idx:idx + 2200]
        self.assertIn('不解析', block)

    def test_it_says_missing_is_not_zero(self):
        """拿不到就不写字段 —— 一个假的等待时间比没有更糟。"""
        idx = SOURCE.find('def _upstream_retry_after')
        block = SOURCE[idx:idx + 2200]
        self.assertIn('而不是写 0', block)


class TheForwardingIsObservableTests(unittest.TestCase):
    """转发必须留下痕迹 —— 否则事后无法回答「它到底有没有生效」。

    2026-11-15 实现它时想端到端验证，发现做不到：上游的 429/503 是外部条件，
    没法主动构造；而请求记录只存状态码与错误文本，不存响应头。
    于是当时只能靠单测说「函数返回了正确的数」。

    加一行日志不是装饰 —— 它把这个能力从「只能推理」变成「可以查证」。
    """

    def test_it_logs_when_forwarding(self):
        idx = SOURCE.find("out.headers['Retry-After']")
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 900]
        self.assertIn('logger.info', window, '转发了却没有留下任何记录')

    def test_the_log_carries_both_numbers(self):
        """要能看懂是哪个状态码、等了多久，否则记了也没用。"""
        idx = SOURCE.find('logger.info(')
        self.assertGreater(idx, 0)
        window = SOURCE[idx:idx + 300]
        self.assertIn('resp.status_code', window)
        self.assertIn('wait', window)

    def test_it_does_not_log_on_the_success_path(self):
        """只有真的转发了才记 —— 正常请求不该被这条日志刷屏。"""
        idx = SOURCE.find("out.headers['Retry-After']")
        before = SOURCE[max(0, idx - 120):idx]
        self.assertIn('if wait is not None', before)


if __name__ == '__main__':
    unittest.main()
