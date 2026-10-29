"""Regression coverage for verified backup evidence and conservative accounting."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from server import backupstatus, budget, operational

class BackupStatusPublicationTests(unittest.TestCase):
    def test_private_job_umask_keeps_default_status_directory_traversable(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'public-status'/'status.json'
            previous=os.umask(0o077)
            try:
                with patch.object(backupstatus,'DEFAULT_STATUS',target):
                    backupstatus.publish_verified_status({'restore_check':'ok','bytes':1},target)
            finally:
                os.umask(previous)
            self.assertEqual(target.parent.stat().st_mode & 0o777,0o755)
            self.assertEqual(target.stat().st_mode & 0o777,0o644)

    def test_public_report_is_readable_and_omits_private_material(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/'public'/'backup-status.json'
            result = {'restore_check':'ok','bytes':128,'path':'/root/private.tar.gz',
                      'password':'private fixture', 'offsite':{'verified_remote':True,'remote_secret':'fixture'}}
            backupstatus.publish_verified_status(result,target)
            data = json.loads(target.read_text())
            self.assertEqual(set(data),{'schema','created_at','bytes','restore_check','offsite'})
            self.assertIs(data['offsite'],True)
            self.assertEqual(target.stat().st_mode & 0o777,0o644)
            self.assertNotIn('fixture',target.read_text())

    def test_invalid_backup_preserves_previous_report(self):
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'status.json'
            target.write_text('previous verified evidence')
            for result in ({'restore_check':'failed','bytes':128},{'restore_check':'ok','bytes':True},
                           {'restore_check':'ok','bytes':0}):
                with self.assertRaises(ValueError):
                    backupstatus.publish_verified_status(result,target)
                self.assertEqual(target.read_text(),'previous verified evidence')

    def test_symlink_cannot_overwrite_another_file(self):
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'private'
            source.write_text('untouched')
            link=Path(root)/'status.json'
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                backupstatus.publish_verified_status({'restore_check':'ok','bytes':1},link)
            self.assertEqual(source.read_text(),'untouched')

    def test_published_report_clears_only_local_backup_alerts(self):
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'status.json'
            backupstatus.publish_verified_status({'restore_check':'ok','bytes':128,'offsite':False},target)
            with patch('server.config.BACKUP_STATUS_FILE',target),patch.object(operational,'alerts',{}),patch.object(operational,'_offsite_configured',return_value=False):
                operational.evaluate_backup_health(time.time())
                self.assertNotIn('backup_status_unavailable',operational.alerts)
                self.assertNotIn('backup_stale',operational.alerts)

class ConservativeReconciliationTests(unittest.TestCase):
    def row(self, **changes):
        return {'request_id':'fixture','key_id':1,'state':'unknown','created_at':1,
                'reserved_tokens':0,'reserved_credit':0,'charged_tokens':0,'charged_credit':0,**changes}

    def test_unknown_nonzero_reservation_is_not_guessed_as_zero(self):
        for field in ('reserved_tokens','reserved_credit','charged_tokens','charged_credit'):
            with self.subTest(field=field),patch.object(budget,'iter_unresolved',return_value=iter([self.row(**{field:1})])),patch.object(budget,'resolve') as resolve:
                self.assertEqual(operational.reconcile_zero_reservation_unknowns(100000),0)
                resolve.assert_not_called()

    def test_zero_unknown_with_receipt_is_resolved_with_atomic_zero_guard(self):
        with patch.object(budget,'iter_unresolved',return_value=iter([self.row()])),patch.object(budget,'_confirmed_zero_usage',return_value=True),patch.object(budget,'resolve',return_value=True) as resolve:
            self.assertEqual(operational.reconcile_zero_reservation_unknowns(100000),1)
            self.assertIs(resolve.call_args.kwargs['zero_only'],True)

    def test_zero_unknown_without_receipt_is_not_a_zero_bill(self):
        with patch.object(budget,'iter_unresolved',return_value=iter([self.row()])),patch.object(budget,'_confirmed_zero_usage',return_value=False),patch.object(budget,'resolve') as resolve:
            self.assertEqual(operational.reconcile_zero_reservation_unknowns(100000),0)
            resolve.assert_not_called()

    def test_amount_changed_since_scan_is_rechecked_inside_transaction(self):
        for field in ('reserved_tokens','reserved_credit','charged_tokens','charged_credit'):
            with self.subTest(field=field),patch.object(budget.db,'transaction',return_value=nullcontext()),patch.object(budget.db,'query_one',return_value=self.row(**{field:1})),patch.object(budget.db,'execute') as execute:
                self.assertFalse(budget.resolve('fixture',0,0,'audit','zero fixture',zero_only=True))
                execute.assert_not_called()

    def test_manual_resolution_remains_available_for_verified_usage(self):
        from server import dailyquota
        with patch.object(budget.db,'transaction',return_value=nullcontext()),patch.object(budget.db,'query_one',side_effect=[self.row(reserved_tokens=500), None]),patch.object(budget.db,'execute') as execute,patch.object(budget.ledger,'adjust'),patch.object(budget.db,'add_audit_log'),patch.object(dailyquota,'charge') as daily:
            self.assertTrue(budget.resolve('fixture',10,0,'audit','verified fixture'))
            self.assertTrue(execute.called)
            daily.assert_called_once_with(1, 10, '0', 1)


class PublicSignaturePermissionsTests(unittest.TestCase):
    def test_private_umask_still_produces_a_public_verifiable_signature(self):
        import importlib.util
        import os
        import subprocess
        signer = Path(__file__).resolve().parents[2]/'deploy/sign-release-ed25519.py'
        spec=importlib.util.spec_from_file_location('audit_signer_fixture',signer)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            key=root/'private.pem';public=root/'public.pem'
            target=root/'manifest.json';signature=root/'manifest.sig'
            target.write_text('{"fixture":true}')
            old=os.umask(0o077)
            try:
                subprocess.run(['openssl','genpkey','-algorithm','Ed25519','-out',str(key)],check=True,capture_output=True)
                subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-out',str(public)],check=True,capture_output=True)
                module.sign_file(key,target,signature)
                module.verify_file(public,target,signature)
            finally:
                os.umask(old)
            self.assertEqual(key.stat().st_mode & 0o777,0o600)
            self.assertEqual(signature.stat().st_mode & 0o777,0o644)
