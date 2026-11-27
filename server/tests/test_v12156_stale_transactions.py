"""Migration cleanup preserves all transaction evidence, including abandoned attempts.

Old string checks expected recursive deletion by version prefix. The current
completed-transaction contract retains history; run the actual script with a
temporary state directory and a fake service controller.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/post-migration-cleanup.sh'


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'requires Bash')
class TransactionEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='zhihui-cleanup-evidence-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.transactions = self.state / 'transactions'
        self.transactions.mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        controller = self.bin / 'systemctl'
        controller.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CLEANUP_COMMAND_LOG"
if [ "$1" = is-failed ]; then exit 1; fi
if [ "$1" = daemon-reload ] && [ "${CLEANUP_FAIL_RELOAD:-0}" = 1 ]; then exit 1; fi
if [ "$1" = show ]; then printf 'synthetic-next-run\\n'; fi
exit 0
''')
        controller.chmod(0o755)
        source = SCRIPT.read_text()
        self.assertEqual(source.count('STATE=/opt/zhihui-release/writer-state'), 1)
        self.script = self.root / 'cleanup.sh'
        self.script.write_text(source.replace('STATE=/opt/zhihui-release/writer-state',
                                             f'STATE="{self.state}"'))
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'CLEANUP_COMMAND_LOG': str(self.root / 'commands.log')}
        self.markers = [self.state / (name + '.start-blocked') for name in
                        ('workbuddy-web.service', 'workbuddy-web-helper.service')]
        for marker in self.markers:
            marker.write_text('block-start')
        for name, phase in (('v12155a-current', 'complete'), ('v12155b-empty', ''),
                            ('v12155c-prepared', 'prepared'), ('v12155d-complete', 'complete'),
                            ('v12154a-prepared', 'prepared'), ('foreign-name', 'prepared')):
            path = self.transactions / name
            path.mkdir()
            (path / 'active.json').write_text(json.dumps({'phase': phase}, indent=2))
            (path / 'evidence.bin').write_bytes(b'preserve-exactly\x00' + name.encode())
        self.before = self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.transactions)): path.read_bytes()
                for path in self.transactions.rglob('*') if path.is_file()}

    def run_cleanup(self, transaction='v12155a-current', **environment):
        return subprocess.run(['bash', str(self.script), transaction], env={**self.env, **environment},
                              capture_output=True, text=True, timeout=10)

    def assert_guard_refuses_without_mutations(self, transaction='v12155a-current'):
        before = self.snapshot()
        self.assertNotEqual(self.run_cleanup(transaction).returncode, 0)
        self.assertTrue(all(path.read_text() == 'block-start' for path in self.markers))
        self.assertFalse((self.root / 'commands.log').exists())
        self.assertEqual(self.snapshot(), before)

    def test_success_preserves_own_other_versions_and_abandoned_evidence(self):
        result = self.run_cleanup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue(all(not path.exists() for path in self.markers))

    def test_failed_service_verification_preserves_every_transaction(self):
        result = self.run_cleanup(CLEANUP_FAIL_RELOAD='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_prepared_and_empty_phase_never_open_start_gates(self):
        for transaction in ('v12155b-empty', 'v12155c-prepared', 'missing'):
            with self.subTest(transaction=transaction):
                self.assertNotEqual(self.run_cleanup(transaction).returncode, 0)
                self.assertTrue(all(path.exists() for path in self.markers))
                self.assertFalse((self.root / 'commands.log').exists())
                self.assertEqual(self.snapshot(), self.before)

    def test_embedded_complete_text_and_malformed_json_are_not_completion(self):
        journal = self.transactions / 'v12155a-current' / 'active.json'
        for value in ('{"phase":"prepared","nested":{"phase":"complete"}}',
                      '{broken "phase":"complete"', '[]'):
            with self.subTest(value=value):
                journal.write_text(value)
                self.assertNotEqual(self.run_cleanup().returncode, 0)
                self.assertTrue(all(path.exists() for path in self.markers))
                self.assertFalse((self.root / 'commands.log').exists())

    def test_empty_unknown_and_path_traversal_identifiers_fail_without_effects(self):
        for value in ('', '../v12155a-current', '/tmp/current', '..', 'name\nother'):
            with self.subTest(value=value):
                self.assertNotEqual(self.run_cleanup(value).returncode, 0)
                self.assertTrue(all(path.exists() for path in self.markers))
                self.assertEqual(self.snapshot(), self.before)
                self.assertFalse((self.root / 'commands.log').exists())

    def test_symlink_transaction_cannot_borrow_external_completion(self):
        outside = self.root / 'outside-transaction'
        outside.mkdir()
        journal = outside / 'active.json'
        journal.write_text('{"phase":"complete"}')
        (self.transactions / 'linked-transaction').symlink_to(outside, target_is_directory=True)
        self.assert_guard_refuses_without_mutations('linked-transaction')
        self.assertEqual(journal.read_text(), '{"phase":"complete"}')

    def test_symlink_journal_cannot_borrow_external_completion(self):
        outside = self.root / 'outside-journal.json'
        outside.write_text('{"phase":"complete"}')
        journal = self.transactions / 'v12155a-current' / 'active.json'
        journal.unlink()
        journal.symlink_to(outside)
        self.assert_guard_refuses_without_mutations()
        self.assertEqual(outside.read_text(), '{"phase":"complete"}')

    def test_duplicate_phase_is_invalid_in_either_order(self):
        journal = self.transactions / 'v12155a-current' / 'active.json'
        for value in ('{"phase":"prepared","phase":"complete"}',
                      '{"phase":"complete","phase":"prepared"}'):
            with self.subTest(value=value):
                journal.write_text(value)
                self.assert_guard_refuses_without_mutations()

    def test_oversized_complete_journal_is_rejected_before_cleanup(self):
        journal = self.transactions / 'v12155a-current' / 'active.json'
        journal.write_text(json.dumps({'phase': 'complete', 'padding': 'x' * (1024 * 1024)}))
        self.assert_guard_refuses_without_mutations()


if __name__ == '__main__':
    unittest.main()
