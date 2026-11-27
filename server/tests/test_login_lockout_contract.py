"""账号锁必须自己生效：锁定期内照拒错密码，但不再连累正确密码。

## 以前用 X，现在用 Y

v2.10.2 起，按用户名的锁定额外要求「当前来源 IP 自己也失败过」
（`security.login_blocked` 里 `icnt > 0`）。**X** = 用「干净 IP 不受他人制造的
账号锁定影响」来防匿名者用 5 次错密码把管理员锁在门外。

代价是账号锁被架空：攻击者每次换一个 IP，`icnt` 恒为 0，所以「5 次失败即锁定」
对一个轮换 IP 的撞库者从来不会生效（审计 D-1）。门户登录是同一个写法（D-2）。

**Y** = 把「防匿名锁死」从入口闸门换成判定粒度，判定顺序改成「先验密码，再决定」：

  · 锁定期内**错误**密码：仍然拒绝（429）并继续计数 —— 撞库拿不到额外尝试；
  · 锁定期内**正确**密码：照常放行并清零 —— 受害者永远能自己走进来。

匿名者能拦住的只有自己猜错的那些尝试，拦不住受害者。代价是锁定期内也要跑一次
KDF（正常失败本来就要跑），所以锁定与普通失败的响应时间一致，不额外制造时序旁路；
限速因此由 IP 维度与登录并发闸门承担，不由账号锁承担。

本文件是这一契约的回归钉。`MAX_FAILS` 是常量，用例用常量驱动而不是写死 5/6/7。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import main, security  # noqa: E402
from server.routers import auth as auth_router  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402

PASSWORD = 'TestPw123!'
ORIGIN = {'Origin': 'http://testserver'}


class AccountLockContract(unittest.TestCase):
    def setUp(self) -> None:
        isolate_database(self)
        self.client = TestClient(main.app)
        security.save_users({'secret': 'T' * 43, 'role_policy_version': 2, 'users': [
            {'username': 'admin', 'role': 'admin', 'sv': 1,
             'pwd_hash': security.make_hash(PASSWORD)}], 'api_keys': []})
        for name, value in (('_fail', {}), ('_user_fail', {}), ('_token_fail', {}),
                            ('_FAILS_LOADED', True)):
            patcher = mock.patch.object(security, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ip = '203.0.113.1'
        patcher = mock.patch.object(auth_router, 'client_ip', lambda request: self.ip)
        patcher.start()
        self.addCleanup(patcher.stop)

    def attempt(self, ip: str, password: str, username: str = 'admin'):
        self.ip = ip
        return self.client.post('/api/login', headers=ORIGIN,
                                json={'username': username, 'password': password})

    def fill_account_lock(self) -> None:
        """用**互不相同**的来源 IP 把同一个账号推到阈值（撞库者换 IP 的形态）。"""
        for index in range(security.MAX_FAILS):
            response = self.attempt(f'203.0.113.{index + 1}', 'wrong-password')
            self.assertEqual(response.status_code, 401, response.text)

    def test_rotating_source_ips_cannot_evade_the_account_lock(self) -> None:
        """审计 D-1：每次换 IP 的分布式撞库必须撞在账号锁上。"""
        self.fill_account_lock()
        self.assertTrue(security.login_blocked('198.51.100.1', 'admin'))
        response = self.attempt('198.51.100.1', 'wrong-password')
        self.assertEqual(response.status_code, 429, response.text)

    def test_correct_password_is_admitted_during_the_account_lock(self) -> None:
        """Y 的核心：账号被匿名者推到锁定期，受害者用正确密码仍能登录。"""
        self.fill_account_lock()
        self.assertTrue(security.login_blocked('198.51.100.2', 'admin'))
        response = self.attempt('198.51.100.2', PASSWORD)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(security.login_blocked('198.51.100.2', 'admin'),
                         '成功登录必须清零账号与来源两个维度的计数')
        self.assertNotIn('admin', security._user_fail)

    def test_wrong_password_is_still_rejected_and_counted_during_the_lock(self) -> None:
        """锁定期内错密码不放行，而且继续计数（不是「锁上就停止记账」）。"""
        self.fill_account_lock()
        response = self.attempt('198.51.100.3', 'wrong-password')
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(security._user_fail['admin'][0], security.MAX_FAILS + 1)

    def test_ip_dimension_stays_a_hard_gate_for_the_same_source(self) -> None:
        """IP 维度是唯一还在真正限速的闸门，保持在密码校验之前硬拦。

        用不存在的用户名构造，避免账号维度先锁，单独验证 IP 维度。
        """
        for _ in range(security.MAX_FAILS):
            self.assertEqual(self.attempt('203.0.113.50', 'wrong-password',
                                          username='ghost').status_code, 401)
        response = self.attempt('203.0.113.50', PASSWORD)
        self.assertEqual(response.status_code, 429, response.text)

    def test_lockout_is_observable(self) -> None:
        """看不见的撞库等于没防：触发锁定要留下账号与来源数。"""
        with self.assertLogs('workbuddy.security', level='WARNING') as logs:
            self.fill_account_lock()
        self.assertTrue(any('login_lockout' in line for line in logs.output), logs.output)
        snapshot = security.login_lockout_snapshot()
        self.assertEqual(snapshot['account_count'], 1, snapshot)
        self.assertEqual(snapshot['accounts'][0]['account'], 'admin')
        self.assertEqual(snapshot['accounts'][0]['sources'], security.MAX_FAILS)


if __name__ == '__main__':
    unittest.main()
