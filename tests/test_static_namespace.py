import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('static_namespace', Path(__file__).resolve().parents[1] / 'tools/materialize_static_namespace.py')
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class StaticNamespaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.web = Path(self.temporary.name)
        (self.web / '.next').mkdir()
        (self.web / '.next/BUILD_ID').write_text('native-build-007')
        (self.web / 'out/_next/native-build-007').mkdir(parents=True)
        (self.web / 'out/_next/static/native-build-007').mkdir(parents=True)

    def test_default_is_readonly_and_preserves_empty_native_directory(self):
        self.assertTrue(subject.check(self.web)['namespace_materialized'])
        self.assertEqual(list((self.web / 'out/_next/native-build-007').iterdir()), [])

    def test_file_only_archive_roundtrip_retains_real_namespace(self):
        subject.check(self.web, True)
        archive_path = self.web / 'output.tar'
        with tarfile.open(archive_path, mode='w') as archive:
            for path in (self.web / 'out').rglob('*'):
                archive.add(path, arcname=path.relative_to(self.web / 'out').as_posix(), recursive=False)
        self.assertTrue(subject.check_archive(archive_path, 'native-build-007', '')['native_directory_preserved'])

    def test_file_only_archive_is_rejected(self):
        archive_path = self.web / 'file-only.tar'
        with tarfile.open(archive_path, mode='w'):
            pass
        with self.assertRaises(ValueError):
            subject.check_archive(archive_path, 'native-build-007', '')

    def test_marker_in_reserved_namespace_is_rejected(self):
        (self.web / 'out/_next/native-build-007/marker.json').write_text('{}')
        with self.assertRaises(ValueError):
            subject.check(self.web)

    def test_signed_export_is_never_modified(self):
        (self.web / 'out/manifest.sig').write_bytes(b'synthetic-signature')
        with self.assertRaises(ValueError):
            subject.check(self.web, True)
        self.assertEqual(list((self.web / 'out/_next/native-build-007').iterdir()), [])

    def test_native_identity_mismatch_is_not_guessed(self):
        (self.web / '.next/BUILD_ID').write_text('different-build-007')
        with self.assertRaises(ValueError):
            subject.check(self.web, True)


if __name__ == '__main__':
    unittest.main()
