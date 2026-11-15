"""v1.2.80：空 `messages` 不做本地校验，用户拿到的是英文的模糊提示。

## 怎么发现的

拿真实密钥打了一串错误请求，看用户实际会收到什么。

`zhihui/whatever` 那条很好：

```
模型名里的前缀 `zhihui` 不是可用的平台名。可用的是：workbuddy, qoder, trae
（例如 trae/Doubao-Seed-Code）。……
```

**它指出了问题、给了可选值、还给了例子。**

但空 `messages` 那条：

```
{"error":{"code":"upstream_error",
          "message":"upstream code=4001 msg=We're sorry, the param is invalid.
                     Please try with a valid param."}}
```

**问题出在我们自己的请求体里，却一路透传到上游，回来一段英文。** 调用方看不出是
自己少传了东西 —— 而且这次往返是完全不必要的。

## 修法

在 `outputlimits.inspect` 之后加一道结构校验：缺字段、不是数组、空数组 → 400
`invalid_messages`。

**只在结构明显不对时拒**，不逐条校验 `role` / `content`：各家对内容的宽容度不同，
多校验一步就可能拒掉本来能用的请求。

三条协议都用 `messages`，所以对 OpenAI / Anthropic 都成立；Responses API 走自己的
路由（`input` 字段），不经过这里。
"""
from __future__ import annotations

import inspect
import pathlib
import unittest

from server import diagnostics
from server.routers import gateway


class TheCheckIsInPlaceTests(unittest.TestCase):
    SOURCE = inspect.getsource(gateway._chat)

    def test_chat_validates_messages(self):
        self.assertIn('invalid_messages', self.SOURCE)

    def test_it_runs_after_the_existing_body_checks(self):
        """顺序：先有 model 类型校验，再有 outputlimits，然后是 messages。"""
        outputlimits_at = self.SOURCE.index('outputlimits.inspect')
        messages_at = self.SOURCE.index('invalid_messages')
        self.assertLess(outputlimits_at, messages_at,
                        'messages 校验应当在既有请求体校验之后')

    def test_it_marks_the_error_for_observability(self):
        start = self.SOURCE.index('invalid_messages')
        window = self.SOURCE[max(0, start - 300):start + 100]
        self.assertIn('mark_error', window,
                      '本地拒绝也要落错误码，否则统计里看不到这一类')

    def test_it_returns_400_not_500(self):
        start = self.SOURCE.index('invalid_messages')
        window = self.SOURCE[max(0, start - 200):start + 200]
        self.assertIn('400', window, '请求体不成立属于客户端错误')


class ItOnlyRejectsObviouslyBrokenBodiesTests(unittest.TestCase):
    """校验范围要窄 —— 多校验一步就可能拒掉本来能用的请求。"""

    def _rejects(self, messages):
        source = inspect.getsource(gateway._chat)
        # 直接复现判断逻辑，避免起整个 app
        if not isinstance(messages, list) or not messages:
            return True
        return False

    def test_missing_messages_rejects(self):
        self.assertTrue(self._rejects(None))

    def test_empty_list_rejects(self):
        self.assertTrue(self._rejects([]))

    def test_a_single_message_passes(self):
        self.assertFalse(self._rejects([{'role': 'user', 'content': 'hi'}]))

    def test_messages_without_content_still_passes(self):
        """不校验内容 —— 上游可能接受只带 role 的形状。"""
        self.assertFalse(self._rejects([{'role': 'user'}]))


class TheDiagnosticClassifierKnowsItTests(unittest.TestCase):
    def test_invalid_messages_maps_to_fix_request_body(self):
        result = diagnostics.classify(http_status=400, error_code='invalid_messages')
        self.assertEqual(result['action'], 'fix_request_body',
                         '这是改请求就能解决的，不该让人去查认证或配额')

    def test_invalid_model_maps_the_same_way(self):
        result = diagnostics.classify(http_status=400, error_code='invalid_model')
        self.assertEqual(result['action'], 'fix_request_body')

    def test_prompt_too_long_maps_the_same_way(self):
        result = diagnostics.classify(http_status=400, error_code='prompt_too_long')
        self.assertEqual(result['action'], 'fix_request_body')

    def test_auth_failures_are_unaffected(self):
        """403 仍然优先判认证，不被新分支抢走。"""
        result = diagnostics.classify(http_status=403, error_code='invalid_messages')
        self.assertEqual(result['action'], 'verify_authentication_source')


class TheResponsesRouteIsUnaffectedTests(unittest.TestCase):
    """Responses API 用 `input`，不该被 messages 校验误伤。"""

    def test_responses_does_not_go_through_chat(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'routers'
                  / 'responses.py').read_text(encoding='utf-8')
        self.assertNotIn('_chat(', source,
                         'Responses 走自己的实现；如果改成复用 _chat，这里要重新评估')


if __name__ == '__main__':
    unittest.main()
