"""v1.2.49：Qoder 账号刷新 —— 网关侧完成，不走容器面板。

容器的面板密码是 scrypt 哈希，网关拿不到明文，所以它的 `/panel/login` 用不了。
但刷新不需要面板会话：账号文件里有 refresh token（root 可读），上游刷新端点直接
可调（实测 2026-11-14 返回 200），新令牌再原子写回。

写回的安全性是这个模块的重点：备份、保留属主与权限、临时文件替换、失败不破坏原文件。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import externalplatforms as ep

UID = '019ee5d2-482e-7924-b38c-329cd5133c4b'

ACCOUNT = {
    'uid': UID,
    'nickname': '轻松熊',
    'realm': 'cn',
    'accessToken': 'dt-OLD',
    'refreshToken': 'drt-OLD',
    'expiresAt': 1797228012,
    'enabled': True,
    'plan': 'Free',
    'credits': {'remain': 594, 'size': 2900},
    'lastCheckin': '2026-11-14 21:00:02',
}

UPSTREAM_OK = json.dumps({
    'device_token': 'dt-NEW',
    'refresh_token': 'drt-NEW',
    'token_type': 'Bearer',
    'expires_at': '2026-12-14T14:07:55Z',
    'refresh_token_expires_at': '2027-11-09T14:07:55Z',
}).encode()


def fake_urlopen(payload=UPSTREAM_OK, status=200):
    class Response:
        def __init__(self):
            self.status = status

        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    return lambda request, timeout=0: Response()


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / f'{UID}.json'
        self.path.write_text(json.dumps(ACCOUNT, ensure_ascii=False), encoding='utf-8')
        patcher = mock.patch.object(ep, 'QODER_ACCOUNTS_DIR', self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _payload(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_success_updates_the_three_token_fields(self):
        with mock.patch('urllib.request.urlopen', fake_urlopen()):
            result = ep.refresh_qoder_account(UID)
        self.assertTrue(result['ok'], result)
        payload = self._payload()
        self.assertEqual(payload['accessToken'], 'dt-NEW')
        self.assertEqual(payload['refreshToken'], 'drt-NEW')

    def test_other_fields_are_preserved(self):
        """容器依赖 plan / credits / lastCheckin 等字段，刷新不能把它们丢掉。"""
        with mock.patch('urllib.request.urlopen', fake_urlopen()):
            ep.refresh_qoder_account(UID)
        payload = self._payload()
        self.assertEqual(payload['nickname'], '轻松熊')
        self.assertEqual(payload['plan'], 'Free')
        self.assertEqual(payload['credits']['remain'], 594)
        self.assertEqual(payload['lastCheckin'], '2026-11-14 21:00:02')
        self.assertIs(payload['enabled'], True)

    def test_expires_at_is_converted_to_epoch(self):
        """上游给 ISO 时间串，账号文件存 epoch 秒 —— 必须换算。"""
        with mock.patch('urllib.request.urlopen', fake_urlopen()):
            result = ep.refresh_qoder_account(UID)
        payload = self._payload()
        self.assertIsInstance(payload['expiresAt'], int)
        self.assertGreater(payload['expiresAt'], 1797228012, '新过期时间应当更晚')
        self.assertIsNotNone(result['days_left'])

    def test_a_backup_is_written_before_replacing(self):
        with mock.patch('urllib.request.urlopen', fake_urlopen()):
            result = ep.refresh_qoder_account(UID)
        self.assertTrue(result['ok'], result)
        backups = list(self.dir.glob('*.bak-*'))
        self.assertEqual(len(backups), 1, '没有留备份')
        self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8'))['accessToken'],
                         'dt-OLD', '备份里应当是旧令牌')

    def test_no_token_in_the_response(self):
        with mock.patch('urllib.request.urlopen', fake_urlopen()):
            result = ep.refresh_qoder_account(UID)
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('dt-NEW', blob, '响应泄漏了新令牌')
        self.assertNotIn('drt-NEW', blob)

    def test_file_mode_and_owner_are_preserved(self):
        os.chmod(self.path, 0o600)
        before = self.path.stat()
        with mock.patch('urllib.request.urlopen', fake_urlopen()):
            ep.refresh_qoder_account(UID)
        after = self.path.stat()
        self.assertEqual(after.st_mode & 0o7777, before.st_mode & 0o7777,
                         '权限被改动了，容器可能读不到')
        self.assertEqual(after.st_uid, before.st_uid)

    def test_unknown_account_is_rejected(self):
        result = ep.refresh_qoder_account('does-not-exist')
        self.assertFalse(result['ok'])
        self.assertIn('不存在', result['reason'])

    def test_account_without_refresh_token_says_so(self):
        broken = dict(ACCOUNT)
        broken['refreshToken'] = ''
        self.path.write_text(json.dumps(broken), encoding='utf-8')
        result = ep.refresh_qoder_account(UID)
        self.assertFalse(result['ok'])
        self.assertIn('refreshToken', result['reason'])

    def test_unparseable_file_is_rejected(self):
        self.path.write_text('{not json', encoding='utf-8')
        result = ep.refresh_qoder_account(UID)
        self.assertFalse(result['ok'])
        self.assertIn('不可解析', result['reason'])


class RefreshFailureDoesNotCorruptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / f'{UID}.json'
        self.path.write_text(json.dumps(ACCOUNT, ensure_ascii=False), encoding='utf-8')
        patcher = mock.patch.object(ep, 'QODER_ACCOUNTS_DIR', self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_upstream_http_error_leaves_the_file_untouched(self):
        import urllib.error

        def boom(request, timeout=0):
            raise urllib.error.HTTPError('u', 401, 'unauth', {}, None)

        before = self.path.read_bytes()
        with mock.patch('urllib.request.urlopen', boom):
            result = ep.refresh_qoder_account(UID)
        self.assertFalse(result['ok'])
        self.assertEqual(self.path.read_bytes(), before, '失败时不该动原文件')

    def test_unreachable_upstream_leaves_the_file_untouched(self):
        def boom(request, timeout=0):
            raise OSError('network down')

        before = self.path.read_bytes()
        with mock.patch('urllib.request.urlopen', boom):
            result = ep.refresh_qoder_account(UID)
        self.assertFalse(result['ok'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_upstream_returning_no_token_leaves_the_file_untouched(self):
        before = self.path.read_bytes()
        with mock.patch('urllib.request.urlopen', fake_urlopen(b'{"ok": true}')):
            result = ep.refresh_qoder_account(UID)
        self.assertFalse(result['ok'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_non_json_upstream_leaves_the_file_untouched(self):
        before = self.path.read_bytes()
        with mock.patch('urllib.request.urlopen', fake_urlopen(b'<html>oops</html>')):
            result = ep.refresh_qoder_account(UID)
        self.assertFalse(result['ok'])
        self.assertEqual(self.path.read_bytes(), before)


class RouteGuardTests(unittest.TestCase):
    def test_path_traversal_is_refused(self):
        """账号标识来自 URL，必须挡住 ../ 之类的穿越。"""
        source = (Path(__file__).parents[1] / 'routers' / 'accounts.py').read_text(encoding='utf-8')
        start = source.index('def qoder_account_refresh(')
        body = source[start:start + 900]
        self.assertIn("'/' in uid", body)
        self.assertIn("uid.startswith('.')", body)


if __name__ == '__main__':
    unittest.main()
