"""v1.2.88：诊断报告里的错误码，八成是 `other`。

## 怎么发现的

审计「上游行为适配是否还准确」时，把 `diagnostics.py` 的错误码白名单和数据库里
**实际出现过**的码做了对比。

实测（2026-11-15，全量 `request_outcomes`）：

| 项 | 数量 |
| --- | --- |
| 实际出现过的错误码 | **23 种** |
| 白名单认识 | **4 种** |
| 被判成 `other` | **19 种** |

被丢掉的包括出现次数最多的 `gateway.auth.denied`（6593 次）。

**也就是说，离线诊断报告的「错误码分布」几乎全是 `other`，那份报告没有信息量。**

## 根因

白名单永远滞后于命名演进：早期是数字码与裸名字，后来扩展成 `gateway.*` /
`portal_*` 命名空间。往后还会有新的。

## 修法

过滤器的本意是**不让任意字符串进报告**。格式约束同样做得到，而且不过期：

| 形状 | 例子 |
| --- | --- |
| `gateway.*` | `gateway.auth.denied`、`gateway.transport.error` |
| `portal_*` | `portal_wallet_pending` |
| 裸名字（可含单点号分段） | `client.cancelled`、`provider_attempt_budget_exhausted` |
| 纯数字上游码 | `11128`、`10605`、`503` |

另外单独挡长度（3–64）与单段长度（≤40），因为真实码没有更长的。

**旧白名单保留**：老库的离线分析结果不变。
"""
from __future__ import annotations

import inspect
import unittest

from server import diagnostics

#: 2026-11-15 从生产库导出的全部真实错误码。
REAL_CODES = [
    'gateway.auth.denied', 'portal_wallet_pending', 'client.cancelled',
    'key_concurrency_exceeded', 'gateway.rate_limited', 'gateway.capacity.rejected',
    'gateway.request.invalid', 'gateway.transport.error', 'portal_hard_cap_unverified',
    'upstream_truncated', 'gateway.interrupted', 'gateway.upstream.error',
    'no_healthy_account', 'upstream_timeout', 'gateway.application.error',
    'rate_limit_exceeded', 'prompt_too_long', 'portal_wallet_empty', 'budget_pending',
    'body_read_timeout', 'upstream_output_limit', 'channel_check_rejected',
    'upstream_transport_uncertain', '503', '11128', '10605',
    'client_cancelled', 'invalid_messages', 'invalid_model', 'budget_rejected_unknown',
]

MALFORMED = [
    '', 'a', 'has space', 'quote"x', 'slash/x', 'new\nline', 'x' * 200, 'x' * 60,
    '../etc/passwd', 'SELECT * FROM t', '中文', 'a b c', 'x;rm -rf /', '..', 'a..b',
    '.hidden', 'a.', '-dash', '3abc', 'tab\there',
]


class RealCodesSurviveTests(unittest.TestCase):
    def test_every_real_code_is_kept(self):
        lost = [c for c in REAL_CODES if diagnostics._metadata('error_code', c) == 'other']
        self.assertEqual(lost, [], f'这些真实错误码被丢掉了：{lost}')

    def test_the_most_frequent_one_specifically(self):
        """`gateway.auth.denied` 出现 6593 次，是最该被看见的那一个。"""
        self.assertNotEqual(
            diagnostics._metadata('error_code', 'gateway.auth.denied'), 'other')

    def test_dotted_bare_names_survive(self):
        """`client.cancelled` 曾是漏网的：分段形状让它被误伤。"""
        self.assertNotEqual(
            diagnostics._metadata('error_code', 'client.cancelled'), 'other')


class MalformedInputIsStillRejectedTests(unittest.TestCase):
    def test_nothing_malformed_gets_through(self):
        leaked = [b for b in MALFORMED if diagnostics._metadata('error_code', b) != 'other']
        self.assertEqual(leaked, [], f'这些畸形输入漏过了：{[x[:24] for x in leaked]}')

    def test_length_bounds(self):
        self.assertEqual(diagnostics._metadata('error_code', 'ab'), 'other')
        self.assertEqual(diagnostics._metadata('error_code', 'a' * 65), 'other')

    def test_no_paths_or_quotes_or_whitespace(self):
        for bad in ('../etc/passwd', 'a/b', 'a"b', 'a b', 'a\nb', 'a\tb'):
            self.assertEqual(diagnostics._metadata('error_code', bad), 'other', bad)


class LegacyAllowlistStillWorksTests(unittest.TestCase):
    def test_old_codes_pass(self):
        for code in ('11128', 'client_cancelled', 'provider_attempt_budget_exhausted',
                     'upstream_error', 'first_byte_timeout'):
            self.assertNotEqual(
                diagnostics._metadata('error_code', code), 'other', code)


class OtherFieldsAreUnchangedTests(unittest.TestCase):
    """这次只动错误码；别的枚举与数值范围不该被顺手改了。"""

    def test_http_status_still_bounded(self):
        self.assertEqual(diagnostics._metadata('http_status', 200), 200)
        self.assertEqual(diagnostics._metadata('http_status', 99), 'other')
        self.assertEqual(diagnostics._metadata('http_status', 600), 'other')

    def test_enum_still_whitelisted(self):
        self.assertEqual(diagnostics._metadata('terminal_state', 'completed'), 'completed')
        self.assertEqual(diagnostics._metadata('terminal_state', 'not-a-state'), 'other')

    def test_release_id_still_validated(self):
        self.assertEqual(diagnostics._metadata('release_id', 'v1.2.88'), 'v1.2.88')
        self.assertEqual(diagnostics._metadata('release_id', 'nonsense'), 'other')

    def test_unknown_field_names_still_dropped(self):
        self.assertEqual(diagnostics._metadata('prompt', 'anything'), 'other')


class TheReasonIsRecordedTests(unittest.TestCase):
    def test_the_module_explains_why_allowlist_became_a_pattern(self):
        source = inspect.getsource(diagnostics)
        start = source.index('_SAFE_ERROR_CODE')
        window = source[max(0, start - 1200):start]
        self.assertIn('19', window, '要写出实测的丢失数量，否则看不出问题的规模')

    def test_the_old_allowlist_is_kept_for_compatibility(self):
        self.assertIn('_KNOWN_CODES', inspect.getsource(diagnostics))
        self.assertTrue(diagnostics._KNOWN_CODES)


if __name__ == '__main__':
    unittest.main()
