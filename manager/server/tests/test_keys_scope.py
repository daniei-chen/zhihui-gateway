"""K01-S2：scope=keys 服务令牌的权限边界测试。

锁定：
1. scope=keys 的令牌可以创建/更新/删除密钥（QTG 作为内部服务代用户操作）；
2. 它**不能**访问账号、设置、认证、用户等高危端点（那里仍是 require_admin）；
3. readonly 仍不能写；admin 行为不变；
4. 该角色不是 admin 的别名——任何需要 admin 的端点都拒绝它。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security, tokensvc  # noqa: E402


class KeysScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'keys-scope.db'
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

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        cls._tmp.cleanup()

    def _token(self, scope: str) -> dict:
        return tokensvc.create_token(f'k01s2-{scope}', scope, None, 'test')

    def _h(self, scope: str) -> dict:
        return {'Authorization': f"Bearer {self._token(scope)['token']}"}

    def test_keys_scope_can_create_and_delete(self) -> None:
        h = self._h('keys')
        r = self.c.post('/api/keys', json={'name': 'svc-key'}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        kid = r.json()['id']
        self.assertTrue(r.json()['key'].startswith('wbk_'))

        r = self.c.patch(f'/api/keys/{kid}', json={'name': 'renamed'}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)

        r = self.c.post(f'/api/keys/{kid}/reset-usage', headers=h)
        self.assertEqual(r.status_code, 200, r.text)

        r = self.c.delete(f'/api/keys/{kid}', headers=h)
        self.assertEqual(r.status_code, 200, r.text)

    def test_keys_scope_can_list(self) -> None:
        r = self.c.get('/api/keys', headers=self._h('keys'))
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_keys_scope_cannot_write_accounts_or_settings(self) -> None:
        """scope=keys 在 role 体系里等同 viewer：读可以，写必须被拒。"""
        h = self._h('keys')
        writes = [
            ('/api/accounts/import', {'payload': {}}),
            ('/api/settings/github-mirror', {'enabled': True}),
        ]
        for path, body in writes:
            r = self.c.post(path, json=body, headers=h)
            self.assertIn(r.status_code, (401, 403, 404, 405, 422),
                          f'{path} -> {r.status_code}')

    def test_keys_scope_cannot_manage_tokens(self) -> None:
        r = self.c.post('/api/tokens', json={'name': 'x', 'scope': 'admin'},
                        headers=self._h('keys'))
        self.assertIn(r.status_code, (401, 403, 404, 405))

    def test_readonly_cannot_create(self) -> None:
        r = self.c.post('/api/keys', json={'name': 'ro-key'}, headers=self._h('readonly'))
        self.assertEqual(r.status_code, 403)

    def test_admin_can_still_create(self) -> None:
        r = self.c.post('/api/keys', json={'name': 'admin-key'}, headers=self._h('admin'))
        self.assertEqual(r.status_code, 200, r.text)

    def test_invalid_scope_still_degrades_to_readonly(self) -> None:
        row = self._token('keys-admin')
        self.assertEqual(row['scope'], 'readonly')
        r = self.c.post('/api/keys', json={'name': 'x'},
                        headers={'Authorization': f"Bearer {row['token']}"})
        self.assertEqual(r.status_code, 403)


if __name__ == '__main__':
    unittest.main()
