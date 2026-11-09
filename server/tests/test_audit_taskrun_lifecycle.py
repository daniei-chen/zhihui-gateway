"""Task lifetime tests use owned fake processes, never host PID/proc state."""
import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from server.services import taskrun


class _Output:
    def __init__(self, owner, mode):
        self.owner = owner
        self.mode = mode
        self.lines = 0
        self.read_started = asyncio.Event()

    async def readline(self):
        self.read_started.set()
        if self.mode == 'continuous':
            self.lines += 1
            await asyncio.sleep(0)
            return b'synthetic progress\n'
        if self.mode == 'eof':
            return b''
        if self.mode == 'broken':
            raise ValueError('synthetic oversized line')
        await asyncio.Event().wait()

    async def read(self, limit):
        self.owner.events.append('pipe-drained')
        return b''


class _Process:
    def __init__(self, mode='silent', *, automatic_reap=True):
        self.stdout = _Output(self, mode)
        self.returncode = None
        self.events = []
        self.killed = asyncio.Event()
        self.wait_started = asyncio.Event()
        self.allow_reap = asyncio.Event()
        self.automatic_reap = automatic_reap

    def kill(self):
        self.events.append('killed')
        self.killed.set()
        if self.automatic_reap:
            self.allow_reap.set()

    async def wait(self):
        self.wait_started.set()
        await self.allow_reap.wait()
        self.returncode = -9 if self.killed.is_set() else 0
        self.events.append('reaped')
        return self.returncode


class TaskRunLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = {'running': True, 'mode': 'preview', 'target': 'ALL',
                      'started_at': 1, 'finished_at': 0, 'exit_code': None,
                      'timed_out': False, 'error': '', 'lines': []}
        self.process = _Process()
        self.history_events = []
        self.spawn_started = asyncio.Event()

        async def spawn(*args, **kwargs):
            self.spawn_started.set()
            return self.process

        for patcher in (
            mock.patch.object(taskrun, '_state', self.state),
            mock.patch.object(taskrun, '_task', None),
            mock.patch.object(taskrun, '_script_path', return_value=Path('/synthetic/task_runner.py')),
            mock.patch.object(taskrun, 'os_environ', return_value={}),
            mock.patch.object(taskrun, '_record_history', side_effect=lambda *args: self.history_events.append('history')),
            mock.patch.object(taskrun.asyncio, 'create_subprocess_exec', side_effect=spawn),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_task(self):
        task = asyncio.create_task(taskrun._run(['synthetic-no-execution'], 'preview', 'ALL'))
        taskrun._task = task
        return task

    async def test_continuous_output_cannot_extend_total_deadline(self):
        self.process = _Process('continuous')
        with mock.patch.object(taskrun, 'MAX_TOTAL_SECONDS', 0.04):
            await asyncio.wait_for(self.run_task(), 2)
        self.assertGreater(self.process.stdout.lines, 2)
        self.assertTrue(self.state['timed_out'])
        self.assertEqual(self.process.events[-1], 'reaped')
        self.assertFalse(self.state['running'])
        self.assertEqual(self.history_events, ['history'])

    async def test_eof_does_not_remove_the_process_lifetime_deadline(self):
        self.process = _Process('eof')
        with mock.patch.object(taskrun, 'MAX_TOTAL_SECONDS', 0.03):
            await asyncio.wait_for(self.run_task(), 2)
        self.assertTrue(self.state['timed_out'])
        self.assertTrue(self.process.killed.is_set())
        self.assertEqual(self.state['exit_code'], -9)

    async def test_read_failure_still_kills_and_reaps(self):
        self.process = _Process('broken')
        await asyncio.wait_for(self.run_task(), 2)
        self.assertIn('oversized line', self.state['error'])
        self.assertEqual(self.process.events, ['killed', 'pipe-drained', 'reaped'])
        self.assertFalse(self.state['running'])

    async def test_repeated_cancellation_joins_child_before_another_run(self):
        self.process = _Process(automatic_reap=False)
        run = self.run_task()
        await asyncio.wait_for(self.process.stdout.read_started.wait(), 2)
        stopping = asyncio.create_task(taskrun.stop())
        try:
            await asyncio.wait_for(self.process.wait_started.wait(), 2)
            stopping.cancel()
            run.cancel()
            await asyncio.sleep(0)
            stopping.cancel()
            run.cancel()
            await asyncio.sleep(0)
            self.assertFalse(run.done())
            self.assertFalse(stopping.done())
            self.assertTrue(self.state['running'])
            self.assertEqual(self.history_events, [])
            self.assertFalse(taskrun._launch([], 'preview', 'ALL', asyncio.get_running_loop())[0])
        finally:
            self.process.allow_reap.set()
            for task in (run, stopping):
                try:
                    await asyncio.wait_for(task, 2)
                except asyncio.CancelledError:
                    pass
        self.assertEqual(self.process.events[-1], 'reaped')
        self.assertEqual(self.history_events, ['history'])
        self.assertFalse(self.state['running'])
        self.assertEqual(self.state['error'], '任务已取消')

    async def test_cancellation_during_spawn_still_reaps_late_child(self):
        allow_spawn = asyncio.Event()

        async def spawn(*args, **kwargs):
            self.spawn_started.set()
            await allow_spawn.wait()
            return self.process

        with mock.patch.object(taskrun.asyncio, 'create_subprocess_exec', side_effect=spawn):
            run = self.run_task()
            await asyncio.wait_for(self.spawn_started.wait(), 2)
            run.cancel()
            await asyncio.sleep(0)
            run.cancel()
            await asyncio.sleep(0)
            self.assertFalse(run.done())
            self.assertTrue(self.state['running'])
            allow_spawn.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(run, 2)
        self.assertTrue(self.process.killed.is_set())
        self.assertEqual(self.process.events[-1], 'reaped')
        self.assertFalse(self.state['running'])

    async def test_inherited_stdout_without_eof_is_bounded_and_closed_before_reap(self):
        self.process = _Process(automatic_reap=False)
        drain_started = asyncio.Event()

        async def never_eof(limit):
            drain_started.set()
            await asyncio.Event().wait()

        def close_reader():
            self.process.events.append('pipe-closed')
            self.process.allow_reap.set()

        self.process.stdout.read = never_eof
        self.process.stdout._transport = SimpleNamespace(close=close_reader)
        with mock.patch.object(taskrun, 'PIPE_DRAIN_TIMEOUT_SECONDS', 0.03):
            run = self.run_task()
            await asyncio.wait_for(self.process.stdout.read_started.wait(), 2)
            run.cancel()
            await asyncio.wait_for(drain_started.wait(), 2)
            run.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.state['running'])
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(run, 2)
        self.assertEqual(self.process.events, ['killed', 'pipe-closed', 'reaped'])
        self.assertEqual(self.history_events, ['history'])
        self.assertFalse(self.state['running'])

    async def test_exited_child_with_inherited_pipe_also_closes_reader(self):
        self.process = _Process(automatic_reap=False)
        self.process.returncode = 0

        async def never_eof(limit):
            await asyncio.Event().wait()

        def close_reader():
            self.process.events.append('pipe-closed')
            self.process.allow_reap.set()

        self.process.stdout.read = never_eof
        self.process.stdout._transport = SimpleNamespace(close=close_reader)
        with mock.patch.object(taskrun, 'PIPE_DRAIN_TIMEOUT_SECONDS', 0.02):
            run = self.run_task()
            await asyncio.wait_for(self.process.stdout.read_started.wait(), 2)
            run.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(run, 2)
        self.assertEqual(self.process.events, ['pipe-closed', 'reaped'])
        self.assertFalse(self.state['running'])

    async def test_preparation_failure_releases_running_state(self):
        with mock.patch.object(taskrun, '_script_path', side_effect=OSError('synthetic mount failure')):
            await asyncio.wait_for(self.run_task(), 2)
        self.assertIn('mount failure', self.state['error'])
        self.assertFalse(self.spawn_started.is_set())
        self.assertFalse(self.state['running'])

    async def test_cancelled_start_joins_preparation_without_launching(self):
        entered, release = threading.Event(), threading.Event()

        def prepare(*args):
            entered.set()
            release.wait(5)
            return True, ['synthetic']

        with mock.patch.object(taskrun, '_prepare', side_effect=prepare), mock.patch.object(taskrun, '_launch') as launch:
            start = asyncio.create_task(taskrun.start_async('preview'))
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                start.cancel()
                await asyncio.sleep(0)
                start.cancel()
                await asyncio.sleep(0)
                self.assertFalse(start.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(start, 2)
            launch.assert_not_called()

    async def test_schedule_read_does_not_block_loop_and_is_joined_on_cancel(self):
        entered, release = threading.Event(), threading.Event()
        thread_ids = []

        def read():
            thread_ids.append(threading.get_ident())
            entered.set()
            release.wait(5)
            return {'enabled': False, 'hours': [10]}

        with mock.patch.object(taskrun, 'get_schedule', side_effect=read):
            worker = asyncio.create_task(taskrun._claim_loop())
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                self.assertNotEqual(thread_ids, [threading.get_ident()])
                worker.cancel()
                await asyncio.sleep(0)
                self.assertFalse(worker.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 2)


if __name__ == '__main__':
    unittest.main()
