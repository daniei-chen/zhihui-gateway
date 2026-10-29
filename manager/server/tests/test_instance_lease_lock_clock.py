"""Lease timestamps must be evaluated after transaction lock acquisition."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from server import config, db, drain, instancelease, tasklease


class LockClockContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = config.DB_PATH
        instancelease.reset_identity()
        tasklease.reset_identity()
        drain.reset_for_tests()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = Path(self.tmp.name) / 'clock.db'
        self.addCleanup(self.cleanup_db)
        db.connect()
        self.me = instancelease.identity()

    def cleanup_db(self):
        drain.reset_for_tests()
        instancelease.reset_identity()
        tasklease.reset_identity()
        with db._lock:
            db._close_connection_locked()
        config.DB_PATH = self.original_db
        self.tmp.cleanup()

    @contextmanager
    def clock_advances_while_waiting(self):
        clock = [1005]
        original = db.transaction
        @contextmanager
        def delayed_transaction():
            with original() as conn:
                clock[0] = 1040
                yield conn
        with patch.object(instancelease, '_now', side_effect=lambda now: clock[0] if now is None else int(now)), patch.object(db, 'transaction', delayed_transaction):
            yield

    def seed(self):
        claimed, fence = instancelease.claim(now=1000, ttl_seconds=30)
        self.assertTrue(claimed)
        return fence

    def test_heartbeat_cannot_revive_lease_expired_during_lock_wait(self):
        fence = self.seed()
        before = instancelease.describe()
        with self.clock_advances_while_waiting():
            renewed = instancelease.heartbeat(fence=fence, ttl_seconds=30)
        self.assertFalse(renewed)
        self.assertEqual(instancelease.describe(), before)

    def test_role_change_rejects_lease_expired_during_lock_wait(self):
        fence = self.seed()
        before = instancelease.describe()
        with self.clock_advances_while_waiting():
            changed = instancelease.set_role(fence=fence, role='draining')
        self.assertFalse(changed)
        self.assertEqual(instancelease.describe(), before)

    def test_claim_starts_ttl_after_lock_acquisition(self):
        with self.clock_advances_while_waiting():
            claimed, fence = instancelease.claim(ttl_seconds=30)
        self.assertTrue(claimed)
        self.assertEqual(fence, 1)
        row = instancelease.describe()
        self.assertEqual(row['updated_at'], 1040)
        self.assertEqual(row['lease_until'], 1070)

    def test_holding_guard_rechecks_clock_after_query_wait(self):
        fence = self.seed()
        clock = [1005]
        original = db.query_one
        def delayed_query(*args, **kwargs):
            row = original(*args, **kwargs)
            clock[0] = 1040
            return row
        with patch.object(instancelease, '_now', side_effect=lambda now: clock[0] if now is None else int(now)), patch.object(db, 'query_one', side_effect=delayed_query):
            self.assertFalse(instancelease.still_holding(fence=fence))


class RuntimeBudgetContracts(unittest.IsolatedAsyncioTestCase):
    setUp = LockClockContracts.setUp
    cleanup_db = LockClockContracts.cleanup_db

    async def test_claim_latency_consumes_local_budget(self):
        from server import instance_runtime
        from types import SimpleNamespace
        clock = [100.0]
        original = instancelease.claim
        def slow_claim(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 25.0
            return result
        runtime = instance_runtime.InstanceRuntime()
        with patch.object(instance_runtime, 'time', SimpleNamespace(monotonic=lambda: clock[0])), patch.object(instancelease, 'claim', side_effect=slow_claim):
            try:
                await runtime.start()
                self.assertLessEqual(runtime.status()['lease_remaining_sec'], 3.0)
                self.assertGreater(runtime.status()['lease_remaining_sec'], 0.0)
            finally:
                await runtime.close()

    async def test_claim_completed_after_conservative_budget_is_not_admitted(self):
        from server import instance_runtime
        from types import SimpleNamespace
        clock = [100.0]
        original = instancelease.claim
        def expired_claim(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 31.0
            return result
        runtime = instance_runtime.InstanceRuntime()
        with patch.object(instance_runtime, 'time', SimpleNamespace(monotonic=lambda: clock[0])), patch.object(instancelease, 'claim', side_effect=expired_claim):
            try:
                with self.assertRaisesRegex(RuntimeError, 'budget'):
                    await runtime.start()
                self.assertTrue(drain.is_draining())
                self.assertIsNone(runtime.task)
            finally:
                await runtime.close()

    async def test_short_ttl_budget_covers_integer_wall_clock_truncation(self):
        from server import instance_runtime
        runtime = instance_runtime.InstanceRuntime(heartbeat_sec=.01, ttl_sec=2)
        try:
            await runtime.start()
            self.assertLessEqual(runtime._lease_remaining(), 1.0)
        finally:
            await runtime.close()
