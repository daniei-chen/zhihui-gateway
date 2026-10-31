import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('compression_preparation',ROOT/'tools/prepare_workbuddy_compression.py')
subject=importlib.util.module_from_spec(spec);spec.loader.exec_module(subject)


class CompressionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source';self.source.mkdir()
        self.bundle=self.root/'bundle';self.bundle.mkdir();self.dest=self.root/'new'
        self.before,self.after=b'fixture-before\n',b'fixture-after\n'
        (self.source/'go.mod').write_bytes(self.before)
        self.plan={'schema':1,'patch_sha256':hashlib.sha256(b'fixture').hexdigest(),
            'source_files':{'go.mod':hashlib.sha256(self.before).hexdigest()},
            'candidate_files':{'go.mod':hashlib.sha256(self.after).hexdigest()}}
        (self.bundle/'rebuild-manifest.json').write_text(json.dumps(self.plan))
        (self.bundle/'compression.patch').write_bytes(b'fixture')
        self.enterContext(patch.object(subject,'BUNDLE',self.bundle))

    def test_source_and_failed_candidate_are_retained(self):
        def apply(destination,*args):(destination/'go.mod').write_bytes(self.after)
        with patch.object(subject.core,'apply_patch',side_effect=apply):
            value=subject.prepare(self.source,self.dest)
        self.assertEqual((self.source/'go.mod').read_bytes(),self.before)
        self.assertEqual((self.dest/'go.mod').read_bytes(),self.after)
        self.assertFalse(value['production_modified'])
        with self.assertRaises(ValueError):subject.prepare(self.source,self.dest)

    def test_input_patch_and_result_drift_fail_closed(self):
        (self.bundle/'compression.patch').write_bytes(b'drift')
        with self.assertRaises(ValueError):subject.prepare(self.source,self.dest)
        self.assertFalse(self.dest.exists())
        (self.bundle/'compression.patch').write_bytes(b'fixture')
        with patch.object(subject.core,'apply_patch'),self.assertRaises(ValueError):subject.prepare(self.source,self.dest)
        self.assertTrue(self.dest.exists())
        self.assertEqual((self.source/'go.mod').read_bytes(),self.before)


class CompressionBundleTests(unittest.TestCase):
    def test_historical_candidate_and_actual_release_are_separate(self):
        folder=ROOT/'ops/workbuddy-compression'
        candidate=json.loads((folder/'manifest.json').read_bytes())
        plan=json.loads((folder/'rebuild-manifest.json').read_bytes())
        actual=json.loads((folder/'production-verification.json').read_bytes())
        self.assertEqual(candidate['state'],'candidate_not_installed')
        self.assertEqual(plan['candidate_files'],candidate['files'])
        self.assertEqual(hashlib.sha256((folder/'compression.patch').read_bytes()).hexdigest(),plan['patch_sha256'])
        self.assertEqual(actual['binary_sha256'],candidate['binary_sha256'])
        self.assertEqual(actual['image'],candidate['candidate_image'])
        self.assertEqual(actual['state'],'production_active')
        for field in ('zero_downtime_claimed','production_rollback_exercised','business_state_restored','all_audit_findings_closed'):
            self.assertIs(actual[field],False)


if __name__=='__main__':unittest.main()
