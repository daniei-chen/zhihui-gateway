"""Bad selectors cannot overflow SQLite or silently broaden a realm query."""
import unittest
from unittest import mock

from fastapi import HTTPException
from server import db
from server.routers import logs


class LogFilterBoundaryTest(unittest.TestCase):
    def test_unrepresentable_integers_are_rejected_before_database_access(self):
        for field in ('key_id', 'since', 'until'):
            for value in (2**63, -(2**63)-1, 10**100):
                with self.subTest(field=field, value=value):
                    call = logs.list_logs if field == 'key_id' else logs.list_outcomes
                    with mock.patch.object(db, 'query_one', side_effect=AssertionError('invalid input reached SQLite')):
                        with self.assertRaises(HTTPException) as failure:
                            call(**{field: str(value) if field == 'key_id' else value}, user={})
                    self.assertEqual(failure.exception.status_code, 400)

    def test_misspelled_realm_does_not_remove_filter(self):
        with mock.patch.object(db, 'query_one', side_effect=AssertionError('unfiltered query')):
            with self.assertRaises(HTTPException) as failure:
                logs.list_logs(realm='glboal', user={})
        self.assertEqual(failure.exception.status_code, 400)

    def test_blank_class_list_is_not_reported_as_no_matching_traffic(self):
        with self.assertRaises(HTTPException) as failure:
            logs.list_outcomes(traffic_class=' , , ', user={})
        self.assertEqual(failure.exception.status_code, 400)
