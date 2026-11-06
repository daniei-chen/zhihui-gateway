"""Candidate runtime inventory must actually compare managed-release hashes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


_SCRIPT = Path(__file__).resolve().parents[1] / 'ops/integrity/verify-runtime.py'
_SPEC = importlib.util.spec_from_file_location('audit_runtime_integrity', _SCRIPT)
integrity = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(integrity)


class RuntimeIntegrityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = self.root / 'baseline.json'

    def test_managed_candidate_digest_detects_modified_runtime(self):
        release = self.root / 'release'
        release.mkdir()
        (release / 'main.py').write_bytes(b'modified runtime')
        self.baseline.write_text(json.dumps({'files': {'main.py': {
            'candidate_sha256': hashlib.sha256(b'approved runtime').hexdigest(), 'mode': 0o644}}}))
        out = self.root / 'report.json'
        self.assertEqual(integrity.main(['--release', str(release), '--out', str(out),
                                         '--baseline', str(self.baseline)]), 0)
        report = json.loads(out.read_text())
        self.assertEqual(report['baseline']['changed'], ['main.py'])
        self.assertEqual(report['baseline']['counts']['changed'], 1)

    def test_existing_sha256_dictionary_and_list_formats(self):
        digest = 'a' * 64
        for files in ({'a.py': digest}, {'a.py': {'sha256': digest}},
                      [{'path': 'a.py', 'sha256': digest}]):
            self.baseline.write_text(json.dumps({'files': files}))
            self.assertEqual(integrity.load_baseline(self.baseline), {'a.py': digest})

    def test_invalid_or_ambiguous_digests_fail_closed(self):
        for value in ({}, {'sha256': None}, {'sha256': 'not-a-digest'},
                      {'sha256': 'a' * 64, 'candidate_sha256': 'b' * 64}):
            self.baseline.write_text(json.dumps({'files': {'a.py': value}}))
            with self.subTest(value=value), self.assertRaises(SystemExit):
                integrity.load_baseline(self.baseline)

    def test_duplicate_normalized_list_entries_are_rejected(self):
        self.baseline.write_text(json.dumps({'files': [
            {'path': 'server/a.py', 'sha256': 'a' * 64},
            {'path': 'server\\a.py', 'sha256': 'b' * 64}]}))
        with self.assertRaises(SystemExit):
            integrity.load_baseline(self.baseline)


if __name__ == '__main__':
    unittest.main()
