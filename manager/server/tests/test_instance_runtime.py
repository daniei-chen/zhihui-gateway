import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import config, db, drain, instancelease, tasklease
from server import instance_runtime


class TestInstanceRuntime(unittest.IsolatedAsyncioTestCase):
    """Independent temporary SQLite DB via real db.connect()/v8 migrations."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = config.DB_PATH
        self.original_data = getattr(config, 'DATA_DIR', None)
        instancelease.reset_identity()
        tasklease.reset_identity()
        drain.reset_for_tests()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = Path(self.tmp.name) / 'instance-lease.db'
        db._conn = None
        self.addCleanup(self._cleanup)
        db.connect()

    def _cleanup(self) -> None:
        drain.reset_for_tests()
        instancelease.reset_identity()
        tasklease.reset_identity()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.original_db
        if self.original_data is not None:
            config.DATA_DIR = self.original_data
        self.tmp.cleanup()

    async def test_start_renews_and_close_is_idempotent(self) -> None:
        real_heartbeat = instancelease.heartbeat
        renewed = asyncio.Event()

        def observing_heartbeat(*args, **kwargs):
            result = real_heartbeat(*args, **kwargs)
            renewed.set()
            return result

        with patch.object(
            instancelease,
            'heartbeat',
            wraps=real_heartbeat,
            side_effect=observing_heartbeat,
        ) as heartbeat_mock:
            runtime = instance_runtime.InstanceRuntime(
                heartbeat_sec=0.001, ttl_sec=30)
            self.addAsyncCleanup(runtime.close)
            await runtime.start()
            await asyncio.wait_for(renewed.wait(), timeout=5)
            self.assertGreaterEqual(heartbeat_mock.call_count, 1)
            fence_before = runtime.fence
            await runtime.close()

        instance_id = runtime.identity['instance_id']
        row = instancelease.describe(instance_id)
        self.assertIsNotNone(row)
        self.assertEqual(row['role'], 'stopped')
        self.assertFalse(row['owner'])
        self.assertEqual(int(row['lease_until'] or 0), 0)
        self.assertEqual(int(row['fence']), int(fence_before))

        before_second_close = dict(row)
        await runtime.close()
        after_second_close = instancelease.describe(instance_id)
        self.assertEqual(after_second_close['role'],
                         before_second_close['role'])
        self.assertEqual(after_second_close['owner'],
                         before_second_close['owner'])
        self.assertEqual(after_second_close['lease_until'],
                         before_second_close['lease_until'])
        self.assertEqual(after_second_close['fence'],
                         before_second_close['fence'])
        self.assertTrue(runtime.task.done())

    async def test_rejected_heartbeat_stops_runtime_without_reclaim(self) -> None:
        with patch.object(
            instancelease,
            'claim',
            wraps=instancelease.claim,
        ) as claim_mock, patch.object(
            instancelease,
            'heartbeat',
            return_value=False,
        ):
            runtime = instance_runtime.InstanceRuntime(
                heartbeat_sec=0.001, ttl_sec=30)
            self.addAsyncCleanup(runtime.close)
            await runtime.start()
            await asyncio.wait_for(runtime.task, timeout=5)
            await asyncio.sleep(0.01)

            self.assertTrue(runtime.lost)
            self.assertEqual(runtime.failure_reason, 'ownership_rejected')
            self.assertTrue(drain.is_draining())
            self.assertFalse(drain.resume())
            self.assertEqual(claim_mock.call_count, 1)

            await runtime.close()
            self.assertEqual(claim_mock.call_count, 1)
            self.assertTrue(runtime.task.done())
            self.assertFalse(runtime.status()['heartbeat_running'])

    async def test_expired_lease_heartbeat_cannot_revive(self) -> None:
        identity = instancelease.identity()
        instance_id = identity['instance_id']
        owner = identity['owner']

        claimed, fence = instancelease.claim(
            instance_id,
            owner=owner,
            ttl_seconds=30,
            now=1000,
        )
        self.assertTrue(claimed)
        before = instancelease.describe(instance_id)
        self.assertIsNotNone(before)

        self.assertFalse(instancelease.heartbeat(
            instance_id=instance_id,
            fence=fence,
            owner=owner,
            ttl_seconds=30,
            now=1030,
        ))
        self.assertFalse(instancelease.heartbeat(
            instance_id=instance_id,
            fence=None,
            owner=owner,
            ttl_seconds=30,
            now=1030,
        ))

        after = instancelease.describe(instance_id)
        self.assertEqual(after['owner'], before['owner'])
        self.assertEqual(after['fence'], before['fence'])
        self.assertEqual(after['lease_until'], before['lease_until'])

    async def test_takeover_is_not_overwritten_by_failed_old_runtime_cleanup(self):
        runtime = instance_runtime.InstanceRuntime(heartbeat_sec=.001, ttl_sec=30)
        self.addAsyncCleanup(runtime.close)
        await runtime.start()
        iid = runtime.identity['instance_id']
        db.execute('UPDATE instance_leases SET owner=?, fence=fence+1 WHERE instance_id=?',
                   ('synthetic-replacement-owner', iid))
        peer = instancelease.describe(iid)
        await asyncio.wait_for(runtime.task, 5)
        self.assertTrue(runtime.lost)
        self.assertFalse(drain.resume())
        await runtime.close()
        self.assertFalse(runtime.release_ok)
        self.assertEqual(instancelease.describe(iid), peer)

    async def test_heartbeat_exception_stops_admission(self):
        # 持续异常最终必须停止准入，但停止要发生在租约到期之前（不能双写窗口）。
        runtime = instance_runtime.InstanceRuntime(heartbeat_sec=.001, ttl_sec=2)
        self.addAsyncCleanup(runtime.close)
        started = time.monotonic()
        with patch.object(instancelease, 'heartbeat', side_effect=RuntimeError('synthetic failure')):
            await runtime.start()
            await asyncio.wait_for(runtime.task, 5)
        stopped_after = time.monotonic() - started
        self.assertTrue(runtime.lost)
        self.assertEqual(runtime.failure_reason, 'heartbeat_error')
        self.assertTrue(drain.is_draining())
        self.assertFalse(drain.resume())
        self.assertGreater(runtime.transient_errors, 1)
        self.assertLess(stopped_after, 2.0)

    async def test_transient_heartbeat_error_recovers_without_stopping_admission(self):
        # 一次瞬时存储故障不能把实例打成永久 draining：租约预算内要重试并恢复。
        real_heartbeat = instancelease.heartbeat
        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('synthetic transient failure')
            return real_heartbeat(*args, **kwargs)

        with patch.object(instancelease, 'heartbeat', side_effect=flaky):
            runtime = instance_runtime.InstanceRuntime(heartbeat_sec=.001, ttl_sec=30)
            self.addAsyncCleanup(runtime.close)
            await runtime.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                    runtime.transient_errors >= 1 and runtime.heartbeats >= 1):
                await asyncio.sleep(.005)
            self.assertFalse(runtime.lost)
            self.assertEqual(runtime.transient_errors, 1)
            self.assertGreaterEqual(runtime.heartbeats, 1)
            self.assertFalse(drain.is_draining())
            row = instancelease.describe(runtime.identity['instance_id'])
            self.assertEqual(row['role'], 'serving')
            self.assertEqual(row['owner'], runtime.identity['owner'])
            self.assertGreater(int(row['lease_until']), int(time.time()))

    async def test_claim_rejection_starts_no_heartbeat_task(self):
        runtime = instance_runtime.InstanceRuntime()
        self.addAsyncCleanup(runtime.close)
        with patch.object(instancelease, 'claim', return_value=(False, 9)):
            with self.assertRaisesRegex(RuntimeError, 'claim rejected'):
                await runtime.start()
        self.assertIsNone(runtime.task)
        self.assertFalse(runtime.held)

    async def test_failed_task_creation_releases_claim_and_closes_coroutine(self):
        runtime = instance_runtime.InstanceRuntime()
        self.addAsyncCleanup(runtime.close)
        with patch.object(asyncio, 'create_task', side_effect=RuntimeError('synthetic task failure')):
            with self.assertRaisesRegex(RuntimeError, 'task failure'):
                await runtime.start()
        row = instancelease.describe(runtime.identity['instance_id'])
        self.assertEqual(row['role'], 'stopped')
        self.assertEqual(row['owner'], '')
        self.assertEqual(row['lease_until'], 0)
        self.assertTrue(runtime.release_ok)

    async def test_invalid_heartbeat_configuration_never_claims(self):
        with patch.object(instancelease, 'claim') as claim:
            for interval, ttl in [(0, 30), (float('nan'), 30), (30, 30), (10, 0)]:
                with self.subTest(interval=interval, ttl=ttl), self.assertRaises(ValueError):
                    instance_runtime.InstanceRuntime(heartbeat_sec=interval, ttl_sec=ttl)
            claim.assert_not_called()

    async def test_expired_self_lease_is_not_automatically_reclaimed(self):
        runtime = instance_runtime.InstanceRuntime(heartbeat_sec=.001, ttl_sec=30)
        self.addAsyncCleanup(runtime.close)
        with patch.object(instancelease, 'claim', wraps=instancelease.claim) as claim:
            await runtime.start()
            db.execute('UPDATE instance_leases SET lease_until=0 WHERE instance_id=?',
                       (runtime.identity['instance_id'],))
            await asyncio.wait_for(runtime.task, 5)
            self.assertEqual(claim.call_count, 1)
            self.assertTrue(runtime.lost)
            self.assertFalse(drain.resume())
            self.assertEqual(instancelease.describe(runtime.identity['instance_id'])['lease_until'], 0)

    async def test_real_application_lifespan_claims_and_releases(self):
        from fastapi.testclient import TestClient
        from server import main, security

        with patch.object(security, 'load_users', return_value={'role_policy_version': 2,
                'users': [{'username': 'admin', 'role': 'admin'}]}), \
                patch.object(security, 'weak_admin_passwords', return_value=[]):
            with TestClient(main.app) as client:
                runtime = main.app.state.instance_runtime
                self.assertTrue(runtime.held)
                self.assertFalse(runtime.task.done())
                iid, fence = runtime.identity['instance_id'], runtime.fence
                self.assertEqual(instancelease.describe(iid)['role'], 'serving')
                self.assertEqual(client.get('/api/healthz').status_code, 200)
                self.assertEqual(client.get('/api/readyz').status_code, 200)
        row = instancelease.describe(iid)
        self.assertEqual(row['role'], 'stopped')
        self.assertEqual(row['owner'], '')
        self.assertEqual(row['lease_until'], 0)
        self.assertEqual(row['fence'], fence)
        self.assertTrue(runtime.task.done())
