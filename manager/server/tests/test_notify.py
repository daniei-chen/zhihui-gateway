"""K46 告警投递层（`server/notify.py`）的行为测试。

为什么值得有：投递是"采集 → 有人知道"之间唯一的环节。它错了不会让任何页面报错，
只会让故障**静默**——而静默正是 S4 要消灭的东西。

钉住五条不变量：
  1. **默认不发**：无配置/配置损坏/未 enabled 时一律 `mode=disabled`；
  2. **file 模式真的落盘**，且每行是合法 JSON、含 schema/name/active/detail；
  3. **http 模式拒绝非 http(s) URL**（不得把 payload 发到任意 scheme）；
  4. **脱敏**：URL userinfo、wbt_/wbk_/sk-/ghp_ 令牌、长 base64 串都要被抹掉；
  5. **永不抛**：配置损坏、URL 非法、目标不可达都只返回 `delivered=False`。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import notify  # noqa: E402


class RedactTest(unittest.TestCase):
    def test_url_userinfo_removed(self) -> None:
        out = notify.redact('connect postgres://user:secret@db:5432/x failed')
        self.assertNotIn('secret', out)
        self.assertIn('***', out)

    def test_token_prefixes_removed(self) -> None:
        for raw in ('wbt_abc123def456ghi', 'wbk_ABCdef012345', 'sk-proj-XYZ123456789',
                    'ghp_abcdefghijklmnop', 'Bearer eyJhbGciOiJIUzI1NiJ9'):
            out = notify.redact(f'upstream said {raw}')
            self.assertNotIn(raw, out, raw)
            self.assertIn('***', out)

    def test_long_base64_removed(self) -> None:
        blob = 'A' * 60
        out = notify.redact(f'authorization {blob}')
        self.assertNotIn(blob, out)

    def test_truncates(self) -> None:
        self.assertLessEqual(len(notify.redact('x' * 2000)), 500)

    def test_named_credentials_in_json_and_key_value_are_redacted(self) -> None:
        for raw in ('{"password":"short-secret"}', "{'access_token': 'short-secret'}",
                    '{"api-key": "short-secret"}', 'refresh_token=short-secret',
                    'AuthCode:short-secret', '{"cookie":"short\\\"secret"}'):
            with self.subTest(raw=raw):
                out = notify.redact(raw)
                self.assertNotIn('short', out)
                self.assertIn('***', out)


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / 'notify.json'
        self.patcher = mock.patch.dict(os.environ, {'WB_NOTIFY_CONFIG': str(self.cfg)})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _write_cfg(self, data) -> None:
        self.cfg.write_text(json.dumps(data), encoding='utf-8')

    def test_missing_config_is_disabled(self) -> None:
        self.assertEqual(notify.configured_mode(), 'disabled')
        res = notify.dispatch('backup_stale', True, 'no freshness')
        self.assertEqual(res['mode'], 'disabled')
        self.assertFalse(res['delivered'])

    def test_corrupt_config_is_disabled(self) -> None:
        self.cfg.write_text('{not json', encoding='utf-8')
        self.assertEqual(notify.configured_mode(), 'disabled')

    def test_enabled_false_is_disabled(self) -> None:
        self._write_cfg({'enabled': False, 'mode': 'file'})
        self.assertEqual(notify.configured_mode(), 'disabled')

    def test_unknown_mode_is_disabled(self) -> None:
        self._write_cfg({'enabled': True, 'mode': 'carrier-pigeon'})
        self.assertEqual(notify.configured_mode(), 'disabled')

    def test_file_mode_writes_jsonl(self) -> None:
        out = Path(self.tmp.name) / 'alerts.jsonl'
        self._write_cfg({'enabled': True, 'mode': 'file', 'file': str(out)})
        res = notify.dispatch('backup_publish', True, 'offsite not verified')
        self.assertTrue(res['delivered'], res)
        lines = out.read_text(encoding='utf-8').strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row['schema'], 'zhihui-alert/1')
        self.assertEqual(row['name'], 'backup_publish')
        self.assertTrue(row['active'])
        self.assertIn('offsite', row['detail'])

    def test_file_mode_redacts_detail(self) -> None:
        out = Path(self.tmp.name) / 'alerts.jsonl'
        self._write_cfg({'enabled': True, 'mode': 'file', 'file': str(out)})
        notify.dispatch('x', True, 'token wbt_deadbeefdeadbeef leaked')
        body = out.read_text(encoding='utf-8')
        self.assertNotIn('wbt_deadbeefdeadbeef', body)

    def test_http_mode_rejects_non_http_url(self) -> None:
        self._write_cfg({'enabled': True, 'mode': 'http', 'url': 'file:///etc/passwd'})
        res = notify.dispatch('x', True, 'd')
        self.assertFalse(res['delivered'])
        self.assertIn('http', (res.get('error') or '').lower())

    def test_http_mode_unreachable_never_raises(self) -> None:
        self._write_cfg({'enabled': True, 'mode': 'http',
                         'url': 'http://127.0.0.1:1/hook', 'timeout_sec': 1})
        res = notify.dispatch('x', True, 'd')   # 不得抛
        self.assertFalse(res['delivered'])

    def test_file_mode_unwritable_dir_never_raises(self) -> None:
        # 指向一个不可创建的路径（父路径是文件）
        blocker = Path(self.tmp.name) / 'blocker'
        blocker.write_text('x', encoding='utf-8')
        self._write_cfg({'enabled': True, 'mode': 'file',
                         'file': str(blocker / 'sub' / 'a.jsonl')})
        res = notify.dispatch('x', True, 'd')
        self.assertFalse(res['delivered'])


class OperationalWiringTest(unittest.TestCase):
    """transition() 进入/恢复时都应调用 dispatch（去重由 transition 自身保证）。"""

    def setUp(self) -> None:
        from server import operational
        self.op = operational
        self.op.alerts.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Path(self.tmp.name) / 'n.json'
        cfg.write_text(json.dumps({'enabled': True, 'mode': 'file',
                                   'file': str(Path(self.tmp.name) / 'a.jsonl')}),
                       encoding='utf-8')
        p = mock.patch.dict(os.environ, {'WB_NOTIFY_CONFIG': str(cfg)})
        p.start()
        self.addCleanup(p.stop)
        self.out = Path(self.tmp.name) / 'a.jsonl'

    def test_enter_and_recover_are_both_delivered(self) -> None:
        self.op.transition('qa_signal', True, 'something wrong')
        self.op.transition('qa_signal', True, 'still wrong')   # 重复进入不重复投递
        lines = [json.loads(x) for x in
                 self.out.read_text(encoding='utf-8').strip().splitlines()]
        self.assertEqual(len(lines), 1, '重复进入不应重复投递')
        self.assertTrue(lines[0]['active'])

        self.op.transition('qa_signal', False, '')
        lines = [json.loads(x) for x in
                 self.out.read_text(encoding='utf-8').strip().splitlines()]
        self.assertEqual(len(lines), 2, '恢复应投递一次')
        self.assertFalse(lines[1]['active'])


if __name__ == '__main__':
    unittest.main()
