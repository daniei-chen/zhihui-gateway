"""R3-L5：Manager 会话签名密钥的**过渡窗口**（`secret_previous`）。

R0 复核 RV04/G4-2 指出：会话密钥是单值，换 secret = 全部会话立即失效，没有过渡窗口。
本文件钉住新行为：

1. 轮换期间（`secret` 换新、`secret_previous` 保留旧值）**旧 Cookie 仍能通过**——
   已登录的标签页不会被强制重登；
2. 新会话一律用**当前** `secret` 签发（不会把流量钉在旧密钥上）；
3. 删掉 `secret_previous` 后，旧 Cookie **立即**失效（过渡期结束即收口）；
4. `secret_previous` 非字符串/为空时忽略（不把坏配置当成"接受任意签名"）。

跑法：python -X utf8 tests/zhihui_harness/run_tests.py --target manager --keep \
        --test-path=server/tests/test_session_secret_rotation.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402
from server.main import app  # noqa: E402

OLD_SECRET = 'old-secret-' + 'a' * 52
NEW_SECRET = 'new-secret-' + 'b' * 52
PASSWORD = 'TestPw123!'


class SessionSecretRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = root / 'rotation.db'
        config.USERS_FILE = root / 'users.json'
        config.STATIC_DIR = root / 'no-static'
        db._conn = None
        db.connect()
        self.client = TestClient(app)
        security._fail.clear()
        security._user_fail.clear()
        self._write_users(OLD_SECRET)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig
        security._fail.clear()
        security._user_fail.clear()
        self._tmp.cleanup()

    # ── 工具 ─────────────────────────────────────────────
    def _write_users(self, secret: str, previous: str | None = None) -> None:
        data = {
            'secret': secret,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash(PASSWORD), 'sv': 0}],
            'api_keys': [],
        }
        if previous is not None:
            data['secret_previous'] = previous
        security.save_users(data)

    def _login(self) -> str:
        r = self.client.post('/api/login', json={'username': 'admin', 'password': PASSWORD},
                             headers={'Origin': 'http://testserver'})
        self.assertEqual(r.status_code, 200, r.text)
        # 登录响应可能给同一个 Cookie 名写多个 set-cookie（不同 path），
        # httpx 的 cookie jar 会因重名报 CookieConflict —— 这里直接从响应头取，
        # 并显式用 Cookie 头发送，避免 cookie jar 的 path/重名干扰。
        prefix = config.COOKIE_NAME + '='
        headers = [h for h in r.headers.get_list('set-cookie') if h.startswith(prefix)]
        self.assertTrue(headers, '登录响应里没有会话 Cookie')
        self.client.cookies.clear()
        return headers[0].split(';', 1)[0][len(prefix):]

    def _me(self, cookie: str | None):
        self.client.cookies.clear()
        headers = {'Cookie': f'{config.COOKIE_NAME}={cookie}'} if cookie else {}
        return self.client.get('/api/me', headers=headers)

    # ── 用例 ─────────────────────────────────────────────
    def test_old_session_survives_rotation_and_dies_when_previous_removed(self) -> None:
        old_cookie = self._login()
        self.assertEqual(self._me(old_cookie).status_code, 200)

        # 轮换：新 secret + 旧值保留在 secret_previous
        self._write_users(NEW_SECRET, previous=OLD_SECRET)
        self.assertEqual(self._me(old_cookie).status_code, 200,
                         '过渡窗口内旧 Cookie 应仍有效（否则就是"强制全员重登"）')

        # 新登录用新 secret 签发
        new_cookie = self._login()
        self.assertNotEqual(new_cookie, old_cookie)
        self.assertEqual(self._me(new_cookie).status_code, 200)

        # 收口：删掉 secret_previous → 旧 Cookie 立即失效，新 Cookie 不受影响
        self._write_users(NEW_SECRET)
        self.assertEqual(self._me(old_cookie).status_code, 401,
                         '删除 secret_previous 后旧会话必须失效')
        self.assertEqual(self._me(new_cookie).status_code, 200)

    def test_previous_only_affects_verification_not_signing(self) -> None:
        """签发只用当前 secret：即使 secret_previous 存在，新 cookie 也必须是新密钥签的。"""
        self._write_users(NEW_SECRET, previous=OLD_SECRET)
        cookie = self._login()
        # 只保留当前 secret 时，这个新 cookie 仍然有效（说明它没有用旧密钥签）
        self._write_users(NEW_SECRET)
        self.assertEqual(self._me(cookie).status_code, 200)

    def test_invalid_previous_value_is_ignored(self) -> None:
        old_cookie = self._login()
        for bad in ({'secret_previous': ''}, {'secret_previous': None},
                    {'secret_previous': ['not-a-string']}):
            data = {
                'secret': NEW_SECRET,
                'users': [{'username': 'admin', 'role': 'admin',
                           'pwd_hash': security.make_hash(PASSWORD), 'sv': 0}],
                'api_keys': [],
            }
            data.update(bad)
            security.save_users(data)
            self.assertEqual(self._me(old_cookie).status_code, 401,
                             f'坏配置 {bad} 不应被当成可接受的旧密钥')
            # 复原到"当前 secret + 旧值"，继续下一个坏值用例
            self._write_users(NEW_SECRET, previous=OLD_SECRET)
        # 正常过渡值仍有效（证明上面的 401 不是因为 cookie 本身坏了）
        self.assertEqual(self._me(old_cookie).status_code, 200)


if __name__ == '__main__':
    unittest.main()
