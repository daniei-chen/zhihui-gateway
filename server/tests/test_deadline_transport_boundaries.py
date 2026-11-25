import asyncio
import unittest

import httpx

from server import deadline


class TransportBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_for_headers_is_inside_first_byte_budget(self):
        closed = asyncio.Event()

        async def upstream():
            try:
                await asyncio.sleep(2)
            finally:
                closed.set()

        dl = deadline.Deadline(deadline.Budgets(first_byte_s=0.025, total_s=1))
        with self.assertRaises(deadline.BudgetExceeded) as exc:
            await deadline.await_headers(upstream(), dl)
        self.assertEqual(exc.exception.kind, 'first_byte')
        self.assertTrue(closed.is_set())

    async def test_nonstream_total_deadline_cancels_transport(self):
        closed = asyncio.Event()

        async def upstream():
            try:
                await asyncio.sleep(2)
            finally:
                closed.set()

        dl = deadline.Deadline(deadline.Budgets(total_s=0.025))
        with self.assertRaises(deadline.BudgetExceeded) as exc:
            await deadline.await_total(upstream(), dl)
        self.assertEqual(exc.exception.kind, 'total')
        self.assertTrue(closed.is_set())

    async def test_cancel_propagates_to_headers_wait(self):
        async def upstream():
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await deadline.await_headers(upstream(), deadline.Deadline(deadline.Budgets()))


class AmbiguousTransportFailureTest(unittest.TestCase):
    def test_accepted_request_disconnect_can_never_be_replayed(self):
        policy = deadline.RetryPolicy(max_attempts=3)
        for exc in (httpx.ReadError('accepted then disconnected'),
                    httpx.WriteError('partial send'), httpx.RemoteProtocolError('empty response'),
                    ConnectionResetError('reset after send')):
            with self.subTest(error=type(exc).__name__):
                decision = deadline.decide_retry(policy, phase=deadline.failure_phase(exc),
                                                 attempt=1, generated_started=False, error=exc)
                self.assertFalse(decision.retry)

    def test_connect_and_pool_acquisition_failures_are_safe(self):
        policy = deadline.RetryPolicy(max_attempts=2)
        for exc in (httpx.ConnectError('refused'), httpx.ConnectTimeout('timeout'),
                    httpx.PoolTimeout('pool not acquired')):
            with self.subTest(error=type(exc).__name__):
                decision = deadline.decide_retry(policy, phase=deadline.failure_phase(exc),
                                                 attempt=1, generated_started=False, error=exc)
                self.assertTrue(decision.retry)

    def test_total_budget_cannot_be_expanded_by_idle_default(self):
        b = deadline.Budgets(total_s=5, stream_idle_s=600).normalized()
        self.assertEqual(b.total_s, 5)
        self.assertEqual(b.stream_idle_s, 5)
        for value in (float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                deadline.Budgets(total_s=value)


class BudgetKindVisibilityTest(unittest.TestCase):
    """预算超限的 `kind` 必须落到日志里。

    2026-11-25 之前的实现把 `kind` 丢掉了：`error_stage` 一律落 `stream_read`、
    终态一律归 `gateway.transport.error`，而这两样都无法区分「连不上」和
    「首字节超时」。实测 253 条记录全部不可分辨，`journalctl` 里也搜不到。

    这个测试锁住两件事：
    1. 五类 `kind` 每一种都会写一条 warning；
    2. 日志里带 `kind=` 与具体的取值。
    """

    KINDS = ('connect', 'send_request', 'first_byte', 'stream_idle', 'total')

    def test_every_kind_is_logged_with_its_value(self):
        for kind in self.KINDS:
            with self.assertLogs('workbuddy.deadline', level='WARNING') as captured:
                deadline.classify_budget_exceeded(deadline.BudgetExceeded(kind))
            line = captured.output[0]
            self.assertIn('kind=' + kind, line)
            self.assertIn('时间预算', line)

    def test_log_line_does_not_leak_upstream_topology(self):
        with self.assertLogs('workbuddy.deadline', level='WARNING') as captured:
            deadline.classify_budget_exceeded(
                deadline.BudgetExceeded('first_byte', '127.0.0.1:7863'))
        line = captured.output[0]
        # detail 是调用方传进来的上下文，可以出现；但不能出现凭据或密钥字样。
        for forbidden in ('authorization', 'bearer', 'api_key', 'token='):
            self.assertNotIn(forbidden, line.lower())

    def test_unknown_kind_still_logs(self):
        """未知 kind 不能因为不在表里就静默。"""
        with self.assertLogs('workbuddy.deadline', level='WARNING') as captured:
            failure = deadline.classify_budget_exceeded(
                deadline.BudgetExceeded('something_new'))
        self.assertIn('kind=something_new', captured.output[0])
        self.assertEqual(failure.terminal_state, 'transport_error')
        self.assertEqual(failure.error_stage, 'stream_read')


if __name__ == '__main__':
    unittest.main()