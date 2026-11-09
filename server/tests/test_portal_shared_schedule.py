"""Keep original pool check-in times when the shared Manager becomes authority."""
import unittest
from unittest import mock

from server.services import dailyplan
from server.tests.test_daily_plan import _patch_db, _body, _ts


class SharedScheduleTests(unittest.TestCase):
    def test_checkin_preserves_multiple_hours_and_each_slot_has_its_own_cursor(self):
        patcher, storage = _patch_db({'daily_tasks_plan': {'checkin': {
            'enabled': True, 'hour': 9, 'hours': [21, 9], 'last_run': '2026-10-13-09'}}})
        with patcher:
            plan = dailyplan.get_plan()
            self.assertEqual(plan['checkin']['hours'], [9, 21])
            self.assertFalse(dailyplan.is_due('checkin', plan, _ts(2026, 10, 13, 9)))
            self.assertTrue(dailyplan.is_due('checkin', plan, _ts(2026, 10, 13, 21)))
            self.assertFalse(dailyplan.is_due('checkin', plan, _ts(2026, 10, 13, 18)))
            dailyplan.set_plan(_body(checkin=(True, 9)))
            self.assertEqual(dailyplan.get_plan()['checkin']['hours'], [9, 21])
            dailyplan.set_plan(_body(checkin=(True, 10)))
            self.assertNotIn('hours', dailyplan.get_plan()['checkin'])
            self.assertTrue(dailyplan.is_due('checkin', dailyplan.get_plan(), _ts(2026, 10, 13, 10)))

    def test_explicit_schedule_edits_validate_and_preserve_last_run(self):
        patcher, storage = _patch_db({'daily_tasks_plan': {'checkin': {'last_run': 'existing'}}})
        with patcher:
            value = _body(checkin=(True, 9))
            for invalid in ([], [24], [True], '9,21', [9.5]):
                value['checkin']['hours'] = invalid
                with self.assertRaises((ValueError, TypeError)):
                    dailyplan.set_plan(value)
            value['checkin']['hours'] = [21, 9, 21]
            result = dailyplan.set_plan(value)
        self.assertEqual(result['checkin']['hours'], [9, 21])
        self.assertEqual(result['checkin']['last_run'], 'existing')
