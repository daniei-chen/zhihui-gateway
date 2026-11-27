"""A missing release check must fail the command and its recorded summary."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tools import ci_regression


class RequiredGateTests(unittest.TestCase):
    def run_gate(self, flags, frontend, static):
        cwd = Path.cwd()
        backend = unittest.TestResult()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                with (mock.patch.object(ci_regression, 'ROOT', root),
                      mock.patch.object(ci_regression, 'MODULES', ()),
                      mock.patch.object(ci_regression, 'frontend_suite', return_value=frontend),
                      mock.patch.object(ci_regression, 'static_suite', return_value=static),
                      mock.patch.object(unittest, 'TextTestRunner') as runner,
                      mock.patch.object(sys, 'path', list(sys.path)),
                      mock.patch.dict(os.environ, {'ZH_CI_NET_ISOLATED': '0'}),
                      mock.patch('server.budgetowner.close'),
                      mock.patch('server.db._close_connection_locked'),
                      contextlib.redirect_stderr(io.StringIO())):
                    runner.return_value.run.return_value = backend
                    status = ci_regression.main(flags)
                    summary = json.loads((root / 'ci-validation-summary.json').read_text())
                return status, summary
            finally:
                os.chdir(cwd)

    def test_missing_required_frontend_fails_and_records_reason(self):
        status, summary = self.run_gate(
            ['--require-frontend'], {'skipped': True, 'reason': 'npm missing'}, {'ok': True})
        self.assertEqual(status, 1)
        self.assertFalse(summary['frontend']['ok'])
        self.assertEqual(summary['frontend']['reason'], 'npm missing')

    def test_missing_required_static_fails_and_records_reason(self):
        status, summary = self.run_gate(
            ['--require-static'], {'skipped': True, 'reason': 'separate frontend job'},
            {'skipped': True, 'reason': 'ruff not installed'})
        self.assertEqual(status, 1)
        self.assertFalse(summary['static']['ok'])
        self.assertTrue(summary['static']['required'])

    def test_local_optional_checks_remain_explicitly_skipped(self):
        status, summary = self.run_gate([], {'skipped': True}, {'skipped': True})
        self.assertEqual(status, 0)
        self.assertTrue(summary['frontend']['skipped'])
        self.assertFalse(summary['frontend']['required'])

    def test_required_checks_must_both_succeed(self):
        for frontend, static in (({'ok': False}, {'ok': True}),
                                 ({'ok': True}, {'ok': False}),
                                 ({}, {'ok': True})):
            with self.subTest(frontend=frontend, static=static):
                status, _ = self.run_gate(
                    ['--require-frontend', '--require-static'], frontend, static)
                self.assertEqual(status, 1)
        status, summary = self.run_gate(
            ['--require-frontend', '--require-static'], {'ok': True}, {'ok': True})
        self.assertEqual(status, 0)
        self.assertTrue(summary['static']['required'])


if __name__ == '__main__':
    unittest.main()
