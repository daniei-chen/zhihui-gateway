"""v1.2.48：外部平台（Trae / Qoder）的账号可见性。

网关的账号管理只覆盖 workbuddy（`WB_AUTH_DIR`）。Trae 与 Qoder 的账号在各自
容器内，网关读不到 —— 界面上对它们点"刷新"实际是空操作。

2026-11-14 排查确认：这两个平台连续六天零调用（Qoder 64 次、Trae 58 次，
都停在 11-08），同期 workbuddy 15 万次。不是它们坏了，是**根本没有被使用的路径**。

这个模块只做两件事：读出容器内的账号状态（只读），以及请求容器刷新它自己的
token。网关不直接改容器内的 token。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import externalplatforms as ep


class RedactionTests(unittest.TestCase):
    def test_tokens_are_never_returned(self):
        entry = {'uid': 'u1', 'nickname': 'n', 'accessToken': 'dt-SECRET',
                 'refreshToken': 'drt-SECRET', 'expiresAt': 4102444800}
        out = ep._redact(entry)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn('SECRET', blob, '凭据泄漏到了响应里')
        self.assertNotIn('accessToken', blob)
        self.assertNotIn('refreshToken', blob)

    def test_it_reports_whether_a_refresh_token_exists(self):
        self.assertTrue(ep._redact({'uid': 'u', 'refreshToken': 'x'})['has_refresh_token'])
        self.assertFalse(ep._redact({'uid': 'u', 'refreshToken': ''})['has_refresh_token'])

    def test_days_left_is_none_when_unknown(self):
        """拿不到过期时间时必须返回 None —— 不能返回 0 或负数冒充"已过期"。"""
        for entry in ({}, {'expiresAt': None}, {'expiresAt': 0}, {'expiresAt': 'x'}):
            self.assertIsNone(ep._redact({'uid': 'u', **entry})['days_left'], entry)

    def test_expiring_soon_uses_the_threshold(self):
        soon = ep._redact({'uid': 'u', 'expiresAt': __import__('time').time() + 3600})
        self.assertTrue(soon['expiring_soon'])
        far = ep._redact({'uid': 'u', 'expiresAt': __import__('time').time() + 86400 * 60})
        self.assertFalse(far['expiring_soon'])


class QoderReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(ep, 'QODER_ACCOUNTS_DIR', self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name, payload):
        (self.dir / name).write_text(json.dumps(payload), encoding='utf-8')

    def test_missing_directory_is_reported_not_faked(self):
        with mock.patch.object(ep, 'QODER_ACCOUNTS_DIR', self.dir / 'nope'):
            result = ep.qoder_accounts()
        self.assertFalse(result['available'])
        self.assertIn('reason', result)
        self.assertEqual(result['accounts'], [])

    def test_accounts_are_read_and_redacted(self):
        self._write('a.json', {'uid': 'u1', 'nickname': '轻松熊', 'realm': 'cn',
                               'refreshToken': 'drt-x', 'expiresAt': 4102444800,
                               'credits': {'remain': 594, 'size': 2900}})
        result = ep.qoder_accounts()
        self.assertTrue(result['available'])
        self.assertEqual(result['count'], 1)
        account = result['accounts'][0]
        self.assertEqual(account['nickname'], '轻松熊')
        self.assertEqual(account['credits_remain'], 594)
        self.assertNotIn('drt-x', json.dumps(result, ensure_ascii=False))

    def test_meta_files_are_skipped(self):
        self._write('settings.json', {'panel_password_hash': 'x'})
        self._write('machine_identity.json', {'machine': 'y'})
        self._write('real.json', {'uid': 'u1', 'nickname': 'n'})
        result = ep.qoder_accounts()
        self.assertEqual(result['count'], 1, 'settings/machine_identity 不是账号')

    def test_broken_json_is_skipped_without_failing_the_whole_read(self):
        (self.dir / 'broken.json').write_text('{not json', encoding='utf-8')
        self._write('good.json', {'uid': 'u1', 'nickname': 'n'})
        result = ep.qoder_accounts()
        self.assertTrue(result['available'])
        self.assertEqual(result['count'], 1)

    def test_expiring_soon_is_counted(self):
        import time
        self._write('a.json', {'uid': 'u1', 'expiresAt': time.time() + 3600})
        self._write('b.json', {'uid': 'u2', 'expiresAt': time.time() + 86400 * 90})
        result = ep.qoder_accounts()
        self.assertEqual(result['expiring_soon'], 1)


class TraeReadingTests(unittest.TestCase):
    def test_missing_docker_is_reported_not_faked(self):
        with mock.patch.object(ep, '_docker_exec', return_value=(127, 'docker 命令不存在')):
            result = ep.trae_accounts()
        self.assertFalse(result['available'])
        self.assertIn('docker', result['reason'])
        self.assertEqual(result['accounts'], [])

    def test_unreadable_database_is_reported(self):
        with mock.patch.object(ep, '_docker_exec', return_value=(0, 'NO_SQLITE')):
            result = ep.trae_accounts()
        self.assertFalse(result['available'])
        self.assertIn('reason', result)


class RefreshNeverTouchesContainerTokensTests(unittest.TestCase):
    def test_unknown_platform_is_rejected(self):
        self.assertFalse(ep.refresh('doubao')['ok'])

    def test_refresh_refuses_rather_than_pretending(self):
        """两个平台的刷新入口都还没接线 —— 必须如实说"没接"，不能返回 ok。"""
        for platform in ('qoder', 'trae'):
            result = ep.refresh(platform)
            self.assertFalse(result['ok'], f'{platform} 谎报刷新成功')
            self.assertIn('reason', result)

    def test_reading_never_writes_into_the_accounts_directory(self):
        """读取路径必须保持只读。

        v1.2.49 起模块**故意**有了一个写入口（刷新后的原子写回），所以这里不再
        笼统断言"整个模块不写文件"——那会锁住一个已经不再成立的约束。改成精确的：
        **只有刷新相关的那两个函数**可以写，读取函数一律不许。
        """
        source = (Path(__file__).parents[1] / 'externalplatforms.py').read_text(encoding='utf-8')

        def body_of(name: str) -> str:
            start = source.index(f'def {name}(')
            tail = source[start + 1:]
            nxt = tail.find('\ndef ')
            return source[start:start + 1 + (nxt if nxt != -1 else 4000)]

        for reader in ('qoder_accounts', 'trae_accounts', 'snapshot', '_redact', '_days_left'):
            body = body_of(reader)
            for forbidden in ('write_text(', 'write_bytes(', 'os.replace(', 'os.unlink('):
                self.assertNotIn(forbidden, body,
                                 f'读取函数 {reader} 里出现了写入调用 {forbidden}')

    def test_only_the_refresh_path_may_write(self):
        source = (Path(__file__).parents[1] / 'externalplatforms.py').read_text(encoding='utf-8')
        writers = [name for name in ('_write_account_atomically', 'refresh_qoder_account')
                   if f'def {name}(' in source]
        self.assertEqual(len(writers), 2, '写入口应当恰好是刷新相关的两个函数')
        # 写入口本身必须带备份
        start = source.index('def refresh_qoder_account(')
        self.assertIn('backup', source[start:start + 4000])


class SnapshotShapeTests(unittest.TestCase):
    def test_snapshot_always_carries_both_platforms(self):
        with mock.patch.object(ep, 'qoder_accounts', return_value={'available': False}):
            with mock.patch.object(ep, 'trae_accounts', return_value={'available': False}):
                snap = ep.snapshot()
        self.assertIn('qoder', snap)
        self.assertIn('trae', snap)
        self.assertIn('sampled_at', snap)

    def test_snapshot_does_not_hide_unavailable_platforms(self):
        with mock.patch.object(ep, 'qoder_accounts',
                               return_value={'available': False, 'reason': 'x'}):
            with mock.patch.object(ep, 'trae_accounts',
                                   return_value={'available': False, 'reason': 'y'}):
                snap = ep.snapshot()
        self.assertFalse(snap['qoder']['available'])
        self.assertFalse(snap['trae']['available'])


if __name__ == '__main__':
    unittest.main()
