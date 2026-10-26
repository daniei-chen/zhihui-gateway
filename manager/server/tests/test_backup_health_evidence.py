"""Health must not claim a backup exists when evidence is absent or corrupt."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from server import operational


class BackupHealthEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'backup-status.json'
        self.now = time.time()
        self.patch = patch('server.config.BACKUP_STATUS_FILE',self.path)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.alerts = patch.object(operational,'alerts',{})
        self.alerts.start();self.addCleanup(self.alerts.stop)

    def write(self, **updates):
        self.path.write_text(json.dumps({'restore_check':'ok','bytes':4096,'offsite':True,**updates}))
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
        self.assertFalse(operational.alerts)

    def test_local_backup_does_not_imply_verified_offsite_copy(self):
        self.write(offsite='true')
        operational.evaluate_backup_health(self.now)
        self.assertIn('backup_publish',operational.alerts)
        self.assertNotIn('backup_stale',operational.alerts)

    def test_stale_oversize_and_future_report_are_observable(self):
        self.write(bytes=46*1024**2)
        operational.evaluate_backup_health(self.now+27*3600)
        self.assertTrue({'backup_stale','backup_size'} <= operational.alerts.keys())
        operational.evaluate_backup_health(self.now-3600)
        self.assertIn('backup_status_unavailable',operational.alerts)
