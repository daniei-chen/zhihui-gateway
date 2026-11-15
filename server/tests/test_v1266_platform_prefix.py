"""v1.2.66：前缀写错时，别再让上游替你报错。

## 实测发现的缺陷

用临时密钥打了几个边界输入：

| 输入 | 结果 |
| --- | --- |
| 畸形 JSON | 400 请求体不是合法 JSON |
| 空 body | 400 请求体不是合法 JSON |
| `model` 是数字 | 400 model 必须是字符串 |
| **`zhihui/whatever`** | **503 no_healthy_account** |

前三个都在本地被正确拒绝了。第四个没有 —— 网关把这个字符串**原样转发**给上游，
上游回一句 `no such model`，用户看到 503 只会以为模型不存在，**而真正的错因是前缀
写错了**。

## 修法

在路由阶段就地判断：模型名带斜杠、前缀**像**平台名却不是合法平台时，返回 400 并
列出可用平台。

**刻意不泛化**：真实模型名也有带斜杠的（`meta-llama/Llama-3` 这类），一律拒绝会
误伤。所以只对一张明确的清单提示 —— 保守，但覆盖实际踩过的坑。
"""
from __future__ import annotations

import json
import unittest

from server.routers import gateway


def _body(response) -> dict:
    return json.loads(response.body.decode('utf-8'))


class MisusedPrefixIsRejectedLocallyTests(unittest.TestCase):
    CASES = ('zhihui/x', 'workbuddy2/x', 'qtg/x', 'buddy/x', 'manager/x',
             'gateway/x', 'wb/x')

    def test_suspicious_prefixes_are_rejected_with_400(self):
        for model in self.CASES:
            response = gateway._misused_platform_prefix(model)
            self.assertIsNotNone(response, f'{model} 应该被就地拒绝')
            self.assertEqual(response.status_code, 400)

    def test_the_error_names_the_usable_platforms(self):
        response = gateway._misused_platform_prefix('zhihui/x')
        message = _body(response)['error']['message']
        for platform in ('workbuddy', 'qoder', 'trae'):
            self.assertIn(platform, message, '错误里要直接告诉用户能用哪些')

    def test_the_error_code_is_stable(self):
        response = gateway._misused_platform_prefix('zhihui/x')
        self.assertEqual(_body(response)['error']['code'], 'unknown_platform_prefix')

    def test_it_gives_a_slash_model_example(self):
        """用户很可能就是照着文档写的，给个能直接抄的例子。"""
        response = gateway._misused_platform_prefix('zhihui/x')
        self.assertIn('trae/Doubao-Seed-Code', _body(response)['error']['message'])

    def test_it_mentions_the_legitimate_slash_case(self):
        """有些模型名真的带斜杠，别让这些人以为自己的名字非法。"""
        response = gateway._misused_platform_prefix('zhihui/x')
        self.assertIn('模型名本身带斜杠', _body(response)['error']['message'])


class LegitimateNamesAreNotTouchedTests(unittest.TestCase):
    CASES = (
        'gpt-x', '', None, 123,
        'trae/Doubao-Seed-Code', 'qoder/glm-5', 'workbuddy/deepseek-v4.1-flash',
        # 真实存在的、名字自带斜杠的模型：一律拒绝会误伤
        'meta-llama/Llama-3-70B', 'Qwen/Qwen2.5-72B', 'deepseek-ai/DeepSeek-V3',
    )

    def test_they_pass_through(self):
        for model in self.CASES:
            self.assertIsNone(gateway._misused_platform_prefix(model),
                              f'{model!r} 不该被拒绝')

    def test_a_bare_model_without_slash_passes(self):
        self.assertIsNone(gateway._misused_platform_prefix('doubao-pro'))

    def test_non_string_values_do_not_crash(self):
        for value in (None, 0, [], {}, 3.14):
            self.assertIsNone(gateway._misused_platform_prefix(value))


class RoutingCallsTheCheckTests(unittest.TestCase):
    """确认它真的接在路由路径上，而不只是一个没人调的函数。"""

    def test_upstream_for_rejects_before_resolving(self):
        import inspect
        source = inspect.getsource(gateway._upstream_for)
        self.assertIn('_misused_platform_prefix', source,
                      '检查没接进路由，等于没做')

    def test_the_check_runs_before_the_upstream_lookup(self):
        import inspect
        source = inspect.getsource(gateway._upstream_for)
        check_at = source.index('_misused_platform_prefix')
        resolve_at = source.index('resolve_for_key')
        self.assertLess(check_at, resolve_at,
                        '检查应当在解析上游之前 —— 否则白跑一趟上游')

    def test_the_prefix_list_is_documented(self):
        """清单是保守的白名单，得写明为什么不能泛化。"""
        source = inspect_source()
        self.assertIn('不能泛化判断', source)
        self.assertIn('meta-llama', source, '要举出会被误伤的真实例子')


def inspect_source() -> str:
    import inspect
    return inspect.getsource(gateway)


if __name__ == '__main__':
    unittest.main()
