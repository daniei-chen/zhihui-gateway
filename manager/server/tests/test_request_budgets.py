"""K21 · 时间预算、重试边界与取消传递的测试。

覆盖审计包 K21 的专项场景：连接/首包/流中空闲/总时长/客户端取消五类预算；
只在安全阶段重试；生成一旦开始就不重放。
"""
from __future__ import annotations

import asyncio
import httpx
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import deadline, observability  # noqa: E402


class BudgetsTest(unittest.TestCase):
    def test_defaults_match_current_behaviour(self) -> None:
        budgets = deadline.Budgets.from_config(600)
        self.assertEqual(budgets.connect_s, 5.0)
        self.assertEqual(budgets.stream_idle_s, 600.0)
        self.assertEqual(budgets.total_s, 600.0)
        # 2026-10-26：首包预算默认值由 min(60, base) 改为 base（= 读超时）。
        # 依据是生产事故 INC-02：上游 copilot.tencent.com 约 9.4% 的请求首字节
        # 超过 60s，60s 硬上限把它们全变成 502；而旧版 v2.8.3 的 first_byte
        # 只记指标、没有任何超时控制，慢请求能一直等到读超时。
        # 这条断言锁的是「默认等于读超时」这一**有意恢复的行为**，不是放宽阈值：
        # 上限仍然存在（= 600s，受 WB_UPSTREAM_TIMEOUT 约束），只是不再额外收紧。
        self.assertEqual(budgets.first_byte_s, 600.0)
        self.assertEqual(budgets.client_cancel_s, 5.0)

    def test_env_overrides(self) -> None:
        env = {'WB_FIRST_BYTE_TIMEOUT_S': '12', 'WB_STREAM_IDLE_TIMEOUT_S': '90',
               'WB_TOTAL_TIMEOUT_S': '120', 'WB_CONNECT_TIMEOUT_S': '3',
               'WB_CLIENT_CANCEL_S': '4'}
        with mock.patch.dict(os.environ, env, clear=False):
            budgets = deadline.Budgets.from_config(600)
        self.assertEqual((budgets.first_byte_s, budgets.stream_idle_s,
                          budgets.total_s, budgets.connect_s, budgets.client_cancel_s),
                         (12.0, 90.0, 120.0, 3.0, 4.0))

    def test_invalid_config_rejected_instead_of_silently_ignored(self) -> None:
        with mock.patch.dict(os.environ, {'WB_TOTAL_TIMEOUT_S': '-1'}, clear=False):
            with self.assertRaises(ValueError):
                deadline.Budgets.from_config(600)

    def test_total_clamps_smaller_budgets(self) -> None:
        budgets = deadline.Budgets(total_s=30, stream_idle_s=600, first_byte_s=120)
        normalized = budgets.normalized()
        self.assertEqual(normalized.total_s, 30.0)
        self.assertEqual(normalized.first_byte_s, 30.0)
        self.assertEqual(normalized.stream_idle_s, 30.0)
        self.assertLessEqual(normalized.first_byte_s, normalized.total_s)
        self.assertLessEqual(normalized.stream_idle_s, normalized.total_s)

    def test_non_positive_budget_rejected(self) -> None:
        with self.assertRaises(ValueError):
            deadline.Budgets(connect_s=0)
        with self.assertRaises(ValueError):
            deadline.Budgets(total_s=-5)


class DeadlineTest(unittest.TestCase):
    def test_next_timeout_kind_switches_after_first_byte(self) -> None:
        clock = {'now': 0.0}
        dl = deadline.Deadline(deadline.Budgets(first_byte_s=10, stream_idle_s=60,
                                                total_s=100), clock=lambda: clock['now'])
        timeout, kind = dl.next_timeout()
        self.assertEqual(kind, 'first_byte')
        self.assertEqual(timeout, 10.0)
        dl.mark_first_byte()
        timeout, kind = dl.next_timeout()
        self.assertEqual(kind, 'stream_idle')
        self.assertEqual(timeout, 60.0)

    def test_total_budget_wins_after_first_byte(self) -> None:
        clock = {'now': 0.0}
        dl = deadline.Deadline(deadline.Budgets(first_byte_s=10, stream_idle_s=60,
                                                total_s=100), clock=lambda: clock['now'])
        dl.mark_first_byte()
        clock['now'] = 95.0
        timeout, kind = dl.next_timeout()
        self.assertEqual(kind, 'total')
        self.assertEqual(timeout, 5.0)

    def test_check_total_raises(self) -> None:
        clock = {'now': 0.0}
        dl = deadline.Deadline(deadline.Budgets(first_byte_s=5, stream_idle_s=5,
                                                total_s=10), clock=lambda: clock['now'])
        clock['now'] = 11.0
        with self.assertRaises(deadline.BudgetExceeded) as ctx:
            dl.check_total()
        self.assertEqual(ctx.exception.kind, 'total')


async def _chunks(values, *, delay: float = 0.0):
    for value in values:
        if delay:
            await asyncio.sleep(delay)
        yield value


class IterateWithBudgetsTest(unittest.TestCase):
    def test_normal_stream_completes(self) -> None:
        async def run():
            dl = deadline.Deadline(deadline.Budgets())
            return [chunk async for chunk in deadline.iterate_with_budgets(
                _chunks([b'a', b'b']), dl)]
        self.assertEqual(asyncio.run(run()), [b'a', b'b'])

    def test_first_byte_timeout(self) -> None:
        async def run():
            budgets = deadline.Budgets(first_byte_s=0.05, stream_idle_s=5, total_s=5)
            dl = deadline.Deadline(budgets)
            with self.assertRaises(deadline.BudgetExceeded) as ctx:
                async for _ in deadline.iterate_with_budgets(
                        _chunks([b'a'], delay=0.2), dl):
                    pass
            return ctx.exception.kind
        self.assertEqual(asyncio.run(run()), 'first_byte')

    def test_stream_idle_timeout(self) -> None:
        async def run():
            budgets = deadline.Budgets(first_byte_s=5, stream_idle_s=0.05, total_s=5)
            dl = deadline.Deadline(budgets)
            with self.assertRaises(deadline.BudgetExceeded) as ctx:
                async for _ in deadline.iterate_with_budgets(
                        _chunks([b'a', b'b'], delay=0.2), dl):
                    pass
            return ctx.exception.kind
        self.assertEqual(asyncio.run(run()), 'stream_idle')

    def test_total_timeout(self) -> None:
        async def run():
            # total 必须 >= 其它预算（normalized 会夹紧），这里让 total 只比它们大一点，
            # 第二块之后剩余总时长不足一个 idle 窗口 → 归因到 total。
            budgets = deadline.Budgets(first_byte_s=0.2, stream_idle_s=0.2, total_s=0.25)
            dl = deadline.Deadline(budgets)
            with self.assertRaises(deadline.BudgetExceeded) as ctx:
                async for _ in deadline.iterate_with_budgets(
                        _chunks([b'a', b'b', b'c'], delay=0.12), dl):
                    pass
            return ctx.exception.kind
        self.assertEqual(asyncio.run(run()), 'total')

    def test_cancel_propagates(self) -> None:
        """客户端取消必须原样上抛（交给 close_stream 去关上游）。"""
        async def run():
            dl = deadline.Deadline(deadline.Budgets())
            iterator = deadline.iterate_with_budgets(_chunks([b'a'], delay=0.2), dl)

            async def consume():
                async for _ in iterator:
                    pass

            task = asyncio.ensure_future(consume())
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(run())


class RetryPolicyTest(unittest.TestCase):
    def test_default_is_single_attempt(self) -> None:
        policy = deadline.RetryPolicy.from_config()
        self.assertEqual(policy.max_attempts, 1)
        decision = deadline.decide_retry(policy, phase=deadline.PHASE_CONNECT, attempt=1,
                                         generated_started=False)
        self.assertFalse(decision.retry)

    def test_safe_phase_can_retry_when_enabled(self) -> None:
        policy = deadline.RetryPolicy(max_attempts=2)
        decision = deadline.decide_retry(
            policy, phase=deadline.PHASE_CONNECT, attempt=1, generated_started=False,
            error=httpx.ConnectError('connection refused'))
        self.assertTrue(decision.retry)
        self.assertEqual(decision.next_attempt, 2)
        self.assertGreater(decision.delay_s, 0)

    def test_generation_started_blocks_retry(self) -> None:
        policy = deadline.RetryPolicy(max_attempts=3)
        decision = deadline.decide_retry(
            policy, phase=deadline.PHASE_FIRST_BYTE, attempt=1, generated_started=True,
            error=ConnectionResetError('reset'))
        self.assertFalse(decision.retry)
        self.assertIn('生成已开始', decision.reason)

    def test_never_retry_phases(self) -> None:
        policy = deadline.RetryPolicy(max_attempts=3)
        for phase in (deadline.PHASE_FIRST_BYTE, deadline.PHASE_STREAMING,
                      deadline.PHASE_DONE):
            decision = deadline.decide_retry(policy, phase=phase, attempt=1,
                                             generated_started=False)
            self.assertFalse(decision.retry, phase)

    def test_non_retryable_error_blocks(self) -> None:
        policy = deadline.RetryPolicy(max_attempts=3)
        decision = deadline.decide_retry(
            policy, phase=deadline.PHASE_CONNECT, attempt=1, generated_started=False,
            error=ValueError('bad body'))
        self.assertFalse(decision.retry)
        self.assertIn('不可重试', decision.reason)

    def test_attempts_exhausted(self) -> None:
        policy = deadline.RetryPolicy(max_attempts=2)
        decision = deadline.decide_retry(
            policy, phase=deadline.PHASE_CONNECT, attempt=2, generated_started=False,
            error=ConnectionResetError('reset'))
        self.assertFalse(decision.retry)

    def test_backoff_is_bounded(self) -> None:
        policy = deadline.RetryPolicy(max_attempts=3, backoff_s=1.0, max_backoff_s=2.0,
                                      jitter_s=0.0)
        self.assertEqual(policy.delay(1), 1.0)
        self.assertEqual(policy.delay(2), 2.0)
        self.assertEqual(policy.delay(8), 2.0, '退避必须封顶，不能放大上游故障')

    def test_env_max_attempts_is_clamped(self) -> None:
        with mock.patch.dict(os.environ, {'WB_SAFE_RETRY_MAX': '99'}, clear=False):
            self.assertEqual(deadline.RetryPolicy.from_config().max_attempts, 3)
        with mock.patch.dict(os.environ, {'WB_SAFE_RETRY_MAX': 'abc'}, clear=False):
            self.assertEqual(deadline.RetryPolicy.from_config().max_attempts, 1)


class CancelClosureTest(unittest.TestCase):
    """取消预算：关闭上下游必须是有界的，且不能因为异常而跳过 client 关闭。"""

    def test_close_stream_closes_both_sides(self) -> None:
        closed: list[str] = []

        class FakeResponse:
            async def aclose(self):
                closed.append('response')

        class FakeClient:
            async def aclose(self):
                closed.append('client')

        asyncio.run(observability.close_stream(FakeResponse(), FakeClient()))
        self.assertEqual(closed, ['response', 'client'])

    def test_close_stream_swallows_errors_but_still_closes_client(self) -> None:
        closed: list[str] = []

        class FakeResponse:
            async def aclose(self):
                raise RuntimeError('boom')

        class FakeClient:
            async def aclose(self):
                closed.append('client')

        asyncio.run(observability.close_stream(FakeResponse(), FakeClient()))
        self.assertEqual(closed, ['client'])


class StreamingResponseCancelTest(unittest.TestCase):
    """生成已开始时被取消：响应对象必须被关闭（取消传到上游）。"""

    def test_cancelled_response_closes_body_iterator(self) -> None:
        closed: list[str] = []

        async def body():
            try:
                yield b'data: 1\n\n'
                await asyncio.sleep(5)
            finally:
                closed.append('generator')

        async def cleanup():
            closed.append('cleanup')

        async def run():
            response = observability.ManagedStreamingResponse(
                body(), cleanup=cleanup, media_type='text/event-stream')
            scope = {'type': 'http', 'method': 'GET', 'path': '/x', 'headers': []}

            async def receive():
                await asyncio.sleep(5)

            async def send(message):
                return None

            task = asyncio.ensure_future(response(scope, receive, send))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self.assertIn('generator', closed)
        self.assertIn('cleanup', closed)


if __name__ == '__main__':
    unittest.main()
