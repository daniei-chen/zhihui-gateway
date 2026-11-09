"""Unknown age or missing charges cannot stand in for provider billing facts."""
from unittest import mock
import io
from contextlib import redirect_stdout

from server import budget, budget_reconcile, db, ledger, operational
from server.tests import test_late_usage_race as fixtures


class AutomaticZeroEvidenceTest(fixtures._RaceFixture):
    def seed(self, request_id, *, confirmed=False):
        self._reserve(request_id, reserved=0, age=10 * 86400)
        self._rejected_log(request_id)
        if confirmed:
            self._zero_receipt(request_id)

    def test_all_automatic_entrypoints_keep_unreported_rejected_requests_unknown(self):
        self.seed('no-receipt')
        before = self._usage()
        self.assertEqual(operational.reconcile_zero_reservation_unknowns(self.now), 0)
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 0)
        self.assertFalse(budget.resolve('no-receipt', 0, 0, 'auto-reconcile', 'legacy timer'))
        self.assertFalse(budget.resolve('no-receipt', 0, 0, 'fixture', 'zero-only', zero_only=True))
        self._assert_nothing_written('no-receipt', before)
        summary = budget.unresolved_summary(now=self.now)['summary']
        self.assertEqual(summary['auto_reconcilable'], 0)
        self.assertEqual(summary['needs_human_old'], 1)

    def test_only_complete_matching_immutable_zero_evidence_authorizes_automation(self):
        variations = [
            ('missing-immutable', {'usage_source': 'upstream', 'usage_complete': 1, 'credit': 0}, None),
            ('missing-credit', {'usage_source': 'upstream', 'usage_complete': 1}, 0),
            ('incomplete', {'usage_source': 'upstream', 'usage_complete': 0, 'credit': 0}, 0),
            ('partial-tokens', {'usage_source': 'upstream', 'usage_complete': 1, 'credit': 0, 'completion_tokens': 1}, 0),
            ('estimated', {'usage_source': 'estimated', 'usage_complete': 1, 'credit': 0}, 0),
            ('conflicting-event', {'usage_source': 'upstream', 'usage_complete': 1, 'credit': 0}, 0.25),
            ('fractional-nonzero', {'usage_source': 'upstream', 'usage_complete': 1, 'credit': 0.0000001}, 0),
        ]
        for rid, patch, event_credit in variations:
            with self.subTest(rid=rid):
                self.seed(rid)
                columns = ','.join(k + '=?' for k in patch)
                db.execute('UPDATE request_logs SET ' + columns + ' WHERE request_id=?', (*patch.values(), rid))
                if event_credit is not None:
                    ledger.record(rid, self.key['id'], 'm', 'cn', 0, 0, event_credit, False, 'completed', 'fixture')
                self.assertFalse(budget.resolve(rid, 0, 0, 'auto-reconcile', 'fixture'))
                self.assertEqual(self._row(rid)['state'], 'unknown')
        self.seed('proven', confirmed=True)
        self.assertTrue(budget.resolve('proven', 0, 0, 'auto-reconcile', 'fixture'))
        self.assertEqual(self._row('proven')['state'], 'settled')

    def test_evidence_rechecked_after_scan_inside_transaction(self):
        self.seed('changed', confirmed=True)
        original = budget.resolve
        def change_before_transaction(rid, *args, **kwargs):
            db.execute('UPDATE request_logs SET credit=NULL,usage_complete=0 WHERE request_id=?', (rid,))
            return original(rid, *args, **kwargs)
        with mock.patch.object(budget, 'resolve', side_effect=change_before_transaction):
            self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 0)
        self._assert_nothing_written('changed', self._usage())

    def test_timer_cannot_convert_positive_usage_or_bypass_manual_correction_policy(self):
        self.seed('positive', confirmed=True)
        self.assertFalse(budget.resolve('positive', 1, 0, 'auto-reconcile', 'fixture'))
        self.assertTrue(budget.resolve('positive', 1, 0, 'admin', 'manually verified fixture'))
        self.assertEqual(self._row('positive')['charged_tokens'], 1)

    def test_provider_zero_receipt_does_not_require_absence_of_upstream_attempts(self):
        self.seed('actually-zero', confirmed=True)
        self._attempt('actually-zero', 'aggregate_terminal')
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 1)

    def test_summary_fetches_zero_evidence_in_batches_not_per_request(self):
        for index in range(40):
            self.seed('batch-' + str(index), confirmed=index % 2 == 0)
        with mock.patch.object(db, 'query', wraps=db.query) as queries:
            summary = budget.unresolved_summary(now=self.now)['summary']
        self.assertEqual(summary['auto_reconcilable'], 20)
        self.assertEqual(summary['needs_human'], 20)
        self.assertLess(queries.call_count, 10)

    def test_guarded_scheduled_job_reports_counts_without_identifiers(self):
        self.seed('still-unknown')
        self.seed('confirmed-zero', confirmed=True)
        result = budget_reconcile.run(self.now)
        self.assertEqual(result, {'settled_confirmed_zero': 1, 'unknown_remaining': 1,
                                  'needs_human': 1, 'archived_pending': 0})
        self.assertEqual(self._row('still-unknown')['state'], 'unknown')
        self.assertEqual(budget_reconcile.run(self.now)['settled_confirmed_zero'], 0)

    def test_scheduled_failure_is_nonzero_and_does_not_echo_exception_secrets(self):
        output = io.StringIO()
        with mock.patch.object(db, 'connect'), mock.patch.object(budget, 'initialize'), \
                mock.patch.object(budget_reconcile, 'run', side_effect=RuntimeError('synthetic-secret')), \
                mock.patch.object(budget_reconcile.budgetowner, 'close') as close_owner, \
                mock.patch.object(db, '_close_connection_locked') as close_db, redirect_stdout(output):
            self.assertEqual(budget_reconcile.main(), 1)
        self.assertNotIn('synthetic-secret', output.getvalue())
        close_owner.assert_called_once()
        close_db.assert_called_once()
