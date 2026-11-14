"""v1.2.51/v1.2.52：Trae 账号元数据可见。

## 一条必须记录在案的自我修正

v1.2.51 最初在这里给出 `needs_login`，依据是「`jwt` 与 `refresh_token` 都为空」。
**那个判断是错的。**

v1.2.52 实测：直接向引擎发一次请求，成功返回 200，同时 —

- `api_usage` 记下了这次调用（`{"total":{"requests":2,"ok":2,"errors":0}}`）；
- `accounts` 表的 `updated_at` 被刷新到调用那一刻；
- `conf/vault.stronghold`（加密存储）同步更新。

**真正的凭据在加密存储里，不在这个表。** 表里的 `jwt` 为空是正常的。

所以服务端不再给「要不要重新登录」的结论，只报事实字段。这些用例跟着改成
断言「不推断」——而不是断言某个推断值。
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
REAL = {
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


class NoLoginInferenceTests(unittest.TestCase):
    """核心：不能凭「令牌字段为空」推断需要重新登录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _read(self, accounts, api_keys=0):
        path = build_db(self.dir, accounts, api_keys)
        with mock.patch.object(ep, 'TRAE_DB', path):
            return ep.trae_accounts()

    def test_no_needs_login_field_is_reported(self):
        result = self._read([REAL])
        self.assertNotIn('needs_login', result,
                         '又凭字段空值给出「需要重新登录」的结论了')
        for account in result['accounts']:
            self.assertNotIn('needs_login', account)

    def test_empty_token_fields_are_reported_as_metadata_only(self):
        """字段名要说清是「元数据表里的值」，不是「有没有凭据」。"""
        account = self._read([REAL])['accounts'][0]
        self.assertIn('jwt_in_metadata', account)
        self.assertIn('refresh_token_in_metadata', account)
        self.assertFalse(account['jwt_in_metadata'])
        self.assertFalse(account['refresh_token_in_metadata'])

    def test_the_note_explains_the_encrypted_store(self):
        result = self._read([REAL])
        note = result['note'] or ''
        self.assertIn('加密存储', note)
        self.assertIn('不代表', note)
        self.assertNotIn('重新登录', note, '不要再给重新登录的建议')

    def test_metadata_counters_do_not_imply_a_failed_refresh(self):
        account = self._read([REAL])['accounts'][0]
        self.assertEqual(account['refresh_token_fails'], 0)
        self.assertFalse(account['refresh_token_invalid'])

    def test_tokens_are_never_returned(self):
        payload = dict(REAL, jwt='JWT-SECRET', refresh_token='RT-SECRET')
        blob = json.dumps(self._read([payload]), ensure_ascii=False)
        self.assertNotIn('JWT-SECRET', blob)
        self.assertNotIn('RT-SECRET', blob)

    def test_invalid_flag_is_mapped_truthfully(self):
        payload = dict(REAL, refresh_token_invalid=1)
        self.assertTrue(self._read([payload])['accounts'][0]['refresh_token_invalid'])

    def test_metadata_is_preserved(self):
        account = self._read([REAL])['accounts'][0]
        self.assertEqual(account['nickname'], '前程如风')
        self.assertEqual(account['uid'], UID)
        self.assertEqual(account['added_at'], '2026-10-19T07:51:55')
        self.assertEqual(account['updated_at'], '2026-11-14T05:30:27+08:00')

    def test_api_key_count_is_reported(self):
        self.assertEqual(self._read([REAL], api_keys=2)['api_keys'], 2)


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
        source = (Path(__file__).parents[1] / 'externalplatforms.py').read_text(encoding='utf-8')
        start = source.index('def trae_accounts(')
        self.assertIn('mode=ro', source[start:start + 1600])

    def test_the_correction_is_documented(self):
        """把这次判断错误写进代码，避免以后有人又按字段空值下结论。"""
        source = (Path(__file__).parents[1] / 'externalplatforms.py').read_text(encoding='utf-8')
        self.assertIn('那个判断是错的', source)
        self.assertIn('vault.stronghold', source)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_carries_both_platforms_even_when_unavailable(self):
        with mock.patch.object(ep, 'qoder_accounts', return_value={'available': False}):
            with mock.patch.object(ep, 'trae_accounts', return_value={'available': False}):
                snap = ep.snapshot()
        self.assertIn('qoder', snap)
        self.assertIn('trae', snap)


if __name__ == '__main__':
    unittest.main()
