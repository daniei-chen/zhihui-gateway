"""A planned admission drain is different from a database/ledger failure."""
import unittest
from unittest import mock

from server import operational


class StopMonitor(Exception):
    pass


def snapshot(*, draining=True, database=True, ledger=True):
    return {'ok': database and ledger and not draining, 'draining': draining,
            'database_write': database, 'ledger_write': ledger}


class PlannedDrainAlertTest(unittest.IsolatedAsyncioTestCase):
    async def observe(self, statuses):
        count = 0
        async def tick(_):
            nonlocal count
            count += 1
            if count == len(statuses):
                raise StopMonitor
        absent_backup = mock.Mock()
        absent_backup.is_file.return_value = False
        with mock.patch.object(operational, 'alerts', {}), \
                mock.patch.object(operational, 'readiness', side_effect=statuses), \
                mock.patch.object(operational.db, 'query_one', return_value={'n':0,'failed':0}), \
                mock.patch.object(operational, 'Path', return_value=absent_backup), \
                mock.patch.object(operational.asyncio, 'sleep', side_effect=tick), \
                mock.patch.object(operational, 'transition', wraps=operational.transition) as events:
            with self.assertRaises(StopMonitor):
                await operational.monitor()
            calls = [call.args[1] for call in events.call_args_list if call.args[0]=='readiness']
            return calls, dict(operational.alerts)

    async def test_repeated_planned_drain_does_not_report_storage_failure(self):
        calls, alerts = await self.observe([snapshot(),snapshot(),snapshot()])
        self.assertEqual(calls,[False,False,False])
        self.assertNotIn('readiness',alerts)

    async def test_real_database_failure_still_alerts_during_planned_drain(self):
        calls, alerts = await self.observe([snapshot(database=False),snapshot(database=False)])
        self.assertEqual(calls,[False,True])
        self.assertIn('readiness',alerts)

    async def test_real_ledger_failure_still_alerts_during_planned_drain(self):
        calls, alerts = await self.observe([snapshot(ledger=False),snapshot(ledger=False)])
        self.assertEqual(calls,[False,True])
        self.assertIn('readiness',alerts)

    async def test_healthy_drain_resets_storage_failure_streak(self):
        calls, alerts = await self.observe([snapshot(database=False),snapshot(),snapshot(database=False)])
        self.assertEqual(calls,[False,False,False])
        self.assertNotIn('readiness',alerts)

    async def test_recovery_is_recorded_without_reopening_admission(self):
        calls, alerts = await self.observe([snapshot(database=False),snapshot(database=False),snapshot()])
        self.assertEqual(calls,[False,True,False])
        self.assertNotIn('readiness',alerts)
