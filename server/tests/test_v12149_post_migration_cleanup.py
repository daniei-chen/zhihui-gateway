"""Post-migration cleanup must report whether every recovery item succeeded."""
from __future__ import annotations

import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'post-migration-cleanup.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8')


class ItRefusesToOpenTheGateEarlyTests(unittest.TestCase):
    def test_it_requires_a_transaction_id(self):
        self.assertIn('[ -z "$TXN" ]', SOURCE)
        self.assertIn('exit 2', SOURCE)

    def test_it_requires_phase_complete(self):
        self.assertIn("value.get('phase') != 'complete'", SOURCE)

    def test_guards_precede_any_marker_removal(self):
        first_rm = SOURCE.find('rm -f "$marker"')
        self.assertGreater(first_rm, 0)
        self.assertLess(SOURCE.find('[ -z "$TXN" ]'), first_rm)
        self.assertLess(SOURCE.find("value.get('phase') != 'complete'"), first_rm)


class ItRestoresAndVerifiesEverythingTests(unittest.TestCase):
    def test_it_clears_start_blocked_markers(self):
        self.assertIn('start-blocked', SOURCE)
        self.assertIn('rm -f "$marker"', SOURCE)

    def test_it_reloads_systemd_after_clearing(self):
        self.assertIn('systemctl daemon-reload', SOURCE)

    def test_it_restarts_and_verifies_the_budget_timer(self):
        self.assertIn('wb-budget-autoreconcile.timer', SOURCE)
        self.assertIn('is-active --quiet "$BUDGET_TIMER"', SOURCE)
        self.assertIn('NextElapseUSecRealtime', SOURCE)

    def test_it_clears_an_old_failed_reconciliation_alarm(self):
        self.assertIn('reset-failed "$BUDGET_SERVICE"', SOURCE)
        self.assertIn('is-failed --quiet "$BUDGET_SERVICE"', SOURCE)

    def test_it_requires_the_primary_service_to_be_running(self):
        self.assertIn('is-active --quiet workbuddy-web.service', SOURCE)


class ItFailsClosedTests(unittest.TestCase):
    def test_documented_failures_exit_nonzero(self):
        self.assertIn('FAILED=1', SOURCE)
        self.assertIn('exit 1', SOURCE)

    def test_success_only_after_verification(self):
        self.assertIn('收尾完成且全部恢复项已复核', SOURCE)
        self.assertLess(SOURCE.find('[ "$FAILED" -ne 0 ]'), SOURCE.rfind('exit 0'))

    def test_it_does_not_set_e(self):
        self.assertIn('set -uo pipefail', SOURCE)
        self.assertNotIn('set -euo', SOURCE)
        self.assertNotIn('set -eu', SOURCE)

    def test_it_never_auto_rolls_back_completed_release(self):
        self.assertNotIn('rollback', SOURCE.lower())


if __name__ == '__main__':
    unittest.main()
