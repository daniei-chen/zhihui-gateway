import sqlite3
import tempfile
import unittest
from pathlib import Path

from qtg.store import Store


class StoreConnectionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'lifecycle.db')

    def tearDown(self):
        self.tmp.cleanup()

    def test_connection_closed_after_success(self):
        with self.store._conn() as conn:
            conn.execute('CREATE TABLE lifecycle(value INTEGER)')
            conn.execute('INSERT INTO lifecycle VALUES(1)')
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
        with self.store._conn() as check:
            self.assertEqual(check.execute('SELECT value FROM lifecycle').fetchone()[0], 1)

    def test_failure_rolls_back_and_closes(self):
        with self.store._conn() as conn:
            conn.execute('CREATE TABLE lifecycle(value INTEGER)')
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.store._conn() as conn:
                conn.execute('INSERT INTO lifecycle VALUES(1)')
                raise RuntimeError('rollback')
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
        with self.store._conn() as check:
            self.assertEqual(check.execute('SELECT count(*) FROM lifecycle').fetchone()[0], 0)
