"""Homepage tooling must never replace the user's selected original login."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SITE = Path(__file__).resolve().parents[1]


class LoginPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='zh-login-preservation-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.site = self.root / 'site'
        (self.site / 'tools').mkdir(parents=True)
        (self.site / 'login').mkdir()
        for name in ('import-home-export.py', 'materialize-home-source.py'):
            shutil.copy2(SITE / 'tools' / name, self.site / 'tools' / name)
        for name in ('index.html', 'preservation.json'):
            shutil.copy2(SITE / 'login' / name, self.site / 'login' / name)
        self.original = (self.site / 'login/index.html').read_bytes()
        self.source = self.root / 'input'
        (self.source / 'login').mkdir(parents=True)
        (self.source / 'index.html').write_text('<!doctype html><title>Homepage fixture</title>')
        (self.source / 'login/index.html').write_text('<h1>Rejected new login concept</h1>')

    def command(self, name, *args):
        return subprocess.run([sys.executable, '-I', '-B', str(self.site / 'tools' / name),
                               *map(str, args)], capture_output=True, timeout=20)

    def test_import_preserves_exact_login_and_excludes_rejected_source(self):
        result = self.command('import-home-export.py', self.source)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual((self.site / 'login/index.html').read_bytes(), self.original)
        manifest = json.loads((self.site / 'home-import.json').read_bytes())
        self.assertEqual(manifest['login']['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertNotIn('login/index.html', [row['path'] for row in manifest['files']])
        self.assertEqual(len(list((self.site / 'home').rglob('index.html'))), 1)
        again = self.command('import-home-export.py', self.source)
        self.assertEqual(again.returncode, 0)
        self.assertEqual((self.site / 'login/index.html').read_bytes(), self.original)

    def test_login_drift_blocks_import_before_homepage_write(self):
        altered = self.original + b'<!-- unrelated edit -->'
        (self.site / 'login/index.html').write_bytes(altered)
        result = self.command('import-home-export.py', self.source)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.site / 'home/index.html').exists())
        self.assertEqual((self.site / 'login/index.html').read_bytes(), altered)

    def test_materialization_never_exports_production_login_or_replaces_it(self):
        imported = self.command('import-home-export.py', self.source)
        self.assertEqual(imported.returncode, 0)
        materialized = self.command('materialize-home-source.py')
        self.assertEqual(materialized.returncode, 0, materialized.stderr.decode(errors='replace'))
        public = self.site / 'home-source/public'
        self.assertTrue((public / 'index.html').is_file())
        self.assertFalse((public / 'login').exists())
        self.assertEqual((self.site / 'login/index.html').read_bytes(), self.original)
        (public / 'index.html').write_text('local edit')
        self.assertNotEqual(self.command('materialize-home-source.py').returncode, 0)
        self.assertEqual((public / 'index.html').read_text(), 'local edit')


if __name__ == '__main__':
    unittest.main()
