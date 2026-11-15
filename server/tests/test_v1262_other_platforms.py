"""v1.2.62：模型清单标注「其它平台也能用」的模型。

## 为什么要它

`/v1/models` 一直按**本次请求的上游**返回清单。对「此刻能看到什么」是准确的，
但它回答不了另一个问题：**这个密钥到底还能用什么**。

具体表现：豆包系列在 trae 平台上一直可用（文本、流式、推理内容都实测过），但持有
workbuddy 入口密钥的人在列表里看不到它们 —— 于是这些能力**事实上不存在**，除非
有人专门去读文档。

## 关键的边界

`data` 数组一个字节都不动。把它们混进 `data` 会更糟：客户端选中一个需要前缀的
模型名，然后收到「不可用」——那比看不到还差。

标注放在同级字段里，客户端照旧拿 `data` 当选择器。
"""
from __future__ import annotations

import unittest
from unittest import mock

from server.routers import gateway


MODELS = {'data': [{'id': 'gpt-x'}, {'id': 'gpt-y'}]}


def key_with(*platforms):
    return {'platforms': list(platforms)}


class AnnotateOtherPlatformsTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload, key, current='workbuddy', *, upstream='ok',
                    response=None):
        calls = []

        class _Resp:
            status_code = response[0] if response else 200

            def json(self):
                return response[1] if response else {'data': [{'id': 'Doubao-Seed-2.1-Turbo'}]}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None):
                calls.append(url)
                if isinstance(upstream, Exception):
                    raise upstream
                return _Resp()

        with mock.patch.object(gateway, '_threeway_upstream',
                               return_value=(None if upstream is None else
                                             {'base_url': 'http://127.0.0.1:8080',
                                              'api_key': 'k'})), \
             mock.patch.object(gateway.config, 'http_client', return_value=_Client()), \
             mock.patch.object(gateway, '_upstream_headers_async',
                               new=mock.AsyncMock(return_value={})):
            result = await gateway._annotate_other_platforms(payload, key, current)
        return result, calls

    async def test_it_annotates_platforms_the_key_may_use(self):
        result, _calls = await self._call(
            dict(MODELS, data=list(MODELS['data'])), key_with('workbuddy', 'trae'))
        self.assertIn('trae', result['other_platforms'])
        self.assertIn('Doubao-Seed-2.1-Turbo', result['other_platforms']['trae'])

    async def test_it_never_touches_the_data_array(self):
        """混进 data 会让客户端选中一个需要前缀的名字，然后失败。"""
        payload = {'data': [{'id': 'gpt-x'}, {'id': 'gpt-y'}]}
        result, _calls = await self._call(payload, key_with('workbuddy', 'trae'))
        self.assertEqual([m['id'] for m in result['data']], ['gpt-x', 'gpt-y'])

    async def test_it_adds_a_hint_showing_the_prefix_form(self):
        result, _calls = await self._call(dict(MODELS), key_with('workbuddy', 'trae'))
        self.assertIn('trae/', result['other_platforms_hint'])

    async def test_it_skips_the_current_platform(self):
        result, calls = await self._call(dict(MODELS), key_with('workbuddy', 'trae'),
                                         current='trae')
        self.assertNotIn('trae', result.get('other_platforms') or {})
        self.assertEqual(calls, [], '对当前平台还去拉一遍清单，纯属浪费')

    async def test_it_skips_platforms_without_permission(self):
        result, calls = await self._call(dict(MODELS), key_with('workbuddy'))
        self.assertNotIn('other_platforms', result)
        self.assertEqual(calls, [])

    async def test_a_missing_upstream_is_skipped(self):
        result, _calls = await self._call(dict(MODELS), key_with('workbuddy', 'trae'),
                                          upstream=None)
        self.assertNotIn('other_platforms', result)

    async def test_an_exception_leaves_the_payload_intact(self):
        """标注是附加信息，某个平台挂了不能连坐整个模型列表。"""
        payload = {'data': [{'id': 'gpt-x'}]}
        result, _calls = await self._call(payload, key_with('workbuddy', 'trae'),
                                          upstream=RuntimeError('boom'))
        self.assertEqual(result, payload)

    async def test_a_non_200_is_skipped(self):
        result, _calls = await self._call(
            dict(MODELS), key_with('workbuddy', 'trae'), response=(503, {}))
        self.assertNotIn('other_platforms', result)

    async def test_a_payload_without_a_data_list_is_returned_untouched(self):
        for payload in ({}, {'data': 'nope'}, [], 'x'):
            result, _calls = await self._call(payload, key_with('workbuddy', 'trae'))
            self.assertEqual(result, payload)

    async def test_the_list_is_capped(self):
        many = {'data': [{'id': f'm{i}'} for i in range(200)]}
        result, _calls = await self._call(dict(MODELS), key_with('workbuddy', 'trae'),
                                          response=(200, many))
        self.assertEqual(len(result['other_platforms']['trae']), 80)


class AnthropicShapeSkipsAnnotationTests(unittest.TestCase):
    def test_the_endpoint_only_annotates_the_openai_shape(self):
        """Anthropic 的转换会重排结构，多出的字段留不住 —— 不该白拉一趟。"""
        source = (gateway.__file__)
        with open(source, encoding='utf-8') as handle:
            text = handle.read()
        start = text.index("if request.headers.get('anthropic-version'):")
        window = text[start:start + 600]
        self.assertIn('_annotate_other_platforms', window)
        self.assertIn('else:', window, '标注应当在 Anthropic 分支之外')


if __name__ == '__main__':
    unittest.main()
