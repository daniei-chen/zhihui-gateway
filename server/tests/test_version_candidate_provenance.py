"""A source version bump must not copy a prior release's deployment claims."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import bump_version


class CandidateProvenanceTests(unittest.TestCase):
    def test_new_version_clears_old_build_and_deployment_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'provenance.json'
            path.write_text(json.dumps({
                'version': '1.6.26', 'release_id': 'v1.6.26',
                'candidate_only': False, 'production_deployed': True,
                'signed': True, 'source_commit': 'a' * 40,
                'source_kind': 'github-actions-release',
                'frontend_rebuilt': True, 'source_input_sha256': 'old-source',
                'frontend_input_sha256': 'old-frontend', 'reviewed_patch_files': {},
            }))
            with (mock.patch.object(bump_version, 'ROOT', root),
                  mock.patch.object(bump_version, 'PROVENANCE', path),
                  mock.patch.object(bump_version, '_git', return_value='b' * 40),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(bump_version.sync_provenance('1.6.27'), 0)
                value = json.loads(path.read_text())
                self.assertEqual(value['version'], '1.6.27')
                self.assertEqual(value['base_release'], 'v1.6.26')
                self.assertEqual(value['base_commit'], 'b' * 40)
                self.assertTrue(value['candidate_only'])
                self.assertFalse(value['production_deployed'])
                self.assertFalse(value['frontend_rebuilt'])
                self.assertFalse(value['signed'])
                self.assertIsNone(value['source_commit'])
                self.assertEqual(value['source_kind'], 'source-candidate')
                self.assertNotIn('source_input_sha256', value)
                self.assertNotIn('frontend_input_sha256', value)
                # Refreshing hashes without another version bump does not invent
                # a commit or mark this candidate as deployed.
                self.assertEqual(bump_version.sync_provenance('1.6.27'), 0)
                self.assertEqual(json.loads(path.read_text()), value)

    def test_same_version_keeps_existing_build_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'provenance.json'
            original = {'version': '1.6.27', 'release_id': 'v1.6.27',
                        'source_commit': 'c' * 40, 'source_kind': 'github-actions-release',
                        'candidate_only': False, 'production_deployed': False,
                        'reviewed_patch_files': {}}
            path.write_text(json.dumps(original))
            with (mock.patch.object(bump_version, 'PROVENANCE', path),
                  mock.patch.object(bump_version, '_git') as git,
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(bump_version.sync_provenance('1.6.27'), 0)
                git.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), original)

    def test_same_version_changed_source_invalidates_old_build_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.py'
            source.write_text('changed source\n')
            path = root / 'provenance.json'
            path.write_text(json.dumps({
                'version': '1.6.27', 'release_id': 'v1.6.27',
                'candidate_only': False, 'production_deployed': True,
                'signed': True, 'source_commit': 'c' * 40,
                'source_kind': 'github-actions-release',
                'reviewed_patch_files': {'source.py': hashlib.sha256(b'old source\n').hexdigest()},
            }))
            with (mock.patch.object(bump_version, 'ROOT', root),
                  mock.patch.object(bump_version, 'PROVENANCE', path),
                  mock.patch.object(bump_version, '_git', return_value='d' * 40),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(bump_version.sync_provenance('1.6.27'), 0)
            value = json.loads(path.read_text())
            self.assertTrue(value['candidate_only'])
            self.assertFalse(value['production_deployed'])
            self.assertFalse(value['signed'])
            self.assertIsNone(value['source_commit'])
            self.assertEqual(value['source_kind'], 'source-candidate')
            self.assertEqual(value['reviewed_patch_files']['source.py'],
                             hashlib.sha256(source.read_bytes()).hexdigest())

    def test_unavailable_git_cannot_leave_mismatched_version_metadata(self):
        for failure in (subprocess.CalledProcessError(128, ['git', 'rev-parse', 'HEAD']),
                        subprocess.TimeoutExpired(['git', 'rev-parse', 'HEAD'], 20)):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'provenance.json'
                path.write_text(json.dumps({'version': '1.6.26', 'base_commit': 'a' * 40,
                                            'reviewed_patch_files': {}}))
                with (mock.patch.object(bump_version, 'PROVENANCE', path),
                      mock.patch.object(bump_version, '_git', side_effect=failure),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertEqual(bump_version.sync_provenance('1.6.27'), 0)
                value = json.loads(path.read_text())
                self.assertEqual(value['version'], '1.6.27')
                self.assertEqual(value['release_id'], 'v1.6.27')
                self.assertTrue(value['candidate_only'])
                self.assertIsNone(value['source_commit'])
                self.assertNotIn('base_commit', value)


if __name__ == '__main__':
    unittest.main()
