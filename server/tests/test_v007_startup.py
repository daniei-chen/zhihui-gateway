import asyncio
import threading
import unittest
from unittest.mock import patch

from server import operational


class StartupReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_sample_runs_off_the_event_loop(self):
        loop_thread = threading.get_ident()
        observed = []

        def probe():
            observed.append(threading.get_ident())
            return {'ok': True}

        with patch.object(operational, '_cached_readiness', None), \
                patch.object(operational, 'readiness', side_effect=probe), \
                patch('server.drain.is_draining', return_value=False):
            self.assertTrue(await operational.initialize_readiness())
            self.assertTrue(operational.public_readiness())
            self.assertNotEqual(observed, [loop_thread])

    async def test_old_green_is_not_reused_while_startup_probe_is_pending(self):
        entered, release = threading.Event(), threading.Event()

        def probe():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('synthetic timeout')
            return {'ok': True}

        with patch.object(operational, '_cached_readiness', {'at': operational.time.monotonic(), 'ok': True}), \
                patch.object(operational, 'readiness', side_effect=probe), \
                patch('server.drain.is_draining', return_value=False):
            task = asyncio.create_task(operational.initialize_readiness())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertFalse(operational.public_readiness())
            finally:
                release.set()
                await task

    async def test_unhealthy_sample_stays_unready(self):
        with patch.object(operational, '_cached_readiness', None), \
                patch.object(operational, 'readiness', return_value={'ok': False}), \
                patch('server.drain.is_draining', return_value=False):
            self.assertFalse(await operational.initialize_readiness())
            self.assertFalse(operational.public_readiness())

    async def test_sample_failure_stays_unknown(self):
        with patch.object(operational, '_cached_readiness', None), \
                patch.object(operational, 'readiness', side_effect=RuntimeError('synthetic')), \
                self.assertLogs(operational.log, level='ERROR'):
            self.assertFalse(await operational.initialize_readiness())
            self.assertIsNone(operational._cached_readiness)

    async def test_healthy_probe_does_not_override_an_admission_drain(self):
        with patch.object(operational, '_cached_readiness', None), \
                patch.object(operational, 'readiness', return_value={'ok': True}), \
                patch('server.drain.is_draining', return_value=True):
            self.assertTrue(await operational.initialize_readiness())
            self.assertFalse(operational.public_readiness())

    async def test_cancellation_retains_blocking_work_ownership(self):
        entered, release = threading.Event(), threading.Event()

        def probe():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('synthetic timeout')
            return {'ok': True}

        with patch.object(operational, '_cached_readiness', None), \
                patch.object(operational, 'readiness', side_effect=probe):
            task = asyncio.create_task(operational.initialize_readiness())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertIsNone(operational._cached_readiness)
