"""Malformed archives never extract files or acquire publisher approval."""
import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from server import packagecheck as subject


class PackageCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, extras=()):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            for name, data, kind in [('release/.version', b'v0.0.4\n', tarfile.REGTYPE), *extras]:
                member = tarfile.TarInfo(name); member.type = kind
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                member.linkname = '../../outside'
                archive.addfile(member, io.BytesIO(data))
        return gzip.compress(output.getvalue())

    def check(self, content):
        path = self.root / 'input.gz'; path.write_bytes(content)
        return subject.inspect_package(path)

    def test_complete_archive_is_not_publisher_approval_and_writes_no_members(self):
        result = self.check(self.archive([('release/server/main.py', b'not executed', tarfile.REGTYPE)]))
        self.assertEqual(result['version'], 'v0.0.4')
        self.assertFalse(result['publisher_verified'])
        self.assertEqual([p.name for p in self.root.iterdir()], ['input.gz'])

    def test_truncated_crc_trailing_and_concatenated_data_are_refused(self):
        valid = self.archive()
        for raw in (valid[:-1], valid[:-8], valid[:-10] + b'broken-tail', valid + b'extra', valid + valid,
                    gzip.compress(gzip.decompress(valid)[:1024])):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                self.check(raw)

    def test_duplicate_version_alias_traversal_links_and_devices_are_refused(self):
        extras = [('release/.version', b'v0.0.6', tarfile.REGTYPE),
            ('release/../outside', b'x', tarfile.REGTYPE), ('/absolute', b'x', tarfile.REGTYPE),
            ('release/back\\slash', b'x', tarfile.REGTYPE), ('release/link', b'', tarfile.SYMTYPE),
            ('release/device', b'', tarfile.CHRTYPE), ('release/.VERSION', b'v0.0.6', tarfile.REGTYPE)]
        for item in extras:
            with self.subTest(name=item[0]), self.assertRaises(ValueError):
                self.check(self.archive([item]))

    def test_hidden_tar_and_resource_bounds_are_refused(self):
        tar = gzip.decompress(self.archive())
        with self.assertRaises(ValueError):
            self.check(gzip.compress(tar + tar))
        for field, limit in (('MAX_MEMBERS', 0), ('MAX_EXPANDED', 512), ('MAX_FILE', 2), ('MAX_COMPRESSED', 1)):
            with self.subTest(field=field), mock.patch.object(subject, field, limit), self.assertRaises(ValueError):
                self.check(self.archive())
