"""new-api 余额协议（GET /api/usage/token）回归测试。

关注点是**契约**：形状、单位选择、错误信封。这些正是面板能不能用起来的
全部依据 —— 少一个字段、错一种单位，对面就只显示「未知」或显示一个错数字。

全部离线运行，不访问真实上游。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402


class UsageTokenProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'b.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _key(self, **kw) -> dict:
        return keysvc.create_key('余额测试', **kw)

    def _get(self, token: str | None, path: str = '/api/usage/token'):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        return self.client.get(path, headers=headers)

    # ── 形状 ────────────────────────────────────────────────────────────

    def test_both_trailing_slash_forms_work(self) -> None:
        """面板实现有带斜杠、有不带斜杠的，两条都得通。"""
        k = self._key()
        for path in ('/api/usage/token', '/api/usage/token/'):
            with self.subTest(path=path):
                r = self._get(k['key'], path)
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()['code'])

    def test_success_envelope_matches_documented_shape(self) -> None:
        k = self._key()
        body = self._get(k['key']).json()
        self.assertIs(body['code'], True)
        self.assertEqual(body['message'], 'ok')
        data = body['data']
        self.assertEqual(data['object'], 'token_usage')
        self.assertEqual(data['name'], '余额测试')
        for field in ('total_granted', 'total_used', 'total_available', 'expires_at'):
            self.assertIsInstance(data[field], (int, float), field)
        self.assertIsInstance(data['unlimited_quota'], bool)
        self.assertIsInstance(data['model_limits'], dict)
        self.assertIsInstance(data['model_limits_enabled'], bool)

    def test_no_extra_keys_in_data(self) -> None:
        """不多塞自己的字段：面板按固定 schema 解析，多给的键没有意义。"""
        k = self._key()
        data = self._get(k['key']).json()['data']
        self.assertEqual(set(data), {
            'object', 'name', 'total_granted', 'total_used', 'total_available',
            'unlimited_quota', 'model_limits', 'model_limits_enabled', 'expires_at',
        })

    # ── 单位选择 ────────────────────────────────────────────────────────

    def test_credit_quota_wins_when_present(self) -> None:
        k = self._key(quota_credit=100.0)
        db.execute('UPDATE api_keys SET used_credit=30.0 WHERE id=?', (k['id'],))
        data = self._get(k['key']).json()['data']
        self.assertAlmostEqual(data['total_granted'], 100.0)
        self.assertAlmostEqual(data['total_used'], 30.0)
        self.assertAlmostEqual(data['total_available'], 70.0)
        self.assertFalse(data['unlimited_quota'])

    def test_token_quota_used_when_no_credit(self) -> None:
        k = self._key(quota=1000)
        db.execute('UPDATE api_keys SET used_tokens=250 WHERE id=?', (k['id'],))
        data = self._get(k['key']).json()['data']
        self.assertEqual(data['total_granted'], 1000)
        self.assertEqual(data['total_used'], 250)
        self.assertEqual(data['total_available'], 750)
        self.assertFalse(data['unlimited_quota'])

    def test_unlimited_when_no_quota_at_all(self) -> None:
        k = self._key()
        data = self._get(k['key']).json()['data']
        self.assertTrue(data['unlimited_quota'])
        self.assertEqual(data['total_granted'], 0)
        self.assertEqual(data['total_available'], 0)

    def test_never_negative_available(self) -> None:
        """超额使用时「可用」是 0，不是负数 —— 面板会把负数当异常。"""
        k = self._key(quota_credit=10.0)
        db.execute('UPDATE api_keys SET used_credit=25.0 WHERE id=?', (k['id'],))
        data = self._get(k['key']).json()['data']
        self.assertEqual(data['total_available'], 0)
        self.assertAlmostEqual(data['total_used'], 25.0)

    # ── 模型与到期 ──────────────────────────────────────────────────────

    def test_model_limits_reflect_whitelist(self) -> None:
        k = self._key(models=['glm-5.3-flash', 'claude-sonnet-4'])
        data = self._get(k['key']).json()['data']
        self.assertTrue(data['model_limits_enabled'])
        self.assertEqual(data['model_limits'],
                         {'glm-5.3-flash': True, 'claude-sonnet-4': True})

    def test_empty_whitelist_disables_model_limits(self) -> None:
        k = self._key()
        data = self._get(k['key']).json()['data']
        self.assertFalse(data['model_limits_enabled'])
        self.assertEqual(data['model_limits'], {})

    def test_expires_at_passthrough_and_zero_means_never(self) -> None:
        k = self._key()
        self.assertEqual(self._get(k['key']).json()['data']['expires_at'], 0)

        stamp = int(time.time()) + 86400
        k2 = self._key(expires_at=stamp)
        self.assertEqual(self._get(k2['key']).json()['data']['expires_at'], stamp)

    # ── 错误信封 ────────────────────────────────────────────────────────

    def test_missing_key_uses_newapi_envelope(self) -> None:
        """必须是 new-api 的 success/message —— 面板解析不了 OpenAI 的 error。"""
        r = self._get(None)
        self.assertEqual(r.status_code, 401)
        body = r.json()
        self.assertIs(body['success'], False)
        self.assertIn('message', body)
        self.assertNotIn('error', body)

    def test_invalid_key_reports_token_not_found(self) -> None:
        r = self._get('wbk_definitely-not-a-real-key')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['message'], 'token not found')

    def test_disabled_key_is_403(self) -> None:
        k = self._key()
        keysvc.update_key(k['id'], {'enabled': False})
        r = self._get(k['key'])
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['message'], 'token disabled')

    def test_expired_key_is_403(self) -> None:
        k = self._key(expires_at=int(time.time()) - 60)
        r = self._get(k['key'])
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['message'], 'token expired')

    # ── 与 /v1/usage 的关系 ─────────────────────────────────────────────

    def test_quota_block_added_to_v1_usage_without_credit_fields(self) -> None:
        """/v1/usage 多了额度自查，但**仍然不出现 credit 字段**（既有对外契约）。"""
        k = self._key(quota=1000, quota_credit=50.0)
        body = self.client.get('/v1/usage',
                               headers={'Authorization': f'Bearer {k["key"]}'}).json()
        self.assertIn('quota', body)
        self.assertEqual(body['quota']['unit'], 'token')
        self.assertEqual(body['quota']['granted'], 1000)

        def walk(node) -> None:
            if isinstance(node, dict):
                for name, value in node.items():
                    self.assertNotIn('credit', name.lower())
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(body)

    def test_quota_exhausted_key_can_still_read_balance(self) -> None:
        """配额打满时最需要看余额 —— 这条端点不能因为超额而拒绝。"""
        k = self._key(quota=100)
        db.execute('UPDATE api_keys SET used_tokens=100 WHERE id=?', (k['id'],))
        r = self._get(k['key'])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['total_available'], 0)


if __name__ == '__main__':
    unittest.main()
