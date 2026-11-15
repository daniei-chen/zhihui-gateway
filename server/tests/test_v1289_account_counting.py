"""v1.2.89：账号池数了「文件」，不是「账号」—— 冗余告警因此被完全掩盖。

## 怎么发现的

审计上游适配时清点 qoder 的账号目录，看到三个 json。逐个看下来只有一个是账号：

| 文件 | 是什么 |
| --- | --- |
| `019ee5d2-482e-7924-b38c-329cd5133c4b.json` | **账号** |
| `machine_identity.json` | 机器标识 |
| `settings.json` | 平台配置 |

而账号池告警的判据是「**≤1 个账号即无冗余**」。

**数文件会得出 3，于是那条告警永远不触发** —— 单点故障被完全掩盖。

workbuddy 那侧没暴露，是因为它 48 个文件全是 `workbuddy-<uuid>.json`，没有辅助文件。

## 修法

按文件名是否含 UUID 判断。**不用排除名单**，因为排除名单会在上游新增辅助文件时
再次失效 —— 上一版（错误码白名单）刚踩过同一个坑：枚举追不上演进。

辅助文件名是语义化的（`machine_identity` / `settings`），账号名一定是 UUID 形状。
"""
from __future__ import annotations

import inspect
import pathlib
import shutil
import tempfile
import unittest

from server import operational


class CountingTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _touch(self, *names):
        for name in names:
            (self.dir / name).write_text('{}', encoding='utf-8')
        return operational._count_account_files(self.dir)

    def test_the_actual_qoder_directory_shape(self):
        """生产里就是这个形状：一个账号加两个辅助文件。"""
        counted = self._touch(
            '019ee5d2-482e-7924-b38c-329cd5133c4b.json',
            'machine_identity.json',
            'settings.json')
        self.assertEqual(counted, 1, '辅助文件被当成账号了')

    def test_the_actual_workbuddy_directory_shape(self):
        """workbuddy 全是 workbuddy-<uuid>.json，不该因为前缀而漏数。"""
        counted = self._touch(
            'workbuddy-032d2815-1dbf-47f5-acb0-dcff9f3ce973.json',
            'workbuddy-0df7f150-f1ae-42e7-964e-174759b3cf23.json')
        self.assertEqual(counted, 2)

    def test_helper_files_alone_count_as_zero(self):
        self.assertEqual(self._touch('settings.json', 'machine_identity.json'), 0)

    def test_only_json_is_considered(self):
        (self.dir / '019ee5d2-482e-7924-b38c-329cd5133c4b.txt').write_text('x')
        (self.dir / 'nested').mkdir()
        (self.dir / 'nested' / '11111111-2222-3333-4444-555555555555.json').write_text('x')
        self.assertEqual(operational._count_account_files(self.dir), 0)

    def test_empty_directory_is_zero(self):
        self.assertEqual(operational._count_account_files(self.dir), 0)

    def test_a_uuid_anywhere_in_the_name_counts(self):
        """不假定位置：`<uuid>.json` 与 `prefix-<uuid>.json` 都算。"""
        counted = self._touch(
            '019ee5d2-482e-7924-b38c-329cd5133c4b.json',
            'workbuddy-032d2815-1dbf-47f5-acb0-dcff9f3ce973.json')
        self.assertEqual(counted, 2)


class TheAlertWouldNowFireTests(unittest.TestCase):
    """真正要修的是「告警不触发」，这里直接确认判据。"""

    def test_one_account_is_no_redundancy(self):
        alerts = operational.account_pool_alerts({'qoder': 1})
        self.assertTrue(any(active for _name, active, _detail in alerts),
                        '一个账号必须触发无冗余告警')

    def test_three_accounts_is_quiet(self):
        alerts = operational.account_pool_alerts({'qoder': 3})
        self.assertFalse(any(active for _name, active, _detail in alerts))

    def test_zero_accounts_is_unusable(self):
        alerts = operational.account_pool_alerts({'qoder': 0})
        details = ' '.join(detail for _name, active, detail in alerts if active)
        self.assertTrue(details, '零账号必须报')

    def test_the_bug_scenario_now_behaves(self):
        """这个测试就是当初的现场：目录里三个文件，一个账号。"""
        counted = operational._count_account_files(
            _dir_with('019ee5d2-482e-7924-b38c-329cd5133c4b.json',
                      'machine_identity.json', 'settings.json'))
        alerts = operational.account_pool_alerts({'qoder': counted})
        self.assertTrue(any(active for _name, active, _detail in alerts),
                        '修复前这里数成 3，告警不响')


def _dir_with(*names):
    directory = pathlib.Path(tempfile.mkdtemp())
    for name in names:
        (directory / name).write_text('{}', encoding='utf-8')
    return directory


class TheCollectorUsesItTests(unittest.TestCase):
    def test_collect_uses_the_helper_not_a_raw_glob(self):
        source = inspect.getsource(operational.collect_account_counts)
        self.assertIn('_count_account_files', source)
        self.assertNotIn("glob('*.json')", source,
                         '裸 glob 会把辅助文件算进账号')

    def test_the_helper_and_pattern_exist(self):
        self.assertTrue(callable(operational._count_account_files))
        self.assertIsNotNone(operational._ACCOUNT_NAME)

    def test_re_is_imported(self):
        """第一版漏了 import，模块直接起不来。"""
        import ast
        tree = ast.parse(inspect.getsource(operational))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split('.')[0] for a in node.names)
        self.assertIn('re', imported)


class TheReasonIsRecordedTests(unittest.TestCase):
    def test_the_module_explains_the_consequence(self):
        source = inspect.getsource(operational)
        start = source.index('_ACCOUNT_NAME')
        window = source[max(0, start - 1500):start]
        self.assertIn('掩盖', window,
                      '要写出后果：那条告警永远不触发，单点故障看不见')


if __name__ == '__main__':
    unittest.main()
