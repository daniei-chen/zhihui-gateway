"""Concurrent direct connect() calls share one fully initialized connection."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import config, db


class ConnectionOwnershipTest(unittest.TestCase):
    def test_direct_concurrent_open_has_one_owner_and_no_partial_schema(self):
        real_connect = sqlite3.connect
        original_path = config.DB_PATH
        original_connection = db._conn
        entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        allocations = []
        allocation_lock = threading.Lock()

        def blocked_open(*args, **kwargs):
            with allocation_lock:
                second = entered.is_set()
                entered.set()
                if second:
                    second_entered.set()
            if not release.wait(5):
                raise RuntimeError('QA connection gate timed out')
            connection = real_connect(*args, **kwargs)
            allocations.append(connection)
            return connection

        with tempfile.TemporaryDirectory() as directory:
            config.DB_PATH = Path(directory)/'owner.db'
            db._conn = None
            try:
                with patch.object(sqlite3, 'connect', side_effect=blocked_open):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        first = executor.submit(db.connect)
                        self.assertTrue(entered.wait(5))
                        second = executor.submit(db.connect)
                        concurrent_factory_entry = second_entered.wait(0.2)
                        release.set()
                        one = first.result(timeout=10)
                        two = second.result(timeout=10)
                self.assertFalse(concurrent_factory_entry,
                                 'another thread entered SQLite open before initialization finished')
                self.assertEqual(len(allocations), 1)
                self.assertIs(one, two)
                self.assertIs(db._conn, one)
                self.assertTrue(one.execute("SELECT name FROM sqlite_master WHERE name='api_keys'").fetchone())
            finally:
                release.set()
                for connection in allocations:
                    connection.close()
                db._conn = original_connection
                config.DB_PATH = original_path
