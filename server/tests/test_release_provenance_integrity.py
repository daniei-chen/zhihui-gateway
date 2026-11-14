"""Release provenance must bind every reviewed source byte to this checkout."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseProvenanceIntegrityTests(unittest.TestCase):
    @staticmethod
    def _source_hashes(path: Path) -> set[str]:
        raw = path.read_bytes()
        values = {hashlib.sha256(raw).hexdigest()}
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            return values
        normalized = text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
        values.add(hashlib.sha256(normalized).hexdigest())
        return values

    def test_reviewed_patch_files_match_current_source(self) -> None:
        value = json.loads((ROOT / 'server/release-provenance.json').read_text(encoding='utf-8'))
        files = value.get('reviewed_patch_files')
        self.assertIsInstance(files, dict)
        self.assertGreater(len(files), 0)
        for relative, expected in files.items():
            self.assertIsInstance(relative, str)
            path = ROOT / Path(relative)
            self.assertTrue(path.is_file(), relative)
            self.assertIn(expected, self._source_hashes(path), relative)


if __name__ == '__main__':
    unittest.main()
