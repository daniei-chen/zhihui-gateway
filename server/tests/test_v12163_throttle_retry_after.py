"""v1.2.163：429 限流现在告诉客户端要等多久。

## 起因

查一个用户的实际调用记录：两天里对同一个**上游并不存在**的模型试了 **55 次**，
全部失败。原因是状态码让他（或他的客户端）以为「稍后重试可能成功」。

**同样的形状出现在限流上。** 429 不带 `Retry-After` 时，客户端的退避库只能自己猜：
常见默认是立刻重试或固定 1 秒，**而窗口是 60 秒** —— 于是它在窗口内反复撞，越撞越远。

## 等待时间是算出来的

| 情况 | 等待 | 依据 |
| --- | --- | --- |
| 频率超限 | **60 秒** | 等整个窗口过去（`RATE_WINDOW`），这是能算出上界的 |
| 并发名额满 | **1 秒** | 等当前在跑的请求让出名额，通常很快 |

**不是拍一个固定值。** 两种情况的等待原因不同，给的数字也该不同。

## 为什么头和体都写

响应头路径在 nginx 反代链路上透传不稳（这是仓库里既有的结论，
`_oai_error_with_blip_body` 就是为同一个原因存在），而错误体字段任何
OpenAI 兼容客户端都能原样读到。

**两个都写，谁通就用谁。** 只写一个的话，另一条路径上的人什么都拿不到 ——
这正是「改了但没人收到」的那类失败。

## 本测试守什么

**真的调用那个函数**看产出的响应头与响应体，而不是 grep 源码。
这类「约定形状」的东西，源码里写对了但组装错了是很常见的。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
GATEWAY = (SERVER / 'routers' / 'gateway.py').read_text(encoding='utf-8')
SOURCE = GATEWAY


def _load_helper():
    """Exercise the real helper and its shared error/identity dependencies."""
    from server.routers.gateway import _oai_error_throttled
    return _oai_error_throttled


class ItSendsRetryAfterTests(unittest.TestCase):
    def test_header_is_set(self):
        resp = _load_helper()('太频繁', 'rate_limit_exceeded', 60)
        self.assertEqual(resp.headers.get('retry-after'), '60')

    def test_body_carries_the_same_number(self):
        """头透传不稳时，客户端只能靠体。两处必须一致。"""
        import json
        resp = _load_helper()('太频繁', 'rate_limit_exceeded', 60)
        payload = json.loads(bytes(resp.body).decode('utf-8'))
        self.assertEqual(payload['error']['retry_after_seconds'], 60)

    def test_message_mentions_the_wait(self):
        import json
        resp = _load_helper()('太频繁', 'rate_limit_exceeded', 60)
        payload = json.loads(bytes(resp.body).decode('utf-8'))
        self.assertIn('60', payload['error']['message'])

    def test_status_is_429(self):
        resp = _load_helper()('太频繁', 'rate_limit_exceeded', 60)
        self.assertEqual(resp.status_code, 429)

    def test_code_is_preserved(self):
        import json
        resp = _load_helper()('x', 'key_concurrency_exceeded', 1)
        payload = json.loads(bytes(resp.body).decode('utf-8'))
        self.assertEqual(payload['error']['code'], 'key_concurrency_exceeded')


class TheTwoCasesUseDifferentWaitsTests(unittest.TestCase):
    """等待原因不同，数字也该不同 —— 都用同一个常数就等于没算。"""

    def test_rate_limit_uses_the_window(self):
        idx = GATEWAY.find("'rate_limit_exceeded'")
        self.assertGreater(idx, 0)
        window = GATEWAY[max(0, idx - 400):idx + 120]
        self.assertIn('RATE_WINDOW', window,
                      '频率超限没有用窗口长度，客户端会等错时间')

    def test_concurrency_uses_one_second(self):
        idx = GATEWAY.find("'key_concurrency_exceeded', 1")
        self.assertGreater(idx, 0,
                           '并发超限没有给 1 秒')

    def test_they_are_not_the_same_constant(self):
        self.assertNotIn("'rate_limit_exceeded', 1", GATEWAY,
                         '两种情况用了同一个等待值')


class TheOldCallSitesAreGoneTests(unittest.TestCase):
    def test_no_bare_429_oai_error_remains(self):
        """旧的写法是 `_oai_error(msg, 429, ...)` —— 不带等待时间。"""
        self.assertNotIn("_oai_error('密钥并发容量已满", GATEWAY)
        self.assertNotIn("_oai_error(msg, 429, 'rate_limit_error'", GATEWAY)

    def test_helper_is_module_level(self):
        tree = ast.parse(SOURCE)
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertIn('_oai_error_throttled', names,
                      '定义被塞进了别的函数里（这个坑踩过）')


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_it_records_the_incident(self):
        idx = GATEWAY.find('def _oai_error_throttled')
        block = GATEWAY[idx:idx + 2400]
        self.assertIn('55 次', block, '没写出触发这件事的具体数字')

    def test_it_explains_the_guessing_problem(self):
        idx = GATEWAY.find('def _oai_error_throttled')
        block = GATEWAY[idx:idx + 2400]
        self.assertIn('猜', block)

    def test_it_says_why_both_header_and_body(self):
        idx = GATEWAY.find('def _oai_error_throttled')
        block = GATEWAY[idx:idx + 2400]
        self.assertIn('透传不稳', block)

    def test_it_says_the_number_is_computed(self):
        """等待时间是从窗口长度算出来的，不是随手给的常数。"""
        idx = GATEWAY.find('def _oai_error_throttled')
        block = GATEWAY[idx:idx + 2400]
        self.assertIn('能算出', block)


if __name__ == '__main__':
    unittest.main()
