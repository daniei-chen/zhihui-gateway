"""Synthetic restore evidence must not approve mismatched or unchecked assets."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


_SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/restore_drill.py'
_SPEC = importlib.util.spec_from_file_location('audit_restore_drill', _SCRIPT)
drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drill)


class RestoreEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshot = self.root / 'snapshot'
        self.snapshot.mkdir()
        self.work = self.root / 'work'
        key = self.root / 'synthetic-key'
        key.write_text('fixture only; never used for decryption')
        patcher = mock.patch.multiple(drill, WORK=self.work, KEY=key)
        patcher.start()
        self.addCleanup(patcher.stop)
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w:gz') as bundle:
            sidecar = b'{"files":[],"sqlite_snapshots":[]}'
            entry = tarfile.TarInfo('recovery-manifest.json')
            entry.size = len(sidecar)
            bundle.addfile(entry, io.BytesIO(sidecar))
        self.archive = payload.getvalue()
        ciphertext = b'synthetic ciphertext'
        (self.snapshot / 'snapshot.part-000.age').write_bytes(ciphertext)
        self.meta = {'cipher_sha256': hashlib.sha256(ciphertext).hexdigest(),
                     'archive_sha256': hashlib.sha256(self.archive).hexdigest(),
                     'parts': [{'name': 'snapshot.part-000.age'}]}

    def run_drill(self, *args):
        (self.snapshot / 'snapshot.json').write_text(json.dumps(self.meta))

        def start(command, **kwargs):
            if command[0] == 'cat':
                return SimpleNamespace(stdout=io.BytesIO(), wait=lambda: 0, returncode=0)
            self.assertEqual(command[0], 'age')
            kwargs['stdout'].write(self.archive)
            return SimpleNamespace(communicate=lambda: (None, b''), returncode=0)

        with mock.patch.object(drill.subprocess, 'Popen', side_effect=start):
            code = drill.main([str(self.snapshot), *args])
        return code, json.loads((self.work / 'drill-report.json').read_text())

    def test_archive_mismatch_stops_before_sidecar_restoration(self):
        self.meta['archive_sha256'] = '0' * 64
        code, report = self.run_drill('--restore-sidecar')
        self.assertEqual(code, 5)
        self.assertFalse((self.snapshot / 'recovery-manifest.json').exists())
        self.assertEqual(report['result'], 'failed')
        self.assertFalse(report['complete_restore_verified'])

    def test_skipped_sqlite_never_claims_complete_recovery(self):
        code, report = self.run_drill('--skip-sqlite')
        self.assertEqual(code, 0)
        self.assertEqual(report['result'], 'sampled')
        self.assertTrue(report['sqlite_skipped'])
        self.assertFalse(report['complete_restore_verified'])

    def test_missing_archive_digest_is_reported_as_unchecked(self):
        self.meta.pop('archive_sha256')
        code, report = self.run_drill('--skip-sqlite')
        self.assertEqual(code, 5)  # Missing receipt is unknown and blocks acceptance.
        archive_step = next(step for step in report['steps'] if step['step'] == 'archive_sha256')
        self.assertIsNone(archive_step['ok'])
        self.assertFalse(report['complete_restore_verified'])

    def test_sidecar_replacement_does_not_follow_existing_symlink(self):
        external = self.root / 'protected'
        external.write_bytes(b'outside sentinel')
        sidecar = self.snapshot / 'recovery-manifest.json'
        try:
            sidecar.symlink_to(external)
        except OSError as exc:
            self.skipTest(f'filesystem cannot create a synthetic symlink: {exc}')
        code, report = self.run_drill('--restore-sidecar', '--skip-sqlite')
        self.assertEqual(code, 0)
        self.assertEqual(external.read_bytes(), b'outside sentinel')
        self.assertFalse(sidecar.is_symlink())
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        self.assertFalse(report['read_only_source'])


if __name__ == '__main__':
    unittest.main()
