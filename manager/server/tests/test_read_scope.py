"""scope=read 只读服务令牌的边界（K26 / N32）。

为什么值得有：这把令牌是给 QTG 的 http 读通道用的。它一旦过宽，就等于
"泄露一把服务令牌 = 能读全站数据 + 可能被当跳板写"；一旦过窄，K26 的统计族又读不到。
两侧都会安静地出错（403 或多余权限），所以用白盒测试钉住边界。

钉住的不变量：
  1. **只读**：任何写方法（POST/PATCH/DELETE/PUT）一律 403，不管路径多"像读"；
  2. **只读族**：统计/日志/任务/审计/账号/模型/密钥列表可读，v2 的 usage 族也可读；
  3. **不得读凭据面与用户面**：/api/tokens、/api/users 明确 403
     （前者是管理凭据，后者是权限主体，都不该给内部只读消费方）；
  4. **scope=keys 不受影响**：原有收敛语义保持不变；
  5. R1-A（2026-10-25）：`/api/audit-logs` 原本只用 `require_admin` 把关，导致
     scope=read 拿了 403——白名单允许、端点依赖不允许，两边自己打自己。现在改成
     `require_admin_or_read_scope`：管理员或只读服务令牌可读，viewer 仍不可读。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import security  # noqa: E402


class _Req:
    """最小 Request 替身：current_user 只用到 url.path / method / state。"""

    def __init__(self, path: str, method: str = 'GET') -> None:
        self.url = mock.Mock(path=path)
        self.method = method
        self.state = mock.Mock(spec=['audit_actor'])

    def __getattr__(self, item):  # pragma: no cover - 兜底
        raise AttributeError(item)


def _user(scope: str) -> dict:
    return {'username': 'qtg-service', 'role': 'viewer', 'scope': scope}


class ReadScopeTest(unittest.TestCase):
    def _call(self, path: str, method: str, scope: str = 'read') -> dict:
        req = _Req(path, method)
        with mock.patch.object(security, '_current_user', return_value=_user(scope)):
            return security.current_user(req)

    def test_stats_readable(self) -> None:
        for path in ('/api/stats/summary', '/api/stats/daily', '/api/stats/hourly',
                     '/api/stats/credits', '/api/stats/by-model', '/api/stats/by-key',
                     '/api/stats/upstream'):
            self.assertTrue(self._call(path, 'GET'))
            self.assertTrue(self._call(path + '/', 'GET'), '尾斜杠也要放行')

    def test_other_read_families(self) -> None:
        for path in ('/api/logs', '/api/task-logs', '/api/checkin-logs',
                     '/api/audit-logs', '/api/accounts', '/api/models',
                     '/api/keys', '/api/upstreams', '/api/proxy-routes',
                     '/api/settings/panel', '/api/read-api/version',
                     '/api/me', '/api/healthz', '/api/readyz'):
            self.assertTrue(self._call(path, 'GET'), path)

    def test_usage_family_v2_allowed(self) -> None:
        """v2 新增的 usage 族必须在白名单内（前缀 /api/read-api/ 覆盖）。"""
        for path in ('/api/read-api/usage-matrix', '/api/read-api/usage-totals'):
            self.assertTrue(self._call(path, 'GET'), path)
            self.assertTrue(self._call(path + '/', 'GET'), '尾斜杠也要放行')

    def test_write_always_denied(self) -> None:
        """任何写方法都必须 403——这是这把令牌的核心边界。"""
        cases = [
            ('/api/keys', 'POST'),
            ('/api/keys/1', 'PATCH'),
            ('/api/keys/1', 'DELETE'),
            ('/api/settings/panel', 'POST'),
            ('/api/accounts/x.json', 'DELETE'),
            ('/api/models', 'PUT'),
        ]
        for path, method in cases:
            with self.assertRaises(HTTPException) as ctx:
                self._call(path, method)
            self.assertEqual(ctx.exception.status_code, 403, f'{method} {path}')

    def test_credential_and_user_surfaces_denied(self) -> None:
        for path in ('/api/tokens', '/api/tokens/1', '/api/users',
                     '/api/users/gateway-admin'):
            with self.assertRaises(HTTPException) as ctx:
                self._call(path, 'GET')
            self.assertEqual(ctx.exception.status_code, 403, path)

    def test_unknown_path_denied(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call('/api/whatever', 'GET')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_keys_scope_unchanged(self) -> None:
        """scope=keys 的原有收敛不变：能读密钥、不能读统计。"""
        self.assertTrue(self._call('/api/keys', 'GET', scope='keys'))
        with self.assertRaises(HTTPException) as ctx:
            self._call('/api/stats/summary', 'GET', scope='keys')
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn('密钥管理', ctx.exception.detail)


class AuditLogReadScopeDependencyTest(unittest.TestCase):
    """`require_admin_or_read_scope`：管理员或只读服务令牌可读；viewer 不放行。"""

    def _call(self, role: str, scope: str | None) -> dict:
        user = {'username': 'u', 'role': role}
        if scope is not None:
            user['scope'] = scope
        return security.require_admin_or_read_scope(user)

    def test_admin_allowed(self) -> None:
        self.assertTrue(self._call('admin', None))

    def test_read_scope_allowed(self) -> None:
        self.assertTrue(self._call('viewer', 'read'))

    def test_viewer_and_other_scopes_denied(self) -> None:
        for scope in (None, 'readonly', 'keys'):
            with self.subTest(scope=scope):
                with self.assertRaises(HTTPException) as ctx:
                    self._call('viewer', scope)
                self.assertEqual(ctx.exception.status_code, 403)


class ReadScopeHttpTest(unittest.TestCase):
    """真 app + 真令牌：只读令牌能读全部只读族，写与凭据面一律 403。"""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from server import config, db, tokensvc

        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'read-scope.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('admin-pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)
        cls.read_token = tokensvc.create_token('qtg-read', 'read')['token']
        cls.keys_token = tokensvc.create_token('qtg-keys', 'keys')['token']
        cls.ro_token = tokensvc.create_token('qtg-ro', 'readonly')['token']
        import time as _t
        cls.since = '2020-01-01'
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens,'
            ' completion_tokens, credit, realm) VALUES (?,?,?,?,?,?,?,?)',
            (_t.strftime('%Y-%m-%d'), 1, 'm', 3, 30, 15, 0.5, 'cn'))

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:  # pragma: no cover - Windows 上偶发占用
            pass

    def setUp(self) -> None:
        self.c.cookies.clear()
        security._fail.clear()
        security._user_fail.clear()

    @staticmethod
    def _bearer(token: str) -> dict:
        return {'Authorization': f'Bearer {token}'}

    # ── 只读令牌能读 ───────────────────────────────────────
    def test_read_token_reads_audit_logs(self) -> None:
        """R1-A：审计日志对 scope=read 打开（此前 require_admin 会 403）。"""
        r = self.c.get('/api/audit-logs', headers=self._bearer(self.read_token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('items', r.json())
        # 人类只读令牌（readonly → viewer）仍被拒绝：这不是给人类放权限
        r2 = self.c.get('/api/audit-logs', headers=self._bearer(self.ro_token))
        self.assertEqual(r2.status_code, 403, r2.text)
        # keys 令牌也不能读审计面
        r3 = self.c.get('/api/audit-logs', headers=self._bearer(self.keys_token))
        self.assertEqual(r3.status_code, 403, r3.text)

    def test_read_token_reads_usage_family(self) -> None:
        r = self.c.get('/api/read-api/usage-matrix', params={'since': self.since},
                       headers=self._bearer(self.read_token))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['version'], 2)
        self.assertTrue(body['rows'], '非空 fixture 必须返回行')
        self.assertIn('failures', body)
        r2 = self.c.get('/api/read-api/usage-totals',
                        headers=self._bearer(self.read_token))
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertGreaterEqual(r2.json()['requests'], 3)

    def test_read_token_reads_version_contract(self) -> None:
        r = self.c.get('/api/read-api/version', headers=self._bearer(self.read_token))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['version'], 2)
        self.assertTrue(body['capabilities']['usage'])
        self.assertIn('/api/read-api/usage-matrix', body['endpoints']['usage'])

    def test_read_token_reads_stats_and_keys(self) -> None:
        for path in ('/api/stats/summary', '/api/keys', '/api/logs',
                     '/api/settings/panel'):
            with self.subTest(path=path):
                r = self.c.get(path, headers=self._bearer(self.read_token))
                self.assertEqual(r.status_code, 200, f'{path}: {r.text}')

    # ── 只读令牌不能写、不能读凭据面 ───────────────────────
    def test_read_token_cannot_write(self) -> None:
        cases = [
            ('POST', '/api/keys', {'name': 'x'}),
            ('DELETE', '/api/keys/1', None),
            ('POST', '/api/logs/clear', None),
            ('POST', '/api/stats/repair-usage', None),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                r = self.c.request(method, path, json=body,
                                   headers=self._bearer(self.read_token))
                self.assertEqual(r.status_code, 403, f'{method} {path}: {r.text}')

    def test_read_api_usage_endpoints_are_get_only(self) -> None:
        """usage 族只有 GET 路由：写方法在路由层就是 405，连依赖都不会执行。"""
        for method in ('POST', 'PATCH', 'DELETE', 'PUT'):
            with self.subTest(method=method):
                r = self.c.request(method, '/api/read-api/usage-matrix',
                                   json={}, headers=self._bearer(self.read_token))
                self.assertIn(r.status_code, (403, 405), r.text)

    def test_read_token_cannot_read_credential_surface(self) -> None:
        for path in ('/api/tokens', '/api/users'):
            with self.subTest(path=path):
                r = self.c.get(path, headers=self._bearer(self.read_token))
                self.assertEqual(r.status_code, 403, f'{path}: {r.text}')


if __name__ == '__main__':
    unittest.main()
