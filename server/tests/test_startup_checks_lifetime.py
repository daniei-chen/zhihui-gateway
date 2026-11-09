"""Startup audits remain enabled, but their workers belong to one lifespan."""
import asyncio,threading,unittest
from unittest import mock
from server import security
from server.services.startupchecks import WeakAdminCheck


class StartupAuditLifetime(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_lifetimes_stop_and_join_each_worker(self):
        for _ in range(3):
            entered=threading.Event()
            def inspect(*,stop):
                entered.set();stop.wait(3);return []
            with mock.patch.object(security,'weak_admin_passwords',side_effect=inspect):
                check=WeakAdminCheck();check.start()
                try:
                    for _ in range(100):
                        if entered.is_set():break
                        await asyncio.sleep(.001)
                    self.assertTrue(entered.is_set())
                finally:await check.close()
                self.assertTrue(check.stop.is_set());self.assertFalse(check.thread.is_alive())
                with self.assertRaises(RuntimeError):check.start()

    async def test_shutdown_waits_for_inflight_hash_after_repeated_cancellation(self):
        entered,finish,returned=threading.Event(),threading.Event(),threading.Event()
        def inspect(*,stop):
            entered.set();finish.wait(3);returned.set();return []
        with mock.patch.object(security,'weak_admin_passwords',side_effect=inspect):
            check=WeakAdminCheck();check.start()
            while not entered.is_set():await asyncio.sleep(.001)
            closing=asyncio.create_task(check.close())
            try:
                await asyncio.sleep(.01);closing.cancel();await asyncio.sleep(.01);closing.cancel()
                await asyncio.sleep(.01);self.assertFalse(closing.done());self.assertFalse(returned.is_set())
            finally:
                finish.set()
                with self.assertRaises(asyncio.CancelledError):await closing
            self.assertFalse(check.thread.is_alive());self.assertTrue(returned.is_set())

    async def test_no_worker_is_started_when_not_requested(self):
        with mock.patch.object(security,'weak_admin_passwords') as inspect:
            check=WeakAdminCheck();await check.close();inspect.assert_not_called()

    async def test_thread_start_failure_can_still_close_cleanly(self):
        check=WeakAdminCheck()
        with mock.patch.object(threading.Thread,'start',side_effect=RuntimeError('synthetic start failure')):
            with self.assertRaisesRegex(RuntimeError,'synthetic start failure'):check.start()
        await check.close();self.assertIsNone(check.thread)


class CooperativePasswordScan(unittest.TestCase):
    def test_already_cancelled_scan_reads_no_users(self):
        stop=threading.Event();stop.set()
        with mock.patch.object(security,'load_users') as load:
            self.assertEqual(security.weak_admin_passwords(stop=stop),[]);load.assert_not_called()

    def test_cancellation_between_hashes_preserves_users_and_prevents_another_hash(self):
        stop=threading.Event();users={'users':[{'username':'synthetic','role':'admin','pwd_hash':'synthetic-hash'}]}
        def verify(*args):stop.set();return False
        with mock.patch.object(security,'load_users',return_value=users),mock.patch.object(security,'verify_pwd',side_effect=verify) as checked:
            self.assertEqual(security.weak_admin_passwords(stop=stop),[])
            self.assertEqual(checked.call_count,1)
        self.assertEqual(users,{'users':[{'username':'synthetic','role':'admin','pwd_hash':'synthetic-hash'}]})

    def test_uncancelled_scan_still_reports_weak_admin(self):
        with mock.patch.object(security,'load_users',return_value={'users':[{'username':'synthetic','role':'admin','pwd_hash':'synthetic'}]}),mock.patch.object(security,'verify_pwd',return_value=True):
            self.assertEqual(security.weak_admin_passwords(stop=threading.Event()),['synthetic'])


class ApplicationShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from server.tests._db_fixture import isolate_database
        from server import drain
        isolate_database(self);drain.reset_for_tests();self.addCleanup(drain.reset_for_tests)

    async def test_cancelled_password_join_still_finishes_other_lifespan_cleanup(self):
        from server import config,main
        users_file=config.DATA_DIR/'startup-users.json'
        users_file.write_text('{"role_policy_version":2,"users":[]}',encoding='utf-8')
        entered,finish=threading.Event(),threading.Event()
        def inspect(*,stop):entered.set();finish.wait(3);return []
        check=WeakAdminCheck()
        with mock.patch.object(security,'weak_admin_passwords',side_effect=inspect), \
             mock.patch.object(security,'load_users',return_value={'role_policy_version':2,'users':[{'username':'admin','role':'admin'}]}), \
             mock.patch.object(config,'BACKGROUND_JOBS',False),mock.patch.object(config,'SHARED_ADMISSION',True), \
             mock.patch.object(config,'USERS_FILE',users_file), \
             mock.patch.object(main,'WeakAdminCheck',return_value=check),mock.patch.object(config,'close_clients',new_callable=mock.AsyncMock) as close_clients:
            check.start();lifetime=main.lifespan(main.app);closing=None
            try:
                await lifetime.__aenter__()
                closing=asyncio.create_task(lifetime.__aexit__(None,None,None))
                await asyncio.sleep(.01);closing.cancel();await asyncio.sleep(.01);closing.cancel()
                await asyncio.sleep(.01);self.assertFalse(closing.done())
                finish.set()
                with self.assertRaises(asyncio.CancelledError):await closing
                close_clients.assert_awaited_once()
                self.assertFalse(main.app.state.instance_runtime.held)
                self.assertFalse(check.thread.is_alive())
            finally:
                finish.set()
                if closing is not None and not closing.done():
                    try:await closing
                    except asyncio.CancelledError:pass
                await check.close()
