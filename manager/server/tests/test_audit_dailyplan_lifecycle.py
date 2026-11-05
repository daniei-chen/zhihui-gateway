"""Lease ownership through real scheduler/taskrun coordination, no subprocesses."""
import asyncio
import threading
import unittest
from unittest import mock

from server.services import dailyplan, taskrun


class DailyPlanLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.entered = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.allow_cleanup.set()
        self.state = {'running': False, 'mode': '', 'target': '', 'started_at': 0,
                      'finished_at': 0, 'exit_code': None, 'timed_out': False,
                      'error': '', 'lines': []}
        self.storage_threads = []

        def claim(task):
            self.storage_threads.append(threading.get_ident())
            self.events.append('claimed')
            return True, 77

        def mark(*args):
            self.storage_threads.append(threading.get_ident())
            self.events.append('marked')
            return True

        def release(*args):
            self.storage_threads.append(threading.get_ident())
            self.events.append('released')
            return True

        async def work(*args):
            self.events.append('script_started')
            self.entered.set()
            try:
                await self.allow_finish.wait()
                self.events.append('script_effect')
            finally:
                self.cleanup_entered.set()
                await self.allow_cleanup.wait()
                self.events.append('script_finished')
                self.state['running'] = False

        for patcher in (
            mock.patch.object(dailyplan, 'claim_task', side_effect=claim),
            mock.patch.object(dailyplan, 'mark_ran_fenced', side_effect=mark),
            mock.patch.object(dailyplan.tasklease, 'release', side_effect=release),
            mock.patch.object(dailyplan.tasklease, 'renew', return_value=True),
            mock.patch.object(dailyplan, 'LEASE_TTL', 300),
            mock.patch.object(taskrun, '_prepare', return_value=(True, ['synthetic-no-execution'])),
            mock.patch.object(taskrun, '_run', side_effect=work),
            mock.patch.object(taskrun, '_state', self.state),
            mock.patch.object(taskrun, '_task', None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_claimed(self):
        return asyncio.create_task(dailyplan._run_claimed('full', {'full': {'hour': 0}}))

    async def wait_event(self, event):
        await asyncio.wait_for(event.wait(), 4)

    async def test_scheduled_lease_covers_actual_background_completion(self):
        run = self.run_claimed()
        await self.wait_event(self.entered)
        self.assertFalse(run.done())
        self.assertNotIn('released', self.events)
        self.allow_finish.set()
        self.assertTrue(await asyncio.wait_for(run, 4))
        self.assertLess(self.events.index('script_finished'), self.events.index('marked'))
        self.assertLess(self.events.index('marked'), self.events.index('released'))
        self.assertTrue(all(tid != threading.get_ident() for tid in self.storage_threads))

    async def test_renewal_rejection_cancels_and_joins_owned_script(self):
        self.allow_cleanup.clear()
        with mock.patch.object(dailyplan, 'LEASE_TTL', 2), \
                mock.patch.object(dailyplan.tasklease, 'renew', return_value=False):
            run = self.run_claimed()
            await self.wait_event(self.cleanup_entered)
            self.assertFalse(run.done())
            self.assertNotIn('released', self.events)
            self.allow_cleanup.set()
            self.assertTrue(await asyncio.wait_for(run, 4))
        self.assertNotIn('script_effect', self.events)
        self.assertLess(self.events.index('script_finished'), self.events.index('released'))

    async def test_renewal_exception_stops_work_instead_of_silently_retrying(self):
        with mock.patch.object(dailyplan, 'LEASE_TTL', 2), \
                mock.patch.object(dailyplan.tasklease, 'renew', side_effect=OSError('synthetic DB busy')):
            self.assertTrue(await asyncio.wait_for(self.run_claimed(), 4))
        self.assertNotIn('script_effect', self.events)
        self.assertIn('script_finished', self.events)
        self.assertIn('released', self.events)

    async def test_expiry_stops_work_while_renewal_thread_is_still_blocked(self):
        entered, release = threading.Event(), threading.Event()

        def renew(*args):
            self.storage_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test failed to release renewal')
            self.events.append('renewal_finished')
            return True

        with mock.patch.object(dailyplan, 'LEASE_TTL', 2), \
                mock.patch.object(dailyplan.tasklease, 'renew', side_effect=renew):
            run = self.run_claimed()
            try:
                await self.wait_event(self.cleanup_entered)
                self.assertTrue(entered.is_set())
                self.assertFalse(run.done())
                self.assertNotIn('released', self.events)
                self.assertNotIn('script_effect', self.events)
            finally:
                release.set()
                await asyncio.wait_for(run, 4)
        self.assertLess(self.events.index('renewal_finished'), self.events.index('released'))
        self.assertTrue(all(tid != threading.get_ident() for tid in self.storage_threads))

    async def test_cancel_during_claim_releases_late_acquired_fence(self):
        entered, release = threading.Event(), threading.Event()

        def claim(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test failed to release claim')
            self.events.append('late_claimed')
            return True, 77

        with mock.patch.object(dailyplan, 'claim_task', side_effect=claim):
            run = self.run_claimed()
            try:
                for _ in range(300):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                run.cancel()
                await asyncio.sleep(0)
                run.cancel()
                await asyncio.sleep(0)
                self.assertFalse(run.done())
                self.assertNotIn('released', self.events)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(run, 4)
        self.assertEqual(self.events, ['late_claimed', 'released'])
        self.assertIsNone(taskrun._task)

    async def test_repeated_cancellation_cannot_release_before_script_cleanup(self):
        self.allow_cleanup.clear()
        run = self.run_claimed()
        await self.wait_event(self.entered)
        run.cancel()
        await self.wait_event(self.cleanup_entered)
        run.cancel()
        await asyncio.sleep(0)
        run.cancel()
        await asyncio.sleep(0)
        self.assertFalse(run.done())
        self.assertNotIn('released', self.events)
        self.allow_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run, 4)
        self.assertLess(self.events.index('script_finished'), self.events.index('released'))
        self.assertNotIn('script_effect', self.events)

    async def test_cancelled_waiter_never_cancels_a_later_run(self):
        self.allow_cleanup.clear()
        waiting = asyncio.create_task(taskrun.run_and_wait('full'))
        await self.wait_event(self.entered)
        old = taskrun._task
        waiting.cancel()
        await self.wait_event(self.cleanup_entered)
        newer = []

        def start_later(_):
            new = asyncio.create_task(asyncio.Event().wait())
            newer.append(new)
            taskrun._task = new
            self.state['running'] = True

        old.add_done_callback(start_later)
        self.allow_cleanup.set()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 4)
            self.assertEqual(len(newer), 1)
            self.assertIs(taskrun._task, newer[0])
            self.assertFalse(newer[0].done())
            self.assertTrue(self.state['running'])
        finally:
            for new in newer:
                new.cancel()
            await asyncio.gather(*newer, return_exceptions=True)

    async def test_refused_start_does_not_own_the_existing_job(self):
        existing = asyncio.create_task(asyncio.Event().wait())
        taskrun._task = existing
        try:
            with mock.patch.object(taskrun, '_prepare', return_value=(False, 'busy')):
                self.assertEqual(await taskrun.run_and_wait('full'), (False, 'busy'))
            self.assertFalse(existing.done())
        finally:
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)

    async def test_manual_run_still_returns_as_soon_as_script_is_started(self):
        ok, _ = await dailyplan.run_task('claim')
        self.assertTrue(ok)
        await self.wait_event(self.entered)
        self.assertFalse(taskrun._task.done())
        self.allow_finish.set()
        await asyncio.wait_for(taskrun._task, 4)

    async def test_exhausted_claim_budget_never_dispatches_work(self):
        with mock.patch.object(dailyplan, 'LEASE_TTL', 1):
            self.assertFalse(await self.run_claimed())
        self.assertEqual(self.events, ['claimed', 'released'])
        self.assertIsNone(taskrun._task)

    async def test_async_scheduler_close_joins_lease_cleanup(self):
        self.allow_cleanup.clear()
        run = self.run_claimed()
        with mock.patch.object(dailyplan, '_loop_task', run):
            await self.wait_event(self.entered)
            closing = asyncio.create_task(dailyplan.close_scheduler())
            await self.wait_event(self.cleanup_entered)
            self.assertFalse(closing.done())
            self.assertNotIn('released', self.events)
            self.allow_cleanup.set()
            await asyncio.wait_for(closing, 4)
        self.assertTrue(run.cancelled())
        self.assertIn('released', self.events)

    async def test_repeated_cancel_of_close_still_joins_the_captured_scheduler(self):
        with mock.patch.object(dailyplan, '_loop_task', None):
            await dailyplan.close_scheduler()
        self.allow_cleanup.clear()
        run = self.run_claimed()
        with mock.patch.object(dailyplan, '_loop_task', run):
            await self.wait_event(self.entered)
            closing = asyncio.create_task(dailyplan.close_scheduler())
            await self.wait_event(self.cleanup_entered)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertNotIn('released', self.events)
            self.allow_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 4)
        self.assertTrue(run.cancelled())
        self.assertIn('released', self.events)


if __name__ == '__main__':
    unittest.main()
