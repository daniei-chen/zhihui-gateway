"""PB0108：IP 管控防锁死回归。

## 为什么这条必须单独测

"写 IP 规则把自己锁在门外"是**不可自助恢复**的故障：一旦保存，管理员立刻失去
访问权，只能登录服务器改数据库。它不报错、也不影响保存那一刻的响应——只是下一次
请求开始 401，所以只能由服务端在**写入前**识别。

## 设计取舍：不硬拦

管理员可能就是要先把规则配好、再从外部切换。所以给 `confirm_lockout` 显式确认
通道；默认拒绝，且拒绝理由要能驱动前端弹确认框（`code=self_lockout_risk`），
而不是一个泛化的 403——泛化错误会让人以为是权限不够，反而去改权限。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import db, main, security  # noqa: E402
from server.routers import security as security_router  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402

#: 测试里冒充的"操作者来源 IP"。client_ip() 在 TestClient 下返回 "testclient"
#: （非合法 IP），所以直接 patch 成可控值，才能精确构造"自己是否被覆盖"。
OPERATOR_IP = '203.0.113.7'
OTHER_IP = '198.51.100.9'


class LockoutBase(unittest.TestCase):
    def setUp(self) -> None:
        isolate_database(self)
        self.client = TestClient(main.app)
        self._patch_ip(OPERATOR_IP)
        self._login()

    def _patch_ip(self, ip: str) -> None:
        patcher = mock.patch.object(security_router, 'client_ip', lambda request: ip)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _login(self) -> None:
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('TestPw123!')}], 'api_keys': []})
        r = self.client.post('/api/login',
                             json={'username': 'admin', 'password': 'TestPw123!'},
                             headers={'Origin': 'http://testserver'})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.cookies.update(dict(r.cookies))
        self.headers = {'Origin': 'http://testserver'}

    # ---- helpers ----
    def _add_rule(self, kind: str, cidr: str, *, confirm: bool = False):
        body = {'kind': kind, 'cidr': cidr, 'note': 't'}
        if confirm:
            body['confirm_lockout'] = True
        return self.client.post('/api/security/rules', json=body, headers=self.headers)

    def _save_config(self, *, enabled: bool, mode: str, confirm: bool = False):
        body = {'enabled': enabled, 'mode': mode}
        if confirm:
            body['confirm_lockout'] = True
        return self.client.post('/api/security/config', json=body, headers=self.headers)

    def _audit_actions(self) -> list[str]:
        return [row['action'] for row in db.query(
            'SELECT action FROM audit_logs ORDER BY id DESC LIMIT 20')]

    def _assert_message_is_accurate(self, message: str, ip: str) -> None:
        """确认文案必须说准**真实后果**，而不是「你被锁在控制台外面了」。

        为什么值得单独钉一条：第一版文案写的是「保存后你会被挡在门外，只能登录
        服务器改数据库才能恢复」。这句话是**错的**——`ip_rules` 只被
        gateway.py / anthropic.py 的 `/v1` 入站闸门消费，`/api/*` 控制面没有这层
        检查，管理员照样能登录本页改回来（复核命令：`grep -rn ip_rules`）。

        危害是双向的：照那句话去做的人会去服务器上翻数据库，而正确操作只是点两下；
        同时它还**漏掉了真正的后果**——推理业务中断。所以这里既要求说全（业务中断
        + 控制台不受影响），也反向禁止那几句错话回来。
        """
        self.assertIn(ip, message, '必须指出是哪个来源 IP 受影响')
        self.assertIn('推理 API', message, '必须说清被切断的是推理入站')
        self.assertIn('业务会中断', message, '真正的后果是业务中断，不能轻描淡写')
        self.assertIn('控制台', message, '必须说明控制台不受影响，否则会把人指向改数据库')
        for stale in ('改数据库', '挡在门外', '失去访问权限'):
            self.assertNotIn(stale, message, f'文案又回到错误说法：{stale}')


class WhitelistEnableTest(LockoutBase):
    def test_enabling_whitelist_without_cover_is_rejected(self) -> None:
        r = self._save_config(enabled=True, mode='whitelist')
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()['detail']
        self.assertEqual(detail['code'], 'self_lockout_risk')
        self.assertEqual(detail['ip'], OPERATOR_IP)
        self._assert_message_is_accurate(detail['message'], OPERATOR_IP)
        # 关键：配置不能被写进去
        saved = db.get_setting('security', {}) or {}
        self.assertFalse(saved.get('enabled'), '被拒的请求不得落库')

    def test_enabling_whitelist_with_confirm_succeeds_and_audits(self) -> None:
        r = self._save_config(enabled=True, mode='whitelist', confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['enabled'])
        self.assertIn('security_self_lockout_confirmed', self._audit_actions())

    def test_enabling_whitelist_when_covered_is_allowed(self) -> None:
        self.assertEqual(self._add_rule('allow', '203.0.113.0/24').status_code, 200)
        r = self._save_config(enabled=True, mode='whitelist')
        self.assertEqual(r.status_code, 200, r.text)
        # 没触发自锁就不该有"确认自锁"的审计
        self.assertNotIn('security_self_lockout_confirmed', self._audit_actions())

    def test_blacklist_mode_never_needs_confirmation(self) -> None:
        r = self._save_config(enabled=True, mode='blacklist')
        self.assertEqual(r.status_code, 200, r.text)

    def test_confirm_flag_is_not_persisted_into_config(self) -> None:
        r = self._save_config(enabled=True, mode='whitelist', confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        saved = db.get_setting('security', {}) or {}
        self.assertNotIn('confirm_lockout', saved, '确认开关是请求参数，不该入库')


class DenyRuleTest(LockoutBase):
    def test_deny_rule_covering_self_is_rejected(self) -> None:
        r = self._add_rule('deny', '203.0.113.0/24')
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['code'], 'self_lockout_risk')
        self.assertEqual(db.query('SELECT * FROM ip_rules'), [], '被拒的规则不得落库')

    def test_deny_rule_covering_self_with_confirm_succeeds(self) -> None:
        r = self._add_rule('deny', '203.0.113.0/24', confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(db.query('SELECT * FROM ip_rules')), 1)
        self.assertIn('security_self_lockout_confirmed', self._audit_actions())

    def test_deny_rule_not_covering_self_is_allowed(self) -> None:
        r = self._add_rule('deny', OTHER_IP)
        self.assertEqual(r.status_code, 200, r.text)

    def test_allow_rule_covering_self_is_never_a_risk(self) -> None:
        r = self._add_rule('allow', OPERATOR_IP)
        self.assertEqual(r.status_code, 200, r.text)


class DeleteRuleTest(LockoutBase):
    def _make_whitelist_with(self, cidr: str) -> int:
        rid = db.execute(
            'INSERT INTO ip_rules(kind, cidr, note, created_at) VALUES(?,?,?,?)',
            ('allow', cidr, 'seed', 0))
        db.set_setting('security', {'enabled': True, 'mode': 'whitelist'})
        return rid

    def test_deleting_only_covering_allow_rule_is_rejected(self) -> None:
        rid = self._make_whitelist_with('203.0.113.0/24')
        r = self.client.delete(f'/api/security/rules/{rid}',
                               headers=self.headers)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['detail']['code'], 'self_lockout_risk')
        self.assertEqual(len(db.query('SELECT * FROM ip_rules')), 1, '规则不得被删')

    def test_deleting_only_covering_allow_with_confirm_succeeds(self) -> None:
        rid = self._make_whitelist_with('203.0.113.0/24')
        r = self.client.delete(f'/api/security/rules/{rid}',
                               params={'confirm_lockout': 'true'},
                               headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.query('SELECT * FROM ip_rules'), [])

    def test_deleting_allow_when_another_rule_covers_is_allowed(self) -> None:
        rid = self._make_whitelist_with('203.0.113.0/24')
        db.execute('INSERT INTO ip_rules(kind, cidr, note, created_at) VALUES(?,?,?,?)',
                   ('allow', OPERATOR_IP, 'second', 0))
        r = self.client.delete(f'/api/security/rules/{rid}', headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)

    def test_deleting_when_not_whitelist_mode_is_allowed(self) -> None:
        rid = self._make_whitelist_with('203.0.113.0/24')
        db.set_setting('security', {'enabled': False, 'mode': 'blacklist'})
        r = self.client.delete(f'/api/security/rules/{rid}', headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == '__main__':
    unittest.main()
