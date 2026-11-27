"""门户账号锁同样没有来源 IP 依赖（审计 D-2）。

## 以前的写法

`portal.login` 把 SQLite 里的账号锁与内存里的 `(ip, 账号)` 计数取「与」：

    if user and user['blocked_until'] > store.now() and _portal_user_blocked(ip, username):

于是锁定期内换一个干净 IP 就能继续试密码 —— 账号锁对分布式撞库永远不生效，
只有「同一 IP 连续失败」这一条路能撞上它。而 `_portal_user_blocked` 的键里带 IP，
本来就表达不了「这个账号被多个来源打过」。

## 现在的约定（与 `test_login_lockout_contract.py` 同一条）

账号维度以 SQLite 的 `blocked_until` 为准、独立生效；内存 `(ip, 账号)` 计数退回
只做来源统计与容量边界，不再参与判定。锁定期内**错**密码继续拒绝并计数，
**对**密码照常放行并清零 —— 匿名者锁不死受害者，也拿不到额外尝试。
"""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI

from server import config, db, security, portalstore as store
from server.routers import portal

NOW = 1793600000
PORTAL_PASSWORD = 'synthetic-password'
PORTAL_MAX_FAILS = 5
ORIGIN = {'origin': 'http://testserver'}


class PortalAccountLockContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='portal-lockout-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in {'DATA_DIR': root / 'data', 'DB_PATH': root / 'manager.db',
                           'USERS_FILE': root / 'users.json',
                           'AUTH_DIR': root / 'pool'}.items():
            self.stack.enter_context(mock.patch.object(config, key, value))
        self.stack.enter_context(mock.patch.object(store, 'now', return_value=NOW))
        self.stack.enter_context(
            mock.patch.dict('os.environ', {'WB_PORTAL_PUBLIC_POOL_ENABLED': '0'}))
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.connect()
        self.addCleanup(self.close_db)
        with portal._PORTAL_IP_LOCK:
            portal._PORTAL_IP_FAILS.clear()
            portal._PORTAL_USER_FAILS.clear()
        self.addCleanup(self.clear_counters)
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_users(id,username,pwd_hash,created_at) '
                         'VALUES(?,?,?,?)',
                         ('alice', 'alice', security.make_hash(PORTAL_PASSWORD), NOW))
        self.app = FastAPI()
        self.app.include_router(portal.router)
        self.transport = httpx.ASGITransport(self.app, raise_app_exceptions=False)
        self.ip = '203.0.113.1'
        patcher = mock.patch.object(portal, 'client_ip', lambda request: self.ip)
        patcher.start()
        self.addCleanup(patcher.stop)

    def close_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None

    def clear_counters(self) -> None:
        with portal._PORTAL_IP_LOCK:
            portal._PORTAL_IP_FAILS.clear()
            portal._PORTAL_USER_FAILS.clear()

    async def attempt(self, ip: str, password: str):
        self.ip = ip
        async with httpx.AsyncClient(transport=self.transport,
                                     base_url='http://testserver') as client:
            return await client.post('/api/portal/login', headers=ORIGIN,
                                     json={'username': 'alice', 'password': password})

    async def fill_account_lock(self) -> None:
        for index in range(PORTAL_MAX_FAILS):
            response = await self.attempt(f'203.0.113.{index + 1}', 'wrong-password')
            self.assertEqual(response.status_code, 401, response.text)
        self.assertGreater(store.user('alice')['blocked_until'], NOW)

    async def test_wrong_password_from_a_fresh_source_is_rejected_inside_the_lock(self) -> None:
        """审计 D-2：锁定期内换干净 IP，错密码必须被账号锁拦下（修复前是 401）。"""
        await self.fill_account_lock()
        response = await self.attempt('198.51.100.1', 'wrong-password')
        self.assertEqual(response.status_code, 429, response.text)

    async def test_correct_password_from_a_fresh_source_still_logs_in(self) -> None:
        """账号锁独立生效后，受害者仍能用自己的正确密码进门，并清掉锁。"""
        await self.fill_account_lock()
        response = await self.attempt('198.51.100.2', PORTAL_PASSWORD)
        self.assertEqual(response.status_code, 200, response.text)
        row = store.user('alice')
        self.assertEqual((row['failed_logins'], row['blocked_until']), (0, 0))


if __name__ == '__main__':
    unittest.main()
