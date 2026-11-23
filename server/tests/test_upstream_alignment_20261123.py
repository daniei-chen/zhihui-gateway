"""上游对齐修复（2026-11-23）：粘帧、结构化输出映射、输出预算耗尽。

三件事都来自同一个现象：**网关把上游的真实意图丢了**。

1. 一条 `data:` 行里并列两个完整 JSON 对象时，整行 `json.loads` 会抛错，
   于是整条流被判成协议错误 —— 正文、工具分片、用量一起丢。上游真这么发过。
2. `output_config.format` 完全没读：客户端以为拿到了受 schema 约束的结果，
   实际是自由生成，且没有任何迹象表明约束被丢掉了。
3. `finish_reason: length` 一律当成功交付：思考把预算吃光时客户端收到的是
   一个空回答，它会原样无限重试。

全部离线可测，不需要上游。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server import observability  # noqa: E402
from server.routers.anthropic import (  # noqa: E402
    BUDGET_EXHAUSTED_MESSAGE,
    OutputBudgetExhausted,
    OutputFormatError,
    _StreamTranslator,
    _budget_exhausted,
    to_anthropic_response,
    to_openai_request,
)


def _feed(*chunks):
    """跑一遍审计器，返回 (audit, 解析出的对象列表)。"""
    audit = observability.StreamAudit(1 << 20)
    out = []
    for chunk in chunks:
        out.extend(audit.feed(chunk))
    out.extend(audit.finish())
    return audit, out


class StickyFrameTest(unittest.TestCase):
    """同一 `data:` 行里的多个 JSON 对象必须逐个处理。"""

    def test_two_objects_on_one_line_both_survive(self) -> None:
        chunk = ('data: {"choices":[{"delta":{"content":"你"}}]}'
                 '{"choices":[{"delta":{"content":"好"}}]}\n\n')
        audit, objects = _feed(chunk.encode())
        self.assertIsNone(audit.error, '粘帧不能被判成协议错误')
        self.assertEqual([o['choices'][0]['delta']['content'] for o in objects], ['你', '好'])

    def test_multiple_data_lines_also_split(self) -> None:
        """两行 `data:` 被同一个事件边界收走时同样要拆开。"""
        chunk = ('data: {"choices":[{"delta":{"content":"a"}}]}\n'
                 'data: {"choices":[{"delta":{"content":"b"}}]}\n\n')
        audit, objects = _feed(chunk.encode())
        self.assertIsNone(audit.error)
        self.assertEqual([o['choices'][0]['delta']['content'] for o in objects], ['a', 'b'])

    def test_usage_merged_across_objects(self) -> None:
        chunk = ('data: {"usage":{"prompt_tokens":7},'
                 '"choices":[{"delta":{"content":"x"}}]}'
                 '{"usage":{"completion_tokens":9},"choices":[]}\n\n')
        audit, _ = _feed(chunk.encode())
        self.assertEqual(audit.usage.get('prompt_tokens'), 7)
        self.assertEqual(audit.usage.get('completion_tokens'), 9)

    def test_finish_reason_in_sticky_frame_is_seen(self) -> None:
        chunk = ('data: {"choices":[{"delta":{"content":"x"}}]}'
                 '{"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        audit, _ = _feed(chunk.encode())
        self.assertTrue(audit.finish_seen)
        self.assertTrue(audit.completed)

    def test_error_envelope_not_overwritten_by_later_object(self) -> None:
        """错误信封先到、成功帧后到：错误必须留下，不能变成「成功」。"""
        chunk = ('data: {"error":{"message":"上游拒绝","code":"11140"}}'
                 '{"choices":[{"delta":{"content":"不该出现"},"finish_reason":"stop"}]}\n\n')
        audit, objects = _feed(chunk.encode())
        self.assertEqual(len(objects), 2, '后续对象仍要交给调用方去中断')
        self.assertEqual(audit.error, '上游拒绝')
        self.assertEqual(audit.error_code, '11140')
        self.assertFalse(audit.completed, '有错误信封就不能算完成')

    def test_malformed_tail_keeps_parsed_prefix(self) -> None:
        chunk = b'data: {"choices":[{"delta":{"content":"ok"}}]}{"brok\n\n'
        audit, objects = _feed(chunk)
        self.assertEqual(len(objects), 1, '能解析的前缀不能因为尾巴坏掉就丢')
        self.assertEqual(audit.error, '上游 SSE 包含无法解析的 JSON 事件')
        self.assertEqual(audit.error_stage, 'protocol')

    def test_done_sentinel_unchanged(self) -> None:
        audit, objects = _feed(b'data: [DONE]\n\n')
        self.assertEqual(objects, [])
        self.assertTrue(audit.done_seen)
        self.assertIsNone(audit.error)

    def test_plain_single_object_unchanged(self) -> None:
        audit, objects = _feed(b'data: {"choices":[{"delta":{"content":"solo"}}]}\n\n')
        self.assertEqual(len(objects), 1)
        self.assertIsNone(audit.error)


class OutputFormatMappingTest(unittest.TestCase):
    """`output_config.format` → 上游 `response_format`。"""

    def test_json_schema_mapped_with_defaults(self) -> None:
        out = to_openai_request({
            'model': 'x', 'max_tokens': 8, 'messages': [],
            'output_config': {'format': {
                'type': 'json_schema',
                'schema': {'type': 'object', 'properties': {'a': {'type': 'string'}}},
            }},
        })
        fmt = out['response_format']
        self.assertEqual(fmt['type'], 'json_schema')
        self.assertEqual(fmt['json_schema']['name'], 'response')
        self.assertIs(fmt['json_schema']['strict'], True)
        self.assertEqual(fmt['json_schema']['schema']['type'], 'object')

    def test_explicit_name_and_strict_preserved(self) -> None:
        out = to_openai_request({
            'model': 'x', 'max_tokens': 8, 'messages': [],
            'output_config': {'format': {
                'type': 'json_schema', 'name': '账单', 'strict': False,
                'schema': {'type': 'object'},
            }},
        })
        self.assertEqual(out['response_format']['json_schema']['name'], '账单')
        self.assertIs(out['response_format']['json_schema']['strict'], False)

    def test_unsupported_type_is_rejected(self) -> None:
        with self.assertRaises(OutputFormatError) as ctx:
            to_openai_request({
                'model': 'x', 'max_tokens': 8, 'messages': [],
                'output_config': {'format': {'type': 'text'}},
            })
        self.assertEqual(ctx.exception.code, 'unsupported_output_format')

    def test_missing_schema_is_rejected(self) -> None:
        for fmt in ({'type': 'json_schema'},
                    {'type': 'json_schema', 'schema': {}},
                    {'type': 'json_schema', 'schema': []}):
            with self.subTest(fmt=fmt):
                with self.assertRaises(OutputFormatError) as ctx:
                    to_openai_request({'model': 'x', 'max_tokens': 8,
                                       'messages': [], 'output_config': {'format': fmt}})
                self.assertEqual(ctx.exception.code, 'invalid_output_format')

    def test_no_format_means_no_response_format(self) -> None:
        """只有 effort 的 `output_config` 不能被误判成结构化输出请求。"""
        out = to_openai_request({'model': 'x', 'max_tokens': 8, 'messages': [],
                                 'output_config': {'effort': 'high'}})
        self.assertNotIn('response_format', out)
        self.assertEqual(out['reasoning_effort'], 'high')


def _upstream(*, text=None, reasoning=None, finish='length', calls=None) -> dict:
    message: dict = {}
    if text is not None:
        message['content'] = text
    if reasoning is not None:
        message['reasoning_content'] = reasoning
    if calls is not None:
        message['tool_calls'] = calls
    return {'choices': [{'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 64}}


class NonStreamBudgetTest(unittest.TestCase):
    """非流式：预算耗尽且没有交付 → 失败，不是「成功但很短」。"""

    def test_reasoning_only_becomes_failure(self) -> None:
        with self.assertRaises(OutputBudgetExhausted) as ctx:
            to_anthropic_response(_upstream(text=None, reasoning='想了很久'), 'm',
                                  thinking=True)
        self.assertEqual(str(ctx.exception), BUDGET_EXHAUSTED_MESSAGE)

    def test_whitespace_only_counts_as_nothing(self) -> None:
        with self.assertRaises(OutputBudgetExhausted):
            to_anthropic_response(_upstream(text='   \n', reasoning='想了很久'), 'm')

    def test_text_kept_with_max_tokens_stop_reason(self) -> None:
        out = to_anthropic_response(_upstream(text='已经写出半句'), 'm')
        self.assertEqual(out['stop_reason'], 'max_tokens')
        self.assertEqual(out['content'][0]['text'], '已经写出半句')

    def test_tool_calls_kept(self) -> None:
        out = to_anthropic_response(_upstream(calls=[{
            'id': 'call_1', 'type': 'function',
            'function': {'name': 'f', 'arguments': '{"a":1}'},
        }], finish='tool_calls'), 'm')
        self.assertEqual(out['stop_reason'], 'tool_use')

    def test_non_length_finish_untouched(self) -> None:
        """`stop` 且正文为空是另一回事（模型就是没说话），不归这条规则管。"""
        out = to_anthropic_response(_upstream(text=None, finish='stop'), 'm')
        self.assertEqual(out['content'], [])
        self.assertEqual(out['stop_reason'], 'end_turn')


class StreamBudgetTest(unittest.TestCase):
    """流式：同一条判据，作用在状态机上。"""

    def _translator(self, thinking=False):
        translator = _StreamTranslator('m', thinking=thinking)
        translator.defer_finish = True
        return translator

    def test_reasoning_only_marks_exhausted(self) -> None:
        translator = self._translator(thinking=True)
        translator.feed({'choices': [{'delta': {'reasoning_content': '想了很久'}}]})
        translator.feed({'choices': [{'delta': {}, 'finish_reason': 'length'}]})
        self.assertTrue(_budget_exhausted(translator))

    def test_visible_text_is_delivery(self) -> None:
        translator = self._translator()
        translator.feed({'choices': [{'delta': {'content': '有内容'}}]})
        translator.feed({'choices': [{'delta': {}, 'finish_reason': 'length'}]})
        self.assertFalse(_budget_exhausted(translator))

    def test_whitespace_only_is_not_delivery(self) -> None:
        translator = self._translator()
        translator.feed({'choices': [{'delta': {'content': ' '}}]})
        translator.feed({'choices': [{'delta': {}, 'finish_reason': 'length'}]})
        self.assertTrue(_budget_exhausted(translator))

    def test_tool_delta_is_delivery(self) -> None:
        translator = self._translator()
        translator.feed({'choices': [{'delta': {'tool_calls': [{
            'index': 0, 'id': 'call_1',
            'function': {'name': 'f', 'arguments': '{"a":'}}]}}]})
        translator.feed({'choices': [{'delta': {}, 'finish_reason': 'length'}]})
        self.assertFalse(_budget_exhausted(translator))

    def test_stop_finish_is_not_budget_exhaustion(self) -> None:
        translator = self._translator()
        translator.feed({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
        self.assertFalse(_budget_exhausted(translator))

    def test_finish_still_emits_message_stop_when_content_exists(self) -> None:
        """有正文时收尾必须照常（不能因为新增判据把正常路径也改了）。"""
        translator = self._translator()
        events = []
        events += translator.feed({'choices': [{'delta': {'content': '答'}}]})
        events += translator.finish('length', force=True)
        joined = b''.join(events).decode()
        self.assertIn('message_stop', joined)
        self.assertIn('"stop_reason": "max_tokens"', joined)


class BudgetExhaustionWiringTest(unittest.TestCase):
    """错误事件的形状：客户端要能从 code 直接判断该不该重试。"""

    def test_error_code_is_stable(self) -> None:
        from server import observability as obs
        payload = json.loads(obs.sse_error(
            BUDGET_EXHAUSTED_MESSAGE, code='output_budget_exhausted',
            leading_separator=True).split(b'data: ')[1].split(b'\n')[0])
        self.assertEqual(payload['error']['code'], 'output_budget_exhausted')
        self.assertIn('预算', payload['error']['message'])


if __name__ == '__main__':
    unittest.main()
