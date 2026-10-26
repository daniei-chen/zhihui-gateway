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
