"""Release provenance must bind every reviewed source byte to this checkout."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseProvenanceIntegrityTests(unittest.TestCase):
    def test_reviewed_patch_files_match_current_source(self) -> None:
        value = json.loads((ROOT / 'server/release-provenance.json').read_text(encoding='utf-8'))
        files = value.get('reviewed_patch_files')
        self.assertIsInstance(files, dict)
        self.assertGreater(len(files), 0)
        for relative, expected in files.items():
            self.assertIsInstance(relative, str)
            path = ROOT / Path(relative)
            self.assertTrue(path.is_file(), relative)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)


if __name__ == '__main__':
    unittest.main()
