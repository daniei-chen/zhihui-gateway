"""开头连续 system 消息合并（promptprep）的回归测试。

要点全在「不该动的时候别动」：带额外键、非字符串内容、非开头的 system 消息，
一旦被合并就会改变请求含义，而这种改变在用户那边看不出来。
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import promptprep  # noqa: E402


def body(*messages) -> dict:
    return {'model': 'glm-5.3-flash', 'messages': list(messages)}


def sys_msg(text, **extra) -> dict:
    message = {'role': 'system', 'content': text}
    message.update(extra)
    return message


class CollapseTest(unittest.TestCase):
    def test_two_leading_merged_in_order(self) -> None:
        payload = body(sys_msg('第一段。'), sys_msg('第二段。'),
                       {'role': 'user', 'content': '你好'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 1)
        self.assertEqual(len(payload['messages']), 2)
        self.assertEqual(payload['messages'][0]['content'], '第一段。\n\n第二段。')
        self.assertEqual(payload['messages'][1], {'role': 'user', 'content': '你好'})

    def test_three_leading_merged(self) -> None:
        payload = body(sys_msg('a'), sys_msg('b'), sys_msg('c'),
                       {'role': 'user', 'content': 'q'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 2)
        self.assertEqual(payload['messages'][0]['content'], 'a\n\nb\n\nc')

    def test_single_system_untouched(self) -> None:
        payload = body(sys_msg('one'), {'role': 'user', 'content': 'q'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 0)
        self.assertEqual(len(payload['messages']), 2)

    def test_extra_keys_block_merge(self) -> None:
        """`name` 等附加键各有语义，带着它们合并等于丢掉这些语义。"""
        payload = body(sys_msg('a', name='planner'), sys_msg('b'),
                       {'role': 'user', 'content': 'q'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 0)
        self.assertEqual(len(payload['messages']), 3)

    def test_non_string_content_block_merge(self) -> None:
        payload = body(
            {'role': 'system', 'content': [{'type': 'text', 'text': 'a'}]},
            sys_msg('b'), {'role': 'user', 'content': 'q'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 0)

    def test_middle_system_untouched(self) -> None:
        """非开头的 system 位置有意义（工具结果后的提醒），不能挪。"""
        payload = body({'role': 'user', 'content': 'q'},
                       sys_msg('a'), sys_msg('b'))
        self.assertEqual(promptprep.collapse_leading_system(payload), 0)
        self.assertEqual(len(payload['messages']), 3)

    def test_system_then_user_then_system(self) -> None:
        payload = body(sys_msg('a'), {'role': 'user', 'content': 'q'},
                       sys_msg('b'))
        self.assertEqual(promptprep.collapse_leading_system(payload), 0)

    def test_empty_texts_are_skipped(self) -> None:
        """全空白的几条不该合并成空串 —— 那只会让差异记录里多一条假改动。"""
        payload = body(sys_msg('   '), sys_msg('\n'), {'role': 'user', 'content': 'q'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 0)

    def test_blank_among_texts_dropped(self) -> None:
        payload = body(sys_msg('a'), sys_msg('  '), sys_msg('b'),
                       {'role': 'user', 'content': 'q'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 2)
        self.assertEqual(payload['messages'][0]['content'], 'a\n\nb')

    def test_no_messages_is_noop(self) -> None:
        for payload in ({}, {'messages': []}, {'messages': 'x'}, None, []):
            with self.subTest(payload=payload):
                self.assertEqual(promptprep.collapse_leading_system(payload), 0)

    def test_developer_role_not_merged(self) -> None:
        """`developer` 与 `system` 不等价，混在一起合并会改变 role 语义。"""
        payload = body({'role': 'developer', 'content': 'a'}, sys_msg('b'),
                       {'role': 'user', 'content': 'q'})
        self.assertEqual(promptprep.collapse_leading_system(payload), 0)

    def test_preserves_rest_of_request(self) -> None:
        payload = body(sys_msg('a'), sys_msg('b'), {'role': 'user', 'content': 'q'})
        payload['temperature'] = 0.2
        payload['tools'] = [{'type': 'function', 'function': {'name': 'f'}}]
        promptprep.collapse_leading_system(payload)
        self.assertEqual(payload['temperature'], 0.2)
        self.assertEqual(len(payload['tools']), 1)


if __name__ == '__main__':
    unittest.main()
