"""Slow password checks cannot block the ASGI loop or queue unbounded work."""
import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from server.routers import auth


class LoginAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_password_work_bounded_and_repeated_cancel_retains_slot(self):
        release = threading.Event()
        lock = threading.Lock()
        entered = []
        def complete(*args):
            with lock:
                entered.append(threading.get_ident())
            if not release.wait(3):
                raise RuntimeError('synthetic fixture exceeded deadline')
            return 'synthetic-response'
        request = Mock()
        request.json = AsyncMock(return_value={'username': 'fixture', 'password': 'synthetic-password'})
        with patch.object(auth, '_complete_login', side_effect=complete), \
                patch.object(auth, 'client_ip', return_value='192.0.2.1'), patch.object(auth, '_login_active', 0):
            tasks = [asyncio.create_task(auth.login(request)) for _ in range(auth._LOGIN_MAX_ACTIVE)]
            try:
                for _ in range(1000):
                    if len(entered) == auth._LOGIN_MAX_ACTIVE:
                        break
                    await asyncio.sleep(.001)
                self.assertEqual(len(entered), 4)
                self.assertEqual(auth._login_active, 4)
                self.assertNotIn(threading.get_ident(), entered)
                with self.assertRaises(HTTPException) as caught:
                    await auth.login(request)
                self.assertEqual(caught.exception.status_code, 429)
                tasks[0].cancel()
                await asyncio.sleep(.01)
                tasks[0].cancel()
                await asyncio.sleep(.01)
                self.assertFalse(tasks[0].done())
                self.assertEqual(auth._login_active, 4)
                self.assertEqual(len(entered), 4)
            finally:
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertEqual(results[1:], ['synthetic-response'] * 3)
            self.assertEqual(auth._login_active, 0)

    async def test_worker_failure_releases_admission_without_logging_password(self):
        request = Mock()
        request.json = AsyncMock(return_value={'username': 'fixture', 'password': 'synthetic-password'})
        with patch.object(auth, '_complete_login', side_effect=HTTPException(401, 'fixture')), \
                patch.object(auth, 'client_ip', return_value='192.0.2.1'), patch.object(auth, '_login_active', 0):
            with self.assertRaises(HTTPException) as caught:
                await auth.login(request)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertEqual(auth._login_active, 0)


if __name__ == '__main__':
    unittest.main()
