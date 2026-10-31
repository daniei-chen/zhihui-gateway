"""The engine patch must fail closed on drift and never alter the source tree."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_workbuddy_ingress', ROOT / 'tools/prepare_workbuddy_ingress.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class WorkBuddyIngressPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.dest = self.root / 'candidate'
        self.original, self.changed = b'package fixture\n', b'package fixture // guarded\n'
        (self.source / 'go.mod').write_bytes(self.original)
        self.manifest = {'schema': 1, 'component': 'fixture',
                         'patch_sha256': module.digest(b'fixture patch'),
                         'source_files': {'go.mod': module.digest(self.original)},
                         'candidate_files': {'go.mod': module.digest(self.changed)}}
        self.write_manifest()
        (self.bundle / 'ingress.patch').write_bytes(b'fixture patch')
        self.override = patch.object(module, 'BUNDLE', self.bundle)
        self.override.start()
        self.addCleanup(self.override.stop)

    def write_manifest(self):
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def fake_apply(self, destination, raw, git):
        self.assertEqual(raw, b'fixture patch')
        (destination / 'go.mod').write_bytes(self.changed)

    def test_verified_candidate_leaves_source_unchanged(self):
        with patch.object(module, 'apply_patch', side_effect=self.fake_apply):
            value = module.prepare(self.source, self.dest)
        self.assertEqual((self.source / 'go.mod').read_bytes(), self.original)
        self.assertEqual((self.dest / 'go.mod').read_bytes(), self.changed)
        self.assertTrue(value['source_unchanged'])
        self.assertFalse(value['production_modified'])

    def test_rejects_wrong_baseline_before_creating_candidate(self):
        (self.source / 'go.mod').write_bytes(b'different')
        with self.assertRaises(ValueError), patch.object(module, 'apply_patch') as apply:
            module.prepare(self.source, self.dest)
        apply.assert_not_called()
        self.assertFalse(self.dest.exists())

    def test_rejects_runtime_files_and_unknown_additions(self):
        for name in ('config.json', 'users.json', 'manager.db', '.env', 'extra.go'):
            path = self.source / name
            path.write_bytes(b'fixture')
            with self.subTest(name=name), self.assertRaises(ValueError):
                module.prepare(self.source, self.dest)
            self.assertFalse(self.dest.exists())
            path.unlink()

    def test_never_overwrites_existing_destination(self):
        self.dest.mkdir()
        keep = self.dest / 'keep'
        keep.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            module.prepare(self.source, self.dest)
        self.assertEqual(keep.read_bytes(), b'keep')

    def test_rejects_candidate_inside_source(self):
        with self.assertRaises(ValueError):
            module.prepare(self.source, self.source / 'candidate')
        self.assertFalse((self.source / 'candidate').exists())

    def test_rejects_hardlinked_source_and_case_colliding_manifest(self):
        outside = self.root / 'linked'
        os.link(self.source / 'go.mod', outside)
        with self.assertRaises(ValueError):
            module.prepare(self.source, self.dest)
        self.assertFalse(self.dest.exists())
        outside.unlink()
        self.manifest['source_files']['Go.mod'] = self.manifest['source_files']['go.mod']
        self.write_manifest()
        with self.assertRaises(ValueError):
            module.prepare(self.source, self.dest)
        self.assertFalse(self.dest.exists())

    def test_rejects_changed_patch_and_unsafe_manifest_paths(self):
        (self.bundle / 'ingress.patch').write_bytes(b'drift')
        with self.assertRaises(ValueError):
            module.prepare(self.source, self.dest)
        (self.bundle / 'ingress.patch').write_bytes(b'fixture patch')
        for name in ('../outside', '/absolute', 'C:/outside', 'a\\outside', 'a/../outside', './go.mod'):
            self.manifest['source_files'] = {name: module.digest(b'fixture')}
            self.write_manifest()
            with self.subTest(name=name), self.assertRaises(ValueError):
                module.prepare(self.source, self.dest)
        self.assertFalse(self.dest.exists())

    def test_rejects_result_drift_and_retains_failed_candidate(self):
        with patch.object(module, 'apply_patch'), self.assertRaises(ValueError):
            module.prepare(self.source, self.dest)
        self.assertTrue(self.dest.exists())
        self.assertEqual((self.source / 'go.mod').read_bytes(), self.original)

    def test_source_change_during_patch_is_not_reported_as_success(self):
        def mutate(destination, raw, git):
            self.fake_apply(destination, raw, git)
            (self.source / 'go.mod').write_bytes(b'concurrent change')
        with patch.object(module, 'apply_patch', side_effect=mutate), self.assertRaises(ValueError):
            module.prepare(self.source, self.dest)
        self.assertTrue(self.dest.exists())

    def test_git_failure_keeps_failed_candidate_and_does_not_retry(self):
        with patch.object(module, 'apply_patch', side_effect=subprocess.CalledProcessError(1, 'git')) as apply:
            with self.assertRaises(subprocess.CalledProcessError):
                module.prepare(self.source, self.dest)
        self.assertEqual(apply.call_count, 1)
        self.assertTrue(self.dest.exists())

    def test_git_ignores_ambient_overrides_and_uses_no_shell(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '5', 'GIT_DIR': 'fixture', 'GIT_INDEX_FILE': 'fixture'}), \
                patch.object(module.subprocess, 'run') as run:
            module.apply_patch(self.root, b'fixture patch', 'fixture-git')
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            args, kwargs = call.args[0], call.kwargs
            self.assertNotIn('GIT_DIR', kwargs['env'])
            self.assertNotIn('GIT_INDEX_FILE', kwargs['env'])
            self.assertNotIn('GIT_CONFIG_COUNT', kwargs['env'])
            self.assertEqual(kwargs['env']['GIT_CONFIG_GLOBAL'], os.devnull)
            self.assertTrue(kwargs['check'])
            self.assertNotIn('shell', kwargs)
            self.assertIn('core.hooksPath=', args)
        self.assertIn('--check', run.call_args_list[0].args[0])


class WorkBuddyIngressBundleTests(unittest.TestCase):
    def test_production_receipt_does_not_rewrite_historical_candidate_or_overclaim(self):
        folder = ROOT / 'ops/workbuddy-ingress'
        candidate = json.loads((folder / 'candidate-verification.json').read_bytes())
        actual = json.loads((folder / 'production-verification.json').read_bytes())
        self.assertTrue(candidate['candidate_not_installed'])
        self.assertEqual(actual['state'], 'production_active')
        self.assertEqual(actual['running_elf_sha256'], candidate['binary_sha256'])
        self.assertEqual(actual['image'], candidate['candidate_image'])
        self.assertEqual(actual['limits'], {'max_body_bytes': 64 << 20,
            'max_buffered_body_bytes': 128 << 20, 'max_concurrent_bodies': 32})
        self.assertEqual(actual['phase'], 'complete')
        for field in ('business_state_restored', 'zero_downtime_claimed',
                      'production_rollback_exercised', 'all_audit_findings_closed'):
            self.assertIs(actual[field], False)
        self.assertEqual(actual['production_header_tests']['real_provider_calls'], 0)

    def test_real_patch_matches_manifest_and_is_narrow(self):
        folder = ROOT / 'ops/workbuddy-ingress'
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        raw = (folder / 'ingress.patch').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['patch_sha256'])
        self.assertEqual(manifest['state'], 'candidate_not_installed')
        self.assertEqual(len(manifest['source_files']), 263)
        self.assertEqual(len(manifest['candidate_files']), 266)
        self.assertEqual(set(manifest['candidate_files']) - set(manifest['source_files']), set(manifest['added']))
        changed = {name for name, sha in manifest['source_files'].items() if manifest['candidate_files'][name] != sha}
        self.assertEqual(changed, set(manifest['modified']))
        self.assertEqual(manifest['limits'], {'max_body_mb': 64, 'max_buffered_body_mb': 128, 'max_concurrent_bodies': 32})
        self.assertIn(b'http.MaxBytesReader', raw)
        self.assertIn(b'defer releaseBody()', raw)
        self.assertIn('Copyright (c) 2026 Sliverkiss', (folder / 'LICENSE').read_text())


if __name__ == '__main__':
    unittest.main()
