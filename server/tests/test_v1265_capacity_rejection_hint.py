"""v1.2.65：容量拒绝要说清「是哪一种」，并留下可分析的占用。

## 问题

网关有六条不同的容量拒绝消息，用户看到的都是 503。最常出现的那条是「响应容量已
满，请稍后重试」—— 它对应**输出预留的内存预算**到顶，既不关并发数、也不关配额。

2026-11-14 实测：某密钥 12 分钟内被拒 20 次，全是这一条。当时并发约 59、单个请求
并不大，是「很多长输出同时在途」把 384 MiB 的预留吃满了。

原来的文案只说「容量已满」，用户据此做不了任何事；日志里也没有占用数字，事后无法
判断是「并发多」还是「某个请求声明得太大」。

## 改了什么

1. 带上 `gateway_hint`：说明这是容量保护，重试或降低长输出并发有用；
2. 拒绝时记一行带**声明输出 / 在途字节 / 上限**的日志；
3. 保留 `Retry-After`。
"""
from __future__ import annotations

import pathlib
import unittest

SOURCE = (pathlib.Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')


def rejection_block() -> str:
    """截出「输出预留被拒」那段。"""
    anchor = 'output_reserve(declared_output_bytes(body) + FAILURE_ENVELOPE_BYTES)'
    start = SOURCE.index(anchor)
    return SOURCE[start:start + 2200]


class CapacityRejectionIsActionableTests(unittest.TestCase):
    def test_the_rejection_carries_a_hint(self):
        block = rejection_block()
        self.assertIn('hint=', block, '拒绝时没给 hint，用户又只能看到「容量已满」')

    def test_the_hint_says_it_is_a_capacity_guard(self):
        block = rejection_block()
        self.assertIn('capacity guard', block)
        self.assertIn('not a key, quota or model problem', block,
                      '要明确排除「是不是我的密钥/配额/模型有问题」这个误判方向')

    def test_the_hint_says_what_actually_helps(self):
        block = rejection_block()
        self.assertIn('Retry shortly', block)
        self.assertIn('concurrency of long-output requests', block)

    def test_retry_after_is_kept(self):
        block = rejection_block()
        self.assertIn("Retry-After", block)

    def test_it_logs_the_occupancy(self):
        """没有占用数字，事后判断不了是并发多还是单个请求太大。"""
        block = rejection_block()
        self.assertIn('logger.warning', block)
        self.assertIn('inflight_body_bytes', block)
        self.assertIn('maximum_body_bytes', block)
        self.assertIn('declared_output_bytes', block)

    def test_the_error_code_is_unchanged(self):
        """改文案不能改 code —— 客户端可能按它做重试判断。"""
        self.assertIn("'gateway_capacity_exceeded'", rejection_block())

    def test_the_status_is_still_503(self):
        self.assertIn('503', rejection_block())


class HintReachesTheResponseTests(unittest.TestCase):
    """确认 `_oai_error` 真的把 hint 放进响应体，而不是只接了个参数。"""

    def test_error_body_contains_gateway_hint(self):
        from server.routers import gateway
        response = gateway._oai_error('x', 503, 'server_error', 'gateway_capacity_exceeded',
                                      hint='capacity guard')
        body = response.body.decode('utf-8')
        self.assertIn('gateway_hint', body)
        self.assertIn('capacity guard', body)

    def test_without_hint_the_field_is_absent(self):
        from server.routers import gateway
        response = gateway._oai_error('x', 503, 'server_error', 'gateway_capacity_exceeded')
        self.assertNotIn('gateway_hint', response.body.decode('utf-8'))


class OtherCapacityMessagesAreDistinguishableTests(unittest.TestCase):
    """六条容量消息必须彼此可分，否则排障会往错方向走。

    它们分散在三个文件里：网关路由、中间件、管理入口。所以这里按「哪个文件里有」
    逐条查，而不是指望都在同一个文件。
    """

    SOURCES = {
        'server/routers/gateway.py': (
            '大上下文处理容量已满', '密钥并发容量已满',
            '入站与转发请求体容量已满', '响应容量已满'),
        'server/observability.py': ('网关处理容量已满',),
        'server/bodyguard.py': ('管理请求容量已满',),
    }

    def test_each_message_names_its_own_ceiling(self):
        root = pathlib.Path(__file__).parents[2]
        for rel, phrases in self.SOURCES.items():
            text = (root / rel).read_text(encoding='utf-8')
            for phrase in phrases:
                self.assertIn(phrase, text, f'{rel} 里找不到「{phrase}」')

    def test_the_six_messages_are_all_distinct(self):
        """措辞必须能区分来源 —— 六条都叫「容量已满」就白写了。"""
        all_phrases = [p for group in self.SOURCES.values() for p in group]
        self.assertEqual(len(all_phrases), 6)
        self.assertEqual(len(set(all_phrases)), 6)

    def test_key_concurrency_is_429_not_503(self):
        """每密钥并发是限流语义，用 429；用 503 会让客户端以为是服务故障。"""
        start = SOURCE.index('密钥并发容量已满')
        window = SOURCE[max(0, start - 400):start]
        self.assertIn('429', window)


if __name__ == '__main__':
    unittest.main()
