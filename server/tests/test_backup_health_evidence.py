"""Health must not claim a backup exists when evidence is absent or corrupt."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from server import operational, offsitebackup


class BackupHealthEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'backup-status.json'
        self.now = time.time()
        self.patch = patch('server.config.BACKUP_STATUS_FILE',self.path)
        self.patch.start();self.addCleanup(self.patch.stop)
        # 异地发布配置：默认「已配置」（保留这些用例原本的语义），
        # 需要验证「关闭后不报」的用例把文件删掉即可。
        self.offsite_path = Path(self.directory.name)/'offsite.json'
        self.offsite_path.write_text('{}')
        self.offsite_patch = patch('server.offsitebackup.CONFIG', self.offsite_path)
        self.offsite_patch.start();self.addCleanup(self.offsite_patch.stop)
        self.alerts = patch.object(operational,'alerts',{})
        self.alerts.start();self.addCleanup(self.alerts.stop)

    def write(self, **updates):
        self.path.write_text(json.dumps({'schema':2,'created_at':int(self.now),
            'restore_check':'ok','bytes':4096,'offsite':True,'archive_sha256':'a'*64,
            'offsite_readback':{'method':offsitebackup.READBACK_METHOD,'verified_at':int(self.now),
                'commit':'b'*40,'cipher_sha256':'c'*64,'cipher_bytes':4096,
                'source_archive_sha256':'a'*64},**updates}))
        os.utime(self.path,(self.now,self.now))

    def test_missing_report_is_not_healthy(self):
        operational.evaluate_backup_health(self.now)
        self.assertTrue({'backup_status_unavailable','backup_stale','backup_publish'} <= operational.alerts.keys())

    def test_corrupt_or_unverified_report_cannot_clear_alerts(self):
        for contents in ('[]','{bad','{}',json.dumps({'restore_check':'failed','bytes':4096,'offsite':True})):
            self.path.write_text(contents)
            operational.evaluate_backup_health(self.now)
            self.assertIn('backup_status_unavailable',operational.alerts)

    def test_fresh_valid_evidence_records_recovery(self):
        operational.evaluate_backup_health(self.now)
        self.write()
        operational.evaluate_backup_health(self.now)
        # 一份新鲜的基线报告清掉的是**基线那三条**。
        #
        # 这里原本断言 `alerts` 整体为空 —— 那个假设在 v1.2.97 之后不再成立：
        # 新增的 `recovery_chain_stale` 测的是加密快照整链，**与这份报告无关**，
        # 所以它继续留着是对的。把断言收窄到真正被这份证据影响的那几条。
        for name in ('backup_status_unavailable', 'backup_stale', 'backup_publish'):
            self.assertNotIn(name, operational.alerts)
        self.assertIn('recovery_chain_stale', operational.alerts,
                      '基线报告不能替整链作证')

    def test_local_backup_does_not_imply_verified_offsite_copy(self):
        self.write(offsite='true')
        operational.evaluate_backup_health(self.now)
        self.assertIn('backup_publish',operational.alerts)
        self.assertNotIn('backup_stale',operational.alerts)

    def test_stale_oversize_and_future_report_are_observable(self):
        self.write(bytes=int(offsitebackup.MAX_CIPHER_BYTES * 0.95))
        operational.evaluate_backup_health(self.now+27*3600)
        self.assertTrue({'backup_stale','backup_size'} <= operational.alerts.keys())
        operational.evaluate_backup_health(self.now-3600)
        self.assertIn('backup_status_unavailable',operational.alerts)

    def test_disabled_offsite_does_not_raise_offsite_alerts(self):
        """异地备份按决策关闭后，这两项是「不适用」而不是「故障」。

        `backup_publish`（加密异地发布未验证）与 `backup_size`（密文接近 50 MiB 上限）
        描述的都是**异地发布**的问题。关闭后继续报，会让每次启动都刷两条噪音，
        把真正需要人看的告警稀释掉（启动日志里它们和 budget_unknown、
        model_completion 混在一起，看得多了就没人看了）。

        本地备份的健康项**仍然要报**——关掉异地不等于不管本地快照。
        """
        self.offsite_path.unlink()          # 未配置 = 按决策关闭
        self.path.write_text(json.dumps({'restore_check':'ok','bytes':46*1024**2}))
        os.utime(self.path,(self.now,self.now))
        operational.evaluate_backup_health(self.now)
        self.assertNotIn('backup_publish', operational.alerts,
                         '异地备份已关闭，不该再报「异地发布未验证」')
        self.assertNotIn('backup_size', operational.alerts,
                         '50 MiB 是密文上限，异地关闭后这条没有意义')
        self.assertNotIn('backup_status_unavailable', operational.alerts)
        self.assertNotIn('backup_stale', operational.alerts)

    def test_disabled_offsite_still_reports_local_backup_gaps(self):
        """关闭异地后，本地备份缺失仍必须告警（不能连带把本地健康也静音）。"""
        self.offsite_path.unlink()
        operational.evaluate_backup_health(self.now)     # 报告缺失
        self.assertIn('backup_status_unavailable', operational.alerts)
        self.assertIn('backup_stale', operational.alerts)
        self.assertNotIn('backup_publish', operational.alerts)
