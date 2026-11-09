"""Never manufacture an empty Trae pool after an unconfirmed read."""
import asyncio
import unittest
from unittest import mock

from server.qtgcore.adapters.trae2 import Trae2Adapter
from server.qtgcore.config import Settings
from server.qtgcore.errors import UpstreamError


class TraePoolSafetyTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        return Trae2Adapter(Settings())

    async def test_invalid_read_never_dispatches_pool_set(self):
        cases = [{}, {'ok': False}, {'ok': 'true', 'data': {'enabled_uids': []}},
                 {'ok': True}, {'ok': True, 'data': {}},
                 {'ok': True, 'data': {'enabled_uids': 'not-a-list'}},
                 {'ok': True, 'data': {'enabled_uids': ['good', 3]}},
                 {'ok': True, 'data': {'enabled_uids': ['good', 'good']}}]
        for result in cases:
            with self.subTest(result=result):
                adapter = self.adapter()
                adapter._cmd = mock.AsyncMock(return_value=result)
                with self.assertRaises(UpstreamError):
                    await adapter.set_account('new', True)
                self.assertEqual([call.args[0] for call in adapter._cmd.call_args_list], ['pool_list'])

    async def test_failed_required_read_never_writes(self):
        adapter = self.adapter()
        adapter._cmd = mock.AsyncMock(side_effect=UpstreamError('synthetic read failure'))
        with self.assertRaises(UpstreamError):
            await adapter.set_account('new', True)
        self.assertEqual(adapter._cmd.call_count, 1)

    async def test_concurrent_manager_mutations_do_not_overwrite_each_other(self):
        adapter = self.adapter()
        pool = ['keep', 'alpha', 'beta']
        writes = []
        async def command(name, body=None, **kwargs):
            nonlocal pool
            if name == 'pool_list':
                snapshot = list(pool)
                await asyncio.sleep(.01)
                return {'ok': True, 'data': {'enabled_uids': snapshot}}
            if name == 'pool_set':
                self.assertIs(kwargs.get('retry'), False)
                pool = list(body['uids'])
                writes.append(list(pool))
                return {'ok': True}
            raise AssertionError('unexpected command')
        adapter._cmd = command
        results = await asyncio.gather(adapter.set_account('alpha', False), adapter.set_account('beta', False))
        self.assertEqual(pool, ['keep'])
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(row['ok'] is True for row in results))

    async def test_ack_without_confirmed_readback_is_not_reported_as_success(self):
        adapter = self.adapter()
        adapter._cmd = mock.AsyncMock(side_effect=[
            {'ok': True, 'data': {'enabled_uids': ['keep']}}, {'ok': True},
            {'ok': True, 'data': {'enabled_uids': ['unexpected-other-writer']}}])
        result = await adapter.set_account('new', True)
        self.assertIs(result['ok'], False)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual([call.args[0] for call in adapter._cmd.call_args_list], ['pool_list', 'pool_set', 'pool_list'])

    async def test_ambiguous_write_is_never_repeated(self):
        adapter = self.adapter()
        adapter._cmd = mock.AsyncMock(side_effect=[
            {'ok': True, 'data': {'enabled_uids': ['keep']}}, UpstreamError('synthetic ambiguous write')])
        with self.assertRaises(UpstreamError):
            await adapter.set_account('new', True)
        self.assertEqual([call.args[0] for call in adapter._cmd.call_args_list], ['pool_list', 'pool_set'])

    async def test_exact_already_applied_change_requires_no_write(self):
        adapter = self.adapter()
        adapter._cmd = mock.AsyncMock(return_value={'ok': True, 'data': {'enabled_uids': ['keep']}})
        result = await adapter.set_account('keep', True)
        self.assertIs(result['ok'], True)
        self.assertEqual(adapter._cmd.call_count, 1)

    async def test_client_cancellation_does_not_release_the_local_write_owner_early(self):
        adapter = self.adapter()
        entered, finish = asyncio.Event(), asyncio.Event()
        pool = ['alpha', 'beta']
        writes = 0
        async def command(name, body=None, **kwargs):
            nonlocal pool, writes
            if name == 'pool_list':
                return {'ok': True, 'data': {'enabled_uids': list(pool)}}
            writes += 1
            if writes == 1:
                entered.set()
                await finish.wait()
            pool = list(body['uids'])
            return {'ok': True}
        adapter._cmd = command
        first = asyncio.create_task(adapter.set_account('alpha', False))
        await asyncio.wait_for(entered.wait(), 1)
        first.cancel()
        second = asyncio.create_task(adapter.set_account('beta', False))
        await asyncio.sleep(.01)
        self.assertFalse(first.done())
        self.assertEqual(writes, 1)
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIs((await second)['ok'], True)
        self.assertEqual(pool, [])

    async def test_cancelled_waiter_or_read_never_becomes_a_new_pool_write(self):
        for phase in ('waiting', 'reading'):
            with self.subTest(phase=phase):
                adapter = self.adapter()
                entered = asyncio.Event()
                async def read(name, body=None, **kwargs):
                    self.assertEqual(name, 'pool_list')
                    entered.set()
                    await asyncio.Event().wait()
                adapter._cmd = mock.AsyncMock(side_effect=read)
                if phase == 'waiting':
                    await adapter._pool_write_lock.acquire()
                task = asyncio.create_task(adapter.set_account('new', True))
                if phase == 'reading':
                    await asyncio.wait_for(entered.wait(), 1)
                else:
                    await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                if phase == 'waiting':
                    adapter._pool_write_lock.release()
                self.assertTrue(all(call.args[0] == 'pool_list' for call in adapter._cmd.call_args_list))
                self.assertFalse(adapter._pool_write_lock.locked())


if __name__ == '__main__':
    unittest.main()
