"""v1.2.199：登录失败时，快到锁定了才告诉用户还剩几次。

## 由来

2026-11-15：有用户连续输错几次之后来问「密码是不是换了」。
每次失败都只回同一句「用户名或密码错误」，**他不知道自己已经接近锁定**。

计数本来就在手里，只是没往外递。

## 为什么只在剩余一两次时才提示

全程报进度，等于把「还能试几次」直接送给爆破方。
而「快锁定了」这条信息对正常用户最有用、对攻击者最没用 —— 他马上就进不去了。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AUTH = (ROOT / 'server' / 'routers' / 'auth.py').read_text(encoding='utf-8')
SEC = (ROOT / 'server' / 'security.py').read_text(encoding='utf-8')


class RecordFailReturnsTheCountTests(unittest.TestCase):
    def test_signature_returns_int(self):
        self.assertIn('def record_fail(ip: str, username: str | None = None) -> int:', SEC)

    def test_it_returns_the_larger_of_the_two_counters(self):
        """锁定判定同时看 IP 与用户名；只报一边会让用户看到「还剩 4 次」却已被锁。

        原来这里断言的是源码字面量 `counted = max(counted, ucnt + 1)`。2026-11-28 的
        D-1 修复把用户名条目改成带来源集合的三段结构，那一行变成 `counted = max(counted,
        entry[0])`，字面量不再存在——但**行为**没变，反而更该直接验证行为：真的调一次
        `record_fail`，让 IP 与用户名两条计数不对称，返回值必须是大的那个。
        """
        from server import security
        with mock.patch.object(security, '_fail', {}),                 mock.patch.object(security, '_user_fail', {}),                 mock.patch.object(security, '_FAILS_LOADED', True):
            for _ in range(6):
                security.record_fail('198.51.100.1', 'fixture-user')
            counted = 0
            for _ in range(2):
                counted = security.record_fail('198.51.100.2', 'fixture-user')
        self.assertEqual(counted, 8, '按用户名 8 次 > 按 IP 2 次，应报 8')

    def test_it_explains_why(self):
        self.assertIn('他不知道自己快到锁定了', SEC)


class ItOnlyWarnsNearTheLimitTests(unittest.TestCase):
    def test_it_computes_remaining(self):
        self.assertIn('remaining = max(security.MAX_FAILS - fails, 0)', AUTH)

    def test_it_tolerates_a_non_int_return(self):
        """测试里会把 record_fail 换成只记调用的桩。

        登录失败这条路径**不能因为一个附加提示取值失败就整条崩掉** ——
        那样连「用户名或密码错误」都不返回，用户看到的是 500。
        """
        self.assertIn('isinstance(fails, int)', AUTH)
        self.assertIn('not isinstance(fails, bool)', AUTH)

    def test_it_only_warns_when_two_or_fewer(self):
        self.assertIn('0 < remaining <= 2', AUTH)

    def test_the_message_states_the_number(self):
        self.assertIn('再错 {remaining} 次将暂时锁定', AUTH)

    def test_it_explains_why_not_every_time(self):
        """全程报进度等于把「还能试几次」送给爆破方。"""
        self.assertIn('送给爆破方', AUTH)

    def test_the_plain_message_still_exists(self):
        self.assertIn("detail = '用户名或密码错误'", AUTH)


class ItDoesNotRevealUserExistenceTests(unittest.TestCase):
    """用户名不存在时也必须走同样的路径，不能靠文案区分。"""

    def test_unknown_user_still_calls_record_fail(self):
        self.assertIn('if not user or not security.verify_pwd', AUTH)


if __name__ == '__main__':
    unittest.main()
