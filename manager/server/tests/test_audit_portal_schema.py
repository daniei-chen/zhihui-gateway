"""DDL reuse must survive database replacement and preserve rollback/durability."""
from pathlib import Path
import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from server import config, portalstore as store


class PortalSchemaReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = config.DATA_DIR / 'user-portal.sqlite3'

    def test_warm_reads_skip_ddl_without_weakening_sqlite_settings(self):
        with store.transaction() as conn:
            version = conn.execute('PRAGMA schema_version').fetchone()[0]
        statements = []
        connect = sqlite3.connect

        def trace_connection(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(sqlite3, 'connect', side_effect=trace_connection):
            with store.transaction() as conn:
                self.assertEqual(conn.execute('PRAGMA schema_version').fetchone()[0], version)
                self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0], 2)
                self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0], 0)
        self.assertFalse(any('CREATE TABLE' in sql or 'CREATE INDEX' in sql or 'CREATE TRIGGER' in sql
                             for sql in statements))

    def test_replaced_file_with_same_schema_version_is_reinitialized(self):
        with store.transaction() as conn:
            version = conn.execute('PRAGMA schema_version').fetchone()[0]
        replacement = self.path.with_suffix('.new')
        with sqlite3.connect(replacement) as conn:
            # An empty replacement can carry the same schema cookie.
            conn.execute(f'PRAGMA schema_version={version}')
        os.replace(replacement, self.path)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0], 0)

    def test_schema_change_is_detected_on_same_file(self):
        with store.transaction() as conn:
            conn.execute('DROP INDEX portal_accounts_owner')
        with store.transaction() as conn:
            names = {row[1] for row in conn.execute('PRAGMA index_list(portal_accounts)')}
            self.assertIn('portal_accounts_owner', names)

    def test_failed_transaction_still_rolls_back(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            with store.transaction() as conn:
                conn.execute('INSERT INTO portal_settings VALUES(?,?)', ('rollback-test', 'yes'))
                raise RuntimeError('synthetic failure')
        with store.transaction() as conn:
            self.assertIsNone(conn.execute("SELECT value FROM portal_settings WHERE key='rollback-test'").fetchone())


class PortalBackgroundIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_wallet_recovery_wait_does_not_suspend_event_loop(self):
        from server import portalservice, portalwallet
        released = threading.Event()

        def blocked_recovery():
            if not released.wait(0.5):
                raise RuntimeError('recovery blocked the event loop')

        handle = asyncio.get_running_loop().call_later(0.01, released.set)
        try:
            with (mock.patch.object(portalwallet, 'recover', side_effect=blocked_recovery),
                  mock.patch.object(portalservice, 'publish_confirmed_accounts'),
                  mock.patch.object(store, 'automatic_enabled', return_value=False)):
                self.assertEqual(await portalservice.automatic_once(), 0)
        finally:
            released.set()
            handle.cancel()

    async def test_cancellation_joins_recovery_before_timer_task_exits(self):
        from server import portalservice, portalwallet
        started = threading.Event()
        released = threading.Event()
        ended = threading.Event()

        def blocked_recovery():
            started.set()
            released.wait(2)
            ended.set()

        with (mock.patch.object(portalwallet, 'recover', side_effect=blocked_recovery),
              mock.patch.object(portalservice, 'publish_confirmed_accounts') as publish):
            task = asyncio.create_task(portalservice.automatic_once())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                released.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(ended.is_set())
                publish.assert_not_called()
            finally:
                released.set()
                if not task.done():
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
