"""v1.2.75：把上游引擎的能力边界测清楚并成文。

## 测了什么

直连 `trae` 引擎（绕过网关），模型 `Doubao-Seed-Code`：

| 参数 | 结果 |
| --- | --- |
| `stream` + `usage` | **正常**（流末尾带 usage 与 `[DONE]`） |
| `max_tokens` | 请求 80，实际 **560** |
| `max_completion_tokens` | 请求 80，两次 **185 / 338** |
| `stop` | 要求「遇 5 停」，输出含 5 且数到 20 |
| `temperature` | 设为 0，两次结果 **68 / 81** |

**引擎基本忽略采样与控制类参数，只保证「能生成」。**

这是引擎的行为，不是网关的 —— 网关原样透传，从不改写这些字段。

## 本版改了什么

文档 + 一处指标说明：

- `docs/UPSTREAM-CAPABILITIES.md`：逐项实测、影响面、可行的绕法、复核命令；
- `/metrics` 里那个计数器的说明补上「非零意味着上游忽略 max_tokens」并指向文档。

**没有改运行时行为** —— 这些限制网关修不了：token 在引擎侧已经生成、已经计费，
网关侧截断只会省钱给客户端看，代价是给出残缺回答。
"""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).parents[2]
DOC = ROOT / 'docs' / 'UPSTREAM-CAPABILITIES.md'
MAIN = (ROOT / 'server' / 'main.py').read_text(encoding='utf-8')


class TheDocumentRecordsMeasuredBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text(encoding='utf-8')

    def test_it_exists(self):
        self.assertTrue(DOC.is_file())

    def test_it_covers_every_parameter_we_tested(self):
        for field in ('max_tokens', 'max_completion_tokens', 'stop', 'temperature'):
            self.assertIn(field, self.text, f'缺少 {field} 的实测结论')

    def test_it_states_that_temperature_zero_is_not_deterministic(self):
        self.assertIn('68', self.text)
        self.assertIn('81', self.text)

    def test_it_records_the_max_tokens_numbers(self):
        self.assertIn('560', self.text)
        self.assertIn('185', self.text)
        self.assertIn('338', self.text)

    def test_it_records_that_streaming_usage_works(self):
        """这一项是好的，不能只写坏消息。"""
        self.assertIn('[DONE]', self.text)
        self.assertIn('usage', self.text)

    def test_it_says_this_is_the_engine_not_the_gateway(self):
        self.assertIn('绕过网关', self.text)
        self.assertIn('引擎', self.text)

    def test_it_explains_why_the_gateway_does_not_truncate(self):
        """不做的理由要写清 —— 否则后人会以为这是漏了实现。"""
        self.assertIn('已经计费', self.text)

    def test_it_gives_reproduction_commands(self):
        self.assertIn('curl -s -X POST', self.text)

    def test_it_warns_the_conclusions_are_version_specific(self):
        self.assertIn('引擎升级后重跑', self.text)


class TheCounterPointsAtTheDocumentTests(unittest.TestCase):
    def test_metric_help_mentions_the_document(self):
        self.assertIn('UPSTREAM-CAPABILITIES.md', MAIN,
                      '指标说明要能把人引到能力边界文档')

    def test_metric_help_explains_what_non_zero_means(self):
        start = MAIN.index('workbuddy_output_over_limit_total')
        window = MAIN[start:start + 400]
        self.assertIn('ignores max_tokens', window)


class NoRuntimeBehaviourWasChangedTests(unittest.TestCase):
    """这版只该动文档与说明文本。"""

    def test_observability_counter_logic_is_untouched(self):
        source = (ROOT / 'server' / 'observability.py').read_text(encoding='utf-8')
        self.assertIn('def note_output_over_limit', source)
        # 仍然是「比较后自增」，没有引入新分支
        start = source.index('def note_output_over_limit')
        block = source[start:start + 700]
        self.assertIn('if got > want', block)


if __name__ == '__main__':
    unittest.main()
