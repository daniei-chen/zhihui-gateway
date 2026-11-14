"""v1.2.72：观测「上游生成得比我们请求的多」。

## 怎么发现的

端到端验证时设了 `max_tokens: 80`，网关返回 `completion_tokens: 1200`。

绕过网关直连引擎，仍然是超：

| 请求 | 实际 completion_tokens |
| --- | --- |
| `max_tokens: 80` | **560** |
| `max_completion_tokens: 80` | **185**，换一次又变 **338** |

**所以是引擎不守约，不是网关算错。** 而计费按实际 token 算 —— 客户端要了 80 个
token 的回答，实际拿到 3～7 倍，账单和延迟都跟着变。

## 网关能做什么

token 在上游生成，网关替上游守不了约。但**至少要能回答「发生过多少次」**。

之前只观测了反方向：`budget.prepare` 把上限**收缩**时会通过响应头告知。
「上游超出」这个方向**完全没有信号**。

所以加一个只读计数器：不改行为、不进账本、不为它多写一行数据库。
"""
from __future__ import annotations

import pathlib
import unittest

from server import observability


class OutputOverLimitCounterTests(unittest.TestCase):
    def setUp(self):
        self.base = observability.output_over_limit_count()

    def _delta(self):
        return observability.output_over_limit_count() - self.base

    def test_it_counts_a_response_that_exceeded_the_cap(self):
        observability.note_output_over_limit(80, 560)
        self.assertEqual(self._delta(), 1)

    def test_it_ignores_a_response_within_the_cap(self):
        observability.note_output_over_limit(80, 80)
        observability.note_output_over_limit(80, 20)
        self.assertEqual(self._delta(), 0)

    def test_it_ignores_a_missing_requested_cap(self):
        """客户端没设上限时无从比较，不该计入。"""
        observability.note_output_over_limit(None, 1000)
        self.assertEqual(self._delta(), 0)

    def test_it_ignores_a_missing_actual_count(self):
        observability.note_output_over_limit(80, None)
        self.assertEqual(self._delta(), 0)

    def test_zero_cap_is_treated_as_unset(self):
        observability.note_output_over_limit(0, 500)
        self.assertEqual(self._delta(), 0)

    def test_non_integers_are_ignored_not_coerced(self):
        """`'80'` 这种不猜 —— 猜错会把噪音算成真实超限。"""
        for requested, actual in (('80', '185'), ('80', 185), (80, '185'), (True, 500)):
            observability.note_output_over_limit(requested, actual)
        self.assertEqual(self._delta(), 0)

    def test_it_never_raises(self):
        for requested, actual in ((None, None), ('x', 'y'), (1.5, 2.5), ([], {})):
            try:
                observability.note_output_over_limit(requested, actual)
            except Exception as exc:      # noqa: BLE001
                self.fail(f'热路径上的计数器不该抛异常：{exc!r}')

    def test_counting_is_additive(self):
        observability.note_output_over_limit(10, 20)
        observability.note_output_over_limit(10, 20)
        observability.note_output_over_limit(10, 20)
        self.assertEqual(self._delta(), 3)


class ItIsWiredIntoTeeSettlementTests(unittest.TestCase):
    SOURCE = (pathlib.Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')

    def test_record_calls_it(self):
        self.assertIn('note_output_over_limit', self.SOURCE,
                      '计数器没接进结算路径，等于没做')

    def test_it_passes_the_requested_cap_from_the_audit_state(self):
        start = self.SOURCE.index('note_output_over_limit')
        window = self.SOURCE[max(0, start - 300):start + 300]
        self.assertIn('max_tokens_requested', window,
                      '要拿请求时的上限来比，不能用别的值代替')

    def test_it_passes_the_measured_completion_tokens(self):
        start = self.SOURCE.index('note_output_over_limit')
        window = self.SOURCE[start:start + 300]
        self.assertIn('ct', window)


class TheMetricIsExposedTests(unittest.TestCase):
    def test_metrics_endpoint_reports_it(self):
        source = (pathlib.Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')
        self.assertIn('workbuddy_output_over_limit_total', source)
        self.assertIn('output_over_limit_count()', source)


if __name__ == '__main__':
    unittest.main()
