"""Standalone backup must not add executable caches to signed releases."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BackupBytecodePolicyTests(unittest.TestCase):
    def test_direct_entry_disables_bytecode_without_launcher_environment(self):
        source = Path(__file__).resolve().parents[1] / 'backup_application.py'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'fixture_backupstatus.py').write_text('verified = True\n', encoding='utf-8')
            program = """
import json, runpy, sys, types
sys.dont_write_bytecode = False
if sys.platform == 'win32':
    sys.modules['fcntl'] = types.ModuleType('fcntl')
runpy.run_path(sys.argv[1], run_name='backup_policy_fixture')
sys.path.insert(0, sys.argv[2])
import fixture_backupstatus
assert sys.dont_write_bytecode is True
assert fixture_backupstatus.verified is True
print(json.dumps({'bytecode_disabled': sys.dont_write_bytecode}))
"""
            environment = dict(os.environ)
            environment.pop('PYTHONDONTWRITEBYTECODE', None)
            environment.pop('PYTHONPYCACHEPREFIX', None)
            result = subprocess.run([sys.executable, '-I', '-c', program, str(source), str(root)],
                                    env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"bytecode_disabled": true', result.stdout)
            self.assertFalse((root / '__pycache__').exists())
            self.assertEqual(list(root.glob('*.pyc')), [])
