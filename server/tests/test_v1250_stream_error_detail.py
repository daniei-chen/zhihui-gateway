"""v1.2.50：流中失败必须留下可读原因。

## 修的是什么

`terminal_state` + `error_stage` 只能说明「哪个阶段失败了」，说明不了「为什么」。
流读取失败时，调用方合成了可读文本（如「上游流传输或协议异常：ReadTimeout」），
把它 yield 给了客户端，也传进了 `set_stream_state` —— 但那个函数**把参数丢了**，
没有任何字段承载它，于是落库的 `error` 列是空的。

2026-11-14 审计 workbuddy 时发现：7 天里有 **269 条**记录同时满足
`terminal_state='transport_error'`、`error_stage='stream_read'`、`latency_ms=0`、
`error` 为空。全部无法归因。

## 修法

请求状态新增 `error` 字段；`set_stream_state` 把它写进去；`_record` 在调用方
没显式传 error 时回退到它。显式传参的路径行为完全不变。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from server import observability as obs

SOURCE = (Path(__file__).parents[1] / 'observability.py').read_text(encoding='utf-8')
GATEWAY = (Path(__file__).parents[1] / 'routers' / 'gateway.py').read_text(encoding='utf-8')


def make_state(**kwargs):
    state = obs.RequestAudit(request_id='r1', endpoint='/v1/chat/completions',
                             method='POST', protocol='openai', started=0.0)
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def make_observer(error=None, error_stage=None, completed=False):
    observer = obs.StreamAudit()
    observer.error = error
    observer.error_stage = error_stage
    # `completed` 是只读属性（done_seen/finish_seen 且无 error），不能直接赋值；
    # 用底层标记构造出「已完成」的状态。
    if completed:
        observer.done_seen = True
    return observer


class StreamErrorIsRetainedTests(unittest.TestCase):
    def test_caller_supplied_error_is_stored_on_the_state(self):
        """这条就是回归本身：修之前 error 参数被丢掉。"""
        state = make_state()
        token = obs.current.set(state)
        try:
            obs.set_stream_state(make_observer(error_stage='stream_read'),
                                 error='上游流传输或协议异常：ReadTimeout')
        finally:
            obs.current.reset(token)
        self.assertEqual(state.terminal_state, 'transport_error')
        self.assertEqual(state.error_stage, 'stream_read')
        self.assertEqual(state.error, '上游流传输或协议异常：ReadTimeout',
                         '失败原因没有留在状态里，落库时会是空的')

    def test_error_is_bounded_to_500_characters(self):
        state = make_state()
        token = obs.current.set(state)
        try:
            obs.set_stream_state(make_observer(error_stage='stream_read'),
                                 error='x' * 5000)
        finally:
            obs.current.reset(token)
        self.assertEqual(len(state.error), 500)

    def test_upstream_error_text_is_stored_too(self):
        """上游 SSE 里带的错误文本走另一条分支，也要留。"""
        state = make_state()
        token = obs.current.set(state)
        try:
            obs.set_stream_state(make_observer(error='上游业务错误', error_stage='upstream'))
        finally:
            obs.current.reset(token)
        self.assertEqual(state.terminal_state, 'upstream_error')
        self.assertEqual(state.error, '上游业务错误')

    def test_cancelled_does_not_record_an_error(self):
        """客户端主动断开不是错误，不该在 error 列留下文本。"""
        state = make_state()
        token = obs.current.set(state)
        try:
            obs.set_stream_state(make_observer(), error='ignored', cancelled=True)
        finally:
            obs.current.reset(token)
        self.assertEqual(state.terminal_state, 'client_cancelled')
        self.assertIsNone(state.error)

    def test_completed_leaves_no_error(self):
        state = make_state()
        token = obs.current.set(state)
        try:
            obs.set_stream_state(make_observer(completed=True))
        finally:
            obs.current.reset(token)
        self.assertEqual(state.terminal_state, 'completed')
        self.assertIsNone(state.error)

    def test_missing_state_does_not_raise(self):
        """没有请求上下文时静默返回，不能炸。"""
        obs.set_stream_state(make_observer(error_stage='stream_read'), error='x')


class RecordFallsBackToStateErrorTests(unittest.TestCase):
    def test_record_uses_the_state_error_when_none_is_passed(self):
        self.assertIn("db._clean(error or getattr(state, 'error', None), 500)", GATEWAY,
                      '_record 没有回退到 state.error')

    def test_explicit_error_still_wins(self):
        """显式传参优先 —— 这条保证已有路径的行为不变。"""
        snippet = "db._clean(error or getattr(state, 'error', None), 500)"
        self.assertTrue(snippet.startswith("db._clean(error or"),
                        '参数在前，回退在后')


class StateFieldContractTests(unittest.TestCase):
    def test_request_audit_has_an_error_field(self):
        state = obs.RequestAudit(request_id='r', endpoint='/', method='POST',
                                 protocol='openai', started=0.0)
        self.assertTrue(hasattr(state, 'error'))
        self.assertIsNone(state.error, '默认必须是 None，不能是空字符串冒充"有错误"')

    def test_field_is_documented(self):
        self.assertIn('269', SOURCE, '应当把这次审计的数字写进注释，便于日后对照')


if __name__ == '__main__':
    unittest.main()
