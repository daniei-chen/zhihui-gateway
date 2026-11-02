"""Adding an account cannot restart unrelated in-flight inference."""
import asyncio
import copy
import time
import unittest
from unittest import mock

from server.services import reload


class ReloadSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        saved = copy.deepcopy(reload._state)
        self.addCleanup(lambda: (reload._state.clear(), reload._state.update(saved)))
        reload._pending_uids.clear()
        reload._group_pending.clear()
        reload._account_reload_states.clear()
        self.addCleanup(reload._pending_uids.clear)
        self.addCleanup(reload._group_pending.clear)
        self.addCleanup(reload._account_reload_states.clear)
        for name, value in (('HOT_RELOAD_WAIT_SECONDS', .04), ('HOT_RELOAD_POLL_SECONDS', .005)):
            patch = mock.patch.object(reload, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.restart = mock.AsyncMock(return_value=(True, 'not permitted by this flow'))
        patch = mock.patch.object(reload.wb2api, 'restart_container', self.restart)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(reload.upstreamsvc, 'forward_api_key', side_effect=lambda u: u.get('test_key', ''))
        patch.start()
        self.addCleanup(patch.stop)

    async def test_unreachable_or_missing_account_never_restarts_default_engine(self):
        for response in ({'connected': False}, {'connected': True, 'accounts': []}):
            with self.subTest(response=response), mock.patch.object(
                    reload.wb2api, 'get_status', new=mock.AsyncMock(return_value=response)):
                reload._pending_uids.add('synthetic-new')
                await reload._reload_worker_loop(.01)
                self.restart.assert_not_awaited()
                self.assertFalse(reload._pending_uids)
                self.assertEqual(reload.state()['account_reload']['phase'], 'needs_attention')

    async def test_success_is_verified_without_any_restart(self):
        with mock.patch.object(reload.wb2api, 'get_status', new=mock.AsyncMock(
                return_value={'connected': True, 'accounts': [{'uid': 'synthetic-new'}]})):
            reload._pending_uids.add('synthetic-new')
            await reload._reload_worker_loop(.04)
        self.restart.assert_not_awaited()
        self.assertEqual(reload.state()['account_reload']['phase'], 'confirmed')

    async def test_slow_status_has_a_total_wait_bound(self):
        async def slow_status():
            await asyncio.sleep(2)
            return {'connected': False}
        with mock.patch.object(reload.wb2api, 'get_status', side_effect=slow_status):
            reload._pending_uids.add('synthetic-slow')
            started = time.monotonic()
            await asyncio.wait_for(reload._reload_worker_loop(.03), .3)
        self.assertLess(time.monotonic() - started, .25)
        self.restart.assert_not_awaited()

    async def test_group_container_configuration_is_not_restart_permission(self):
        upstream = {'id': 17, 'base_url': 'http://127.0.0.1:17999',
                    'container': 'synthetic-group', 'test_key': 'synthetic'}
        with mock.patch.object(reload.wb2api, 'get_status', new=mock.AsyncMock(return_value={'connected': False})):
            self.assertTrue(reload.request_reload_or_restart('synthetic-new', upstream=upstream))
            tasks = list(reload._group_workers.values())
            await asyncio.gather(*tasks)
        self.restart.assert_not_awaited()
        self.assertEqual(reload.state()['account_reload']['phase'], 'needs_attention')

    async def test_status_exception_is_not_restart_or_background_task_failure(self):
        with mock.patch.object(reload.wb2api, 'get_status', new=mock.AsyncMock(side_effect=RuntimeError('synthetic'))):
            reload._pending_uids.add('synthetic-new')
            await reload._reload_worker_loop(.01)
        self.restart.assert_not_awaited()
        self.assertEqual(reload.state()['account_reload']['reason'], 'upstream_unavailable')

    async def test_cancellation_records_uncertainty_and_does_not_restart(self):
        entered = asyncio.Event()
        async def wait_status():
            entered.set()
            await asyncio.Event().wait()
        with mock.patch.object(reload.wb2api, 'get_status', side_effect=wait_status):
            reload._pending_uids.add('synthetic-new')
            task = asyncio.create_task(reload._reload_worker_loop(30))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.restart.assert_not_awaited()
        self.assertFalse(reload._pending_uids)
        self.assertEqual(reload.state()['account_reload']['reason'], 'observation_cancelled')

    async def test_same_url_with_different_credentials_never_shares_account_observation(self):
        entered = asyncio.Event()
        scopes = set()
        async def status(*, base_url, api_key):
            scopes.add(api_key)
            if len(scopes) == 2:
                entered.set()
            await asyncio.wait_for(entered.wait(), 1)
            return {'connected': True, 'accounts': [{'uid': 'synthetic-' + api_key}]}
        with mock.patch.object(reload.wb2api, 'get_status', side_effect=status):
            for value in ('a', 'b'):
                upstream = {'id': 17, 'base_url': 'http://127.0.0.1:17999', 'test_key': value}
                self.assertTrue(reload.request_reload_or_restart('synthetic-' + value, upstream=upstream))
            await asyncio.gather(*list(reload._group_workers.values()))
        self.restart.assert_not_awaited()
        self.assertEqual(scopes, {'a', 'b'})
        states = reload.state()['account_reload_scopes']
        self.assertEqual(len(states), 2)
        self.assertTrue(all(s['phase'] == 'confirmed' for s in states))
        self.assertFalse(reload._group_pending)

    async def test_public_state_cannot_mutate_internal_observation(self):
        reload._record_account_reload('default', None, 'needs_attention', 'upstream_unavailable', {'synthetic'})
        exposed = reload.state()
        exposed['account_reload']['reason'] = 'changed'
        exposed['account_reload_scopes'][0]['reason'] = 'changed'
        self.assertEqual(reload.state()['account_reload']['reason'], 'upstream_unavailable')

    async def test_inherited_credential_change_cannot_orphan_or_rebind_a_waiter(self):
        upstream = {'id': 17, 'base_url': 'http://127.0.0.1:17999'}
        with mock.patch.object(reload.upstreamsvc, 'forward_api_key', side_effect=['first', 'changed']) as key, \
                mock.patch.object(reload.wb2api, 'get_status', new=mock.AsyncMock(
                    return_value={'connected': True, 'accounts': [{'uid': 'synthetic-new'}]})) as status:
            reload.request_reload_or_restart('synthetic-new', upstream=upstream)
            await asyncio.gather(*list(reload._group_workers.values()))
        self.assertEqual(key.call_count, 1)
        self.assertEqual(status.call_args.kwargs['api_key'], 'first')
        self.assertFalse(reload._group_pending)


if __name__ == '__main__':
    unittest.main()
