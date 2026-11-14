"""v1.2.51：Trae 账号可见性，并明确指出「需要重新登录」。

## 为什么这条重要

2026-11-14 排查 Trae 时，它唯一的账号是这样（宿主数据库实测）：

    jwt = ""                    refresh_token = null
    refresh_token_fails = 0     refresh_token_invalid = 0
    added_at = 2026-10-19       updated_at = 2026-11-14 05:30

凭据是空的，而**失败计数和失效标记都是 0** —— 说明「刷新」这条路径根本没被走到过，
不是「刷新失败」。

只报「账号存在、未过期」会把人引向限流、路由这些错误方向。真正要做的是重新登录。
所以视图里单独给出 `needs_login`，把结论直接摆出来。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import externalplatforms as ep

UID = '2385260569699368'

#: 服务器上读到的真实字段结构（值已脱敏）。
REAL_EMPTY = {
    'name': '前程如风',
    'UserID': UID,
    'jwt': '',
    'refresh_token': None,
    'added_at': '2026-10-19T07:51:55',
    'updated_at': '2026-11-14T05:30:27+08:00',
    'DcID': None,
    'refresh_token_expires_at': None,
    'refresh_token_fails': 0,
    'refresh_token_invalid': 0,
    'auth_saved_at': '2026-11-14T05:30:27+08:00',
}


def build_db(directory: Path, accounts: list[dict], api_keys: int = 0) -> Path:
    path = directory / 'aiwork.sqlite'
    handle = sqlite3.connect(str(path))
    try:
        handle.execute('CREATE TABLE accounts (seq INTEGER PRIMARY KEY AUTOINCREMENT,'
                       ' user_id TEXT UNIQUE, data TEXT NOT NULL)')
        handle.execute('CREATE TABLE api_keys (pk TEXT PRIMARY KEY, data TEXT)')
        for payload in accounts:
            handle.execute('INSERT INTO accounts(user_id, data) VALUES(?, ?)',
                           (payload.get('UserID', 'x'), json.dumps(payload)))
        for index in range(api_keys):
            handle.execute('INSERT INTO api_keys(pk, data) VALUES(?, ?)',
                           (f'k{index}', '{}'))
        handle.commit()
    finally:
        handle.close()
    return path


class TraeAccountReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _read(self, accounts, api_keys=0):
        path = build_db(self.dir, accounts, api_keys)
        with mock.patch.object(ep, 'TRAE_DB', path):
            return ep.trae_accounts()

    def test_empty_credentials_are_flagged_as_needing_login(self):
        """这次回归的核心：凭据为空必须被明确标出来。"""
        result = self._read([REAL_EMPTY])
        self.assertTrue(result['available'], result)
        self.assertEqual(result['count'], 1)
        account = result['accounts'][0]
        self.assertFalse(account['has_jwt'])
        self.assertFalse(account['has_refresh_token'])
        self.assertTrue(account['needs_login'])
        self.assertEqual(result['needs_login'], 1)
        self.assertIn('重新登录', result['note'] or '')

    def test_the_failure_counters_do_not_imply_a_failed_refresh(self):
        """失败计数是 0、失效标记是 0 —— 说明刷新路径没被走到，不是刷新失败。"""
        account = self._read([REAL_EMPTY])['accounts'][0]
        self.assertEqual(account['refresh_token_fails'], 0)
        self.assertFalse(account['refresh_token_invalid'])

    def test_an_account_with_credentials_does_not_need_login(self):
        payload = dict(REAL_EMPTY, jwt='eyJhbGciOi...', refresh_token='rt-xxx')
        account = self._read([payload])['accounts'][0]
        self.assertTrue(account['has_jwt'])
        self.assertTrue(account['has_refresh_token'])
        self.assertFalse(account['needs_login'])

    def test_jwt_only_is_enough_to_skip_the_login_hint(self):
        payload = dict(REAL_EMPTY, jwt='eyJhbGciOi...', refresh_token=None)
        self.assertFalse(self._read([payload])['accounts'][0]['needs_login'])

    def test_tokens_are_never_returned(self):
        payload = dict(REAL_EMPTY, jwt='JWT-SECRET-VALUE', refresh_token='RT-SECRET-VALUE')
        blob = json.dumps(self._read([payload]), ensure_ascii=False)
        self.assertNotIn('JWT-SECRET-VALUE', blob, '响应泄漏了 jwt')
        self.assertNotIn('RT-SECRET-VALUE', blob, '响应泄漏了 refresh token')

    def test_invalid_flag_is_mapped_truthfully(self):
        payload = dict(REAL_EMPTY, refresh_token_invalid=1)
        self.assertTrue(self._read([payload])['accounts'][0]['refresh_token_invalid'])

    def test_metadata_is_preserved(self):
        account = self._read([REAL_EMPTY])['accounts'][0]
        self.assertEqual(account['nickname'], '前程如风')
        self.assertEqual(account['uid'], UID)
        self.assertEqual(account['added_at'], '2026-10-19T07:51:55')
        self.assertEqual(account['updated_at'], '2026-11-14T05:30:27+08:00')

    def test_api_key_count_is_reported(self):
        """有 API Key 但没有账号凭据，正是这台机器的真实状态。"""
        result = self._read([REAL_EMPTY], api_keys=2)
        self.assertEqual(result['api_keys'], 2)
        self.assertEqual(result['needs_login'], 1)


class TraeUnavailableTests(unittest.TestCase):
    def test_missing_database_is_reported_with_a_reason(self):
        with mock.patch.object(ep, 'TRAE_DB', Path('/nonexistent/aiwork.sqlite')):
            result = ep.trae_accounts()
        self.assertFalse(result['available'])
        self.assertIn('reason', result)
        self.assertEqual(result['accounts'], [])

    def test_corrupt_database_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / 'aiwork.sqlite'
            broken.write_bytes(b'not a database at all')
            with mock.patch.object(ep, 'TRAE_DB', broken):
                result = ep.trae_accounts()
            self.assertFalse(result['available'])
            self.assertIn('reason', result)

    def test_it_opens_the_database_read_only(self):
        """只读打开：不能给容器里的库带来写入风险。"""
        source = (Path(__file__).parents[1] / 'externalplatforms.py').read_text(encoding='utf-8')
        start = source.index('def trae_accounts(')
        body = source[start:start + 1600]
        self.assertIn('mode=ro', body)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_carries_both_platforms_even_when_unavailable(self):
        with mock.patch.object(ep, 'qoder_accounts', return_value={'available': False}):
            with mock.patch.object(ep, 'trae_accounts', return_value={'available': False}):
                snap = ep.snapshot()
        self.assertIn('qoder', snap)
        self.assertIn('trae', snap)


if __name__ == '__main__':
    unittest.main()
