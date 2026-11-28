"""Malformed local notification configuration never creates unbounded I/O."""
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import notify


class NotifyConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'notify.json'
        patcher = mock.patch.dict(os.environ, {'WB_NOTIFY_CONFIG': str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_object_json_uses_disabled_default_without_raising(self):
        for value in ([], None, False, True, 1, 'synthetic'):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                self.assertEqual(notify.configured_mode(), 'disabled')
                result = notify.dispatch('synthetic', True)
                self.assertFalse(result['delivered'])
                self.assertEqual(result['mode'], 'disabled')

    def test_invalid_encoding_is_disabled_without_raising(self):
        self.path.write_bytes(b'\xff\xfeinvalid')
        self.assertEqual(notify.configured_mode(), 'disabled')
        self.assertFalse(notify.dispatch('synthetic', True)['delivered'])

    def test_non_boolean_enabled_never_sends(self):
        for enabled in ('false', 'true', 1, [], {'yes': True}):
            with self.subTest(enabled=enabled):
                self.path.write_text(json.dumps({'enabled': enabled, 'mode': 'http', 'url': 'https://synthetic.invalid'}))
                with mock.patch.object(notify.urllib.request, 'urlopen') as send:
                    self.assertFalse(notify.dispatch('synthetic', True)['delivered'])
                send.assert_not_called()

    def test_invalid_and_extreme_timeouts_are_finite_and_bounded(self):
        values = [None, False, True, [], {}, 'not a timeout', 'nan', 'inf',
                  float('nan'), float('inf'), float('-inf'), -1, 0, 10 ** 1000, 1000000]
        with mock.patch.object(notify.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.status = 204
            for value in values:
                with self.subTest(value=str(value)[:40]), mock.patch.object(notify, '_settings', return_value={
                        'enabled': True, 'mode': 'http', 'url': 'https://synthetic.invalid',
                        'timeout_sec': value}):
                    self.assertTrue(notify.dispatch('synthetic', True)['delivered'])
                    timeout = send.call_args.kwargs['timeout']
                    self.assertTrue(math.isfinite(timeout))
                    self.assertEqual(timeout, 3.0)

    def test_valid_short_timeouts_and_minimum_are_preserved(self):
        for value, expected in ((0.25, 0.25), ('1.5', 1.5), (3, 3.0), (0.001, 0.1)):
            with self.subTest(value=value), mock.patch.object(notify.urllib.request, 'urlopen') as send, \
                    mock.patch.object(notify, '_settings', return_value={
                        'enabled': True, 'mode': 'http', 'url': 'https://synthetic.invalid',
                        'timeout_sec': value}):
                send.return_value.__enter__.return_value.status = 200
                self.assertTrue(notify.dispatch('synthetic', True)['delivered'])
                self.assertEqual(send.call_args.kwargs['timeout'], expected)

    def test_dispatch_uses_one_consistent_configuration_snapshot(self):
        config = {'enabled': True, 'mode': 'http', 'url': 'https://synthetic.invalid'}
        with mock.patch.object(notify, '_settings', side_effect=[config, AssertionError('second read')]) as read, \
                mock.patch.object(notify.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.status = 200
            self.assertTrue(notify.dispatch('synthetic', True)['delivered'])
            self.assertEqual(read.call_count, 1)


class AlertRoutingDocTests(unittest.TestCase):
    """`file` 模式的最小示例必须让服务真的写得进去。

    `/opt/notify` 是 root 的 0755，容器里服务以 uid 10001 运行（Dockerfile:155）。
    只写 `config.json` 而不预建输出文件，`dispatch()` 会在 append 时
    PermissionError，每条告警都投递失败；而 `status()` 仍报 `configured=true`，
    静默模式也不再记本地账 —— 比未配置更安静。文档示例要预建输出文件并交给
    服务 UID。
    """

    DOC = (Path(__file__).resolve().parents[2] / 'docs' / 'ALERT-ROUTING.md').read_text(
        encoding='utf-8')

    def test_file_mode_recipe_precreates_the_output_for_the_service_uid(self):
        self.assertIn('alerts.jsonl', self.DOC)
        self.assertIn('install -o 10001', self.DOC,
                      'file 模式示例没有预建可写的输出文件：uid 10001 建不了，投递会 PermissionError')


if __name__ == '__main__':
    unittest.main()
