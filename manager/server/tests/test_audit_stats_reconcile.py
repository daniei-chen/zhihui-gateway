"""Grouped reconciliation preserves null/zero/orphan semantics without writes."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import config, db, keysvc
from server.routers import stats


class ReconciliationAggregationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value, owner in [('DB_PATH', Path(self.tmp.name) / 'stats.db', config),
                                   ('_conn', None, db)]:
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.connect()
        self.addCleanup(lambda: db._conn.close() if db._conn is not None else None)

    def test_empty_database_has_zero_counts_and_no_fabricated_keys(self):
        result = stats.credit_reconcile(user={})
        self.assertEqual(result['items'], [])
        self.assertEqual(result['orphan_rows'], 0)
        self.assertTrue(result['ok'])
        self.assertTrue(all(value == 0 for value in result['totals'].values()))

    def test_unused_unknown_zero_and_deleted_keys_remain_distinct(self):
        used = keysvc.create_key('used')['id']
        unused = keysvc.create_key('unused')['id']
        unknown = keysvc.create_key('unknown')['id']
        db.execute('UPDATE api_keys SET used_credit_units=? WHERE id=?', (250000, used))
        db.executemany('INSERT INTO request_logs(ts,key_id,credit) VALUES(?,?,?)', [
            (1, used, 0.25), (2, used, 0), (3, used, None),
            (4, unknown, None), (5, None, 0.5), (6, 9999, 0.75),
            (7, 9999, None), (8, None, 0),
        ])
        before = db.credit_units_snapshot()
        conn = db.connect()
        writes = conn.total_changes
        result = stats.credit_reconcile(user={})
        self.assertEqual(conn.total_changes, writes)
        self.assertEqual(db.credit_units_snapshot(), before)
        items = {item['key_id']: item for item in result['items']}
        self.assertEqual(set(items), {used, unused, unknown})
        self.assertEqual(items[used]['detail_credit'], 0.25)
        self.assertEqual(items[used]['detail_credit_known_rows'], 2)
        self.assertEqual(items[used]['detail_credit_unknown_rows'], 1)
        self.assertEqual(items[unused]['detail_credit_known_rows'], 0)
        self.assertEqual(items[unused]['detail_credit_unknown_rows'], 0)
        self.assertEqual(items[unknown]['detail_credit_unknown_rows'], 1)
        self.assertEqual(result['totals']['detail'], 1.5)
        self.assertEqual(result['totals']['orphan'], 1.25)
        self.assertEqual(result['totals']['detail_known_rows'], 5)
        self.assertEqual(result['totals']['detail_unknown_rows'], 3)
        self.assertEqual(result['totals']['detail_zero_rows'], 2)
        self.assertEqual(result['orphan_rows'], 3)
