"""v1.2.91：请求体超时有两种，指标上分不开。

## 怎么发现的

查历史记录时看到 8 次 `body_read_timeout`，耗时分布在 60 秒到 180 秒之间：

| 耗时 | 次数 |
| --- | --- |
| 正好 180 秒 | 3 |
| 60 秒 | 1 |
| 61 / 79 / 115 / 163 秒 | 4 |

一开始以为那 5 个非 180 秒的是**误分类**。追到代码才明白：超时本来就有两种。

## 两种超时

`BodyReadTimeout` 自带 `reason`：

| reason | 含义 | 调用方该做什么 |
| --- | --- | --- |
| `idle` | 连续 60 秒**一个字节都没收到** | 查上传链路 |
| `total` | 累计上传时间超过 180 秒 | 精简上下文，或换条链路 |

非 180 秒的那几个就是 idle：传输了一段时间（60 秒里有 N 秒在传），然后链路停住，
再满 60 秒无数据 —— 总耗时自然落在 60 与 180 之间。

## 问题

两种成因的**处置完全不同**，但它们在同一个错误码下面，从指标上分不出来。

看到一条 `body_read_timeout`，不知道该让调用方去查网络，还是去砍上下文。

## 修法

按 `reason` 分成两个计数器。**未知取值不计入** —— 猜一个桶会让指标说谎，而这条指标
存在的意义正是区分两种成因。

用 `Counter` 而不是普通字典：漏初始化的键会在自增时抛，v1.2.83 就是这么崩的。
"""
from __future__ import annotations

import inspect
import unittest
from collections import Counter

from server import main, observability


class CountingTests(unittest.TestCase):
    def setUp(self):
        self.saved = Counter(observability._body_timeouts)
        observability._body_timeouts.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        observability._body_timeouts.clear()
        observability._body_timeouts.update(self.saved)

    def test_initial_shape_has_both_keys(self):
        self.assertEqual(observability.body_timeout_counts(), {'idle': 0, 'total': 0})

    def test_idle_and_total_are_counted_separately(self):
        observability.note_body_timeout('idle')
        observability.note_body_timeout('idle')
        observability.note_body_timeout('total')
        self.assertEqual(observability.body_timeout_counts(), {'idle': 2, 'total': 1})

    def test_unknown_reason_is_not_counted(self):
        """猜一个桶会让指标说谎 —— 这正是它存在的意义所在。"""
        before = observability.body_timeout_counts()
        for bogus in (None, '', 'IDLE', 'other', 0, object()):
            observability.note_body_timeout(bogus)
        self.assertEqual(observability.body_timeout_counts(), before)

    def test_it_never_raises(self):
        for bogus in (None, object(), [], {}):
            try:
                observability.note_body_timeout(bogus)
            except Exception as exc:      # noqa: BLE001
                self.fail(f'不该抛异常：{exc!r}')


class ItUsesACounterTests(unittest.TestCase):
    def test_backing_store_is_a_counter(self):
        """v1.2.83 的教训：普通字典漏了键会在自增时抛。"""
        self.assertIsInstance(observability._body_timeouts, Counter)

    def test_missing_key_increments_without_raising(self):
        box = Counter()
        box['never_initialised'] += 1
        self.assertEqual(box['never_initialised'], 1)


class ItIsWiredInTests(unittest.TestCase):
    def test_the_handler_records_the_reason(self):
        source = inspect.getsource(observability)
        start = source.index("except BodyReadTimeout as exc:")
        window = source[start:start + 600]
        self.assertIn('note_body_timeout', window)

    def test_it_passes_the_exception_reason(self):
        source = inspect.getsource(observability)
        start = source.index("except BodyReadTimeout as exc:")
        window = source[start:start + 600]
        self.assertIn("'reason'", window, '要取异常自带的分类，别自己推断')


class TheMetricsExposeThemTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_both_metrics_exist(self):
        for name in ('workbuddy_body_timeout_idle_total',
                     'workbuddy_body_timeout_total_total'):
            self.assertIn(name, self.SOURCE)

    def test_idle_help_points_at_the_link(self):
        start = self.SOURCE.index('workbuddy_body_timeout_idle_total')
        window = self.SOURCE[start:start + 400]
        self.assertIn('connectivity', window,
                      '空闲超时的处置是查链路，不是砍请求')

    def test_total_help_points_at_the_payload(self):
        start = self.SOURCE.index('workbuddy_body_timeout_total_total')
        window = self.SOURCE[start:start + 400]
        self.assertIn('shrinking', window,
                      '累计超时的处置是精简请求，两者不能混')

    def test_route_is_intact(self):
        from fastapi.routing import APIRoute
        hits = [r for r in main.app.routes
                if isinstance(r, APIRoute) and getattr(r, 'path', '') == '/metrics']
        self.assertEqual(hits[0].endpoint.__name__, 'prometheus_metrics')


class TheReasonIsRecordedTests(unittest.TestCase):
    def test_module_explains_the_two_kinds(self):
        source = inspect.getsource(observability)
        start = source.index('_body_timeouts = Counter()')
        window = source[max(0, start - 1600):start]
        self.assertIn('idle', window)
        self.assertIn('total', window)

    def test_module_records_the_measured_spread(self):
        source = inspect.getsource(observability)
        start = source.index('_body_timeouts = Counter()')
        window = source[max(0, start - 1600):start]
        self.assertIn('8', window, '要写出实测的样本数，否则看不出问题规模')


if __name__ == '__main__':
    unittest.main()
