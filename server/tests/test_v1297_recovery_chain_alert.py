"""v1.2.97：告警一直在测错的那条链。

## 现场

2026-11-15 这一整段审计里，完整恢复链**连续失败多次**：

1. 基线备份超时（300 秒不够）
2. 归档校验重复解压（111 TB 读取，跑不完）
3. 加密超过 1 GiB 上限

**而 `backup_stale` 告警从头到尾没有响过。**

## 为什么

| 文件 | 谁写 | 反映什么 |
| --- | --- | --- |
| `backup-status.json` | 基线备份 | **只有基线 tar** |
| `/root/.../snapshots` | 恢复脚本 | 但**网关读不到**（0700 vs uid 10001） |

`backup_stale` 读的是第一行那份文件，而**基线备份每次都成功**。

于是：**告警测的那条链永远绿，真正会坏的那条链没人看。**

## 修法

1. 恢复脚本在整链成功后写 `/var/lib/zhihui-gateway/recovery-chain.json`（网关可读）
2. 网关读它，出 `workbuddy_recovery_chain_age_seconds` / `_bytes`
3. 新告警 `recovery_chain_stale`：**没有证据也报**，不只是过期才报

**第三条是关键**：老告警在「没有证据」时也报了 `backup_stale`，但那条证据本身
是误导的。新告警把「从未成功过」和「上次成功太久以前」都算作需要人看的信号。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from server import config, main, operational


class ChainStatusTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.status = self.dir / 'recovery-chain.json'
        patcher = mock.patch.object(operational, '_RECOVERY_CHAIN_STATUS', self.status)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, payload):
        self.status.write_text(json.dumps(payload))

    GOOD = {'schema': 1, 'verified_at': 1000000, 'snapshot_bytes': 587860067,
            'snapshot_parts': 15, 'snapshot_checks': {'files_verified': 46550}}

    def test_valid_status_is_read(self):
        self._write(self.GOOD)
        got = operational.recovery_chain_status(now=1003600)
        self.assertEqual(got['age_seconds'], 3600)
        self.assertEqual(got['snapshot_parts'], 15)
        self.assertEqual(got['files_verified'], 46550)

    def test_missing_file_is_none(self):
        self.assertIsNone(operational.recovery_chain_status(now=1000000))

    def test_wrong_schema_is_none(self):
        self._write(dict(self.GOOD, schema=2))
        self.assertIsNone(operational.recovery_chain_status(now=1000000))

    def test_zero_parts_is_none(self):
        self._write(dict(self.GOOD, snapshot_parts=0))
        self.assertIsNone(operational.recovery_chain_status(now=1000000))

    def test_zero_bytes_is_none(self):
        self._write(dict(self.GOOD, snapshot_bytes=0))
        self.assertIsNone(operational.recovery_chain_status(now=1000000))

    def test_boolean_parts_is_none(self):
        """`True` 是 int 的子类，直接判断会把它当 1。"""
        self._write(dict(self.GOOD, snapshot_parts=True))
        self.assertIsNone(operational.recovery_chain_status(now=1000000))

    def test_future_timestamp_is_none(self):
        self._write(self.GOOD)
        self.assertIsNone(operational.recovery_chain_status(now=1000000 - 600))

    def test_small_skew_tolerated(self):
        self._write(self.GOOD)
        self.assertIsNotNone(operational.recovery_chain_status(now=1000000 - 120))

    def test_it_never_raises(self):
        with mock.patch.object(operational, '_RECOVERY_CHAIN_STATUS',
                               pathlib.Path('/root/forbidden/x.json')):
            try:
                operational.recovery_chain_status(now=1)
            except Exception as exc:      # noqa: BLE001
                self.fail(f'不该抛异常：{exc!r}')


class TheAlertNowCoversTheRealChainTests(unittest.TestCase):
    """这条告警是本次修复的目的本身。"""

    def _run(self, now, chain):
        """跑备份告警，返回 recovery_chain_stale 是否 active。"""
        with mock.patch.object(operational, 'recovery_chain_status', return_value=chain), \
                mock.patch.object(operational, 'backup_age_seconds', return_value=600), \
                mock.patch.object(operational, 'transition') as tr, \
                mock.patch.object(operational, '_offsite_configured', return_value=False), \
                mock.patch.object(config, 'BACKUP_STATUS_FILE',
                                  pathlib.Path(tempfile.mkdtemp()) / 'b.json'):
            operational.evaluate_backup_health(now)
            for call in tr.call_args_list:
                if call.args and call.args[0] == 'recovery_chain_stale':
                    return call.args[1]
        return None

    def test_no_evidence_raises_the_alert(self):
        """从未成功过也必须报 —— 这正是过去完全静默的情形。"""
        self.assertTrue(self._run(1000000, None))

    def test_fresh_chain_is_quiet(self):
        self.assertFalse(self._run(1000000, {'age_seconds': 3600, 'snapshot_parts': 15,
                                             'snapshot_bytes': 587860067}))

    def test_over_50_hours_raises(self):
        self.assertTrue(self._run(1000000, {'age_seconds': 51*3600, 'snapshot_parts': 15,
                                            'snapshot_bytes': 1}))

    def test_just_under_50_hours_is_quiet(self):
        self.assertFalse(self._run(1000000, {'age_seconds': 49*3600, 'snapshot_parts': 15,
                                             'snapshot_bytes': 1}))


class TheMetricsExposeItTests(unittest.TestCase):
    SOURCE = __import__('inspect').getsource(main.prometheus_metrics)

    def test_both_metrics_exist(self):
        for name in ('workbuddy_recovery_chain_age_seconds',
                     'workbuddy_recovery_chain_bytes'):
            self.assertIn(name, self.SOURCE)

    def test_help_distinguishes_it_from_the_baseline_gauge(self):
        start = self.SOURCE.index('workbuddy_recovery_chain_age_seconds')
        window = self.SOURCE[start:start + 600]
        self.assertIn('backup_age_seconds', window,
                      '要写明与基线指标的区别，否则后来人会以为两个是重复的')

    def test_unknown_is_reported_rather_than_omitted(self):
        """链状态读不到时要报 -1，不能让整条序列消失。

        2026-11-15 那天这条链反复失败而基线一直成功；
        如果指标在异常时反而不输出，告警会全程安静 —— 那正是要避免的。
        """
        start = self.SOURCE.index('workbuddy_recovery_chain_age_seconds')
        window = self.SOURCE[max(0, start - 500):start + 900]
        self.assertIn('-1 if _chain is None', window,
                      '链状态不可用要报 -1，不是不报')


class TheReasonIsRecordedTests(unittest.TestCase):
    def test_module_records_the_incident(self):
        source = __import__('inspect').getsource(operational)
        start = source.index('_RECOVERY_CHAIN_STATUS')
        window = source[max(0, start - 800):start + 200]
        self.assertIn('backup-status.json', window)
        self.assertIn('0700', window, '要写出「网关读不到 /root」这个原因')

    def test_alert_source_records_why_the_old_one_stayed_silent(self):
        source = __import__('inspect').getsource(operational.evaluate_backup_health)
        self.assertIn('一片安静', source)


if __name__ == '__main__':
    unittest.main()
