"""`/v1/responses` 的两处「静默走错路」：预算耗尽与缓存命中数。

## 为什么单列

这两处都不是崩溃，而是**给出错误的信息**，所以既不会被异常监控发现，也不会
让客户端报错 —— 它们只是让客户端做出错误决定：

1. **预算耗尽**：chat（`gateway._chat`）与 messages（`anthropic`）都会把它翻译成
   一条明确的错误事件；responses 没有这个分支，于是落到
   `translator.finish(force=False)`，发 `response.completed`／`incomplete` +
   **空 output**。客户端把「额度/预算用完了」读成「模型没说话」，既不重试也不
   提示，而账单上已经扣过费。

2. **缓存命中数恒为 0**：上游用国内一族的字段名 `prompt_cache_hit_tokens`，
   而 responses 只读 OpenAI 的 `prompt_tokens_details.cached_tokens`。同一个上游
   在 `anthropic.py` / `gateway.py` 里读的是前者。结果客户端永远看到
   `cached_tokens: 0`，据此以为每次都是全量计价。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.routers import responses as R


class _FakeAudit:
    """只保留被判定用到的两个属性。"""

    def __init__(self, budget_exhausted: bool = False) -> None:
        self.budget_exhausted = budget_exhausted
        self.error_stage = ''
        self.error_code = ''


class BudgetExhaustedTest(unittest.TestCase):
    def test_exhausted_budget_becomes_a_visible_error(self) -> None:
        audit = _FakeAudit(budget_exhausted=True)
        text = R._apply_budget_exhausted(audit, None, SimpleNamespace(request_id='r1'))
        self.assertTrue(text, '预算耗尽必须变成客户端可见的错误，不能静默结束')
        self.assertEqual(audit.error_code, 'output_budget_exhausted')
        self.assertEqual(audit.error_stage, 'upstream')

    def test_existing_error_is_not_overwritten(self) -> None:
        """上游已经把错误告诉客户端了，不要再盖一条互相矛盾的文案。"""
        audit = _FakeAudit(budget_exhausted=True)
        text = R._apply_budget_exhausted(audit, 'upstream said no')
        self.assertEqual(text, 'upstream said no')
        self.assertEqual(audit.error_code, '')

    def test_healthy_stream_stays_clean(self) -> None:
        audit = _FakeAudit(budget_exhausted=False)
        self.assertIsNone(R._apply_budget_exhausted(audit, None))
        self.assertEqual(audit.error_code, '')

    def test_missing_flag_does_not_crash(self) -> None:
        """老的 audit 对象没有这个属性时也不能抛。"""
        self.assertIsNone(R._apply_budget_exhausted(SimpleNamespace(), None))


class CachedTokensTest(unittest.TestCase):
    def test_reads_the_field_the_upstream_actually_sends(self) -> None:
        usage = {'prompt_tokens': 100, 'completion_tokens': 10,
                 'prompt_cache_hit_tokens': 64}
        mapped = R._usage_object(usage)
        self.assertEqual(mapped['input_tokens_details']['cached_tokens'], 64)
        self.assertEqual(mapped['input_tokens'], 100,
                         '缓存命中数不能从 input 里减掉（OpenAI 语义是已包含）')

    def test_openai_shaped_field_still_wins(self) -> None:
        """上游若真给了 OpenAI 字段，以它为准（避免两套字段打架时口径漂移）。"""
        usage = {'prompt_tokens': 100, 'completion_tokens': 10,
                 'prompt_tokens_details': {'cached_tokens': 32},
                 'prompt_cache_hit_tokens': 64}
        self.assertEqual(
            R._usage_object(usage)['input_tokens_details']['cached_tokens'], 32)

    def test_no_cache_info_reports_zero(self) -> None:
        mapped = R._usage_object({'prompt_tokens': 5, 'completion_tokens': 1})
        self.assertEqual(mapped['input_tokens_details']['cached_tokens'], 0)


if __name__ == '__main__':
    unittest.main()
