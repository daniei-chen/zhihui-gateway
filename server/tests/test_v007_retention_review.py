import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from server import archival, archiveworker, budget, budgetreview, config, db, diagnostics
from server.routers import logs


class RetentionReviewTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = config.DB_PATH
        config.DB_PATH = self.root / 'synthetic.db'
        db._conn = None
        db.connect()
        budget.initialize()
        self.connection = db._conn
        self.out = self.root / 'archives'
        self.old = int(time.time()) - 100 * 86400
        db.execute("INSERT INTO api_keys(id,name,key_hash,prefix,created_at,used_credit_units) VALUES(1,'synthetic','synthetic','synthetic',0,123000020)")
        for index in range(3):
            db.execute('INSERT INTO request_logs(ts,key_id,model,status,request_id,credit) VALUES(?,1,?,?,?,?)',
                       (self.old + index, 'synthetic-model', 200, 'synthetic-' + str(index), 0.1))

    def tearDown(self):
        db._conn.close()
        db._conn = None
        config.DB_PATH = self.original

    def archive(self, **options):
        return archival.archive_dataset(self.connection, 'request_logs', out_dir=self.out,
                                         before_ts=int(time.time()), **options)

    def prune(self, receipt):
        return archival.delete_archived(self.connection, 'request_logs', watermark=receipt['watermark'],
                                        sha=receipt['sha256'], confirm_sha=receipt['sha256'])

    def reservation(self, request_id, *, state='unknown'):
        db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,'
                   'reserved_credit,charged_tokens,charged_credit,updated_at,owner,fence,lease_until) '
                   "VALUES(?,1,?,?,10,0,0,0,?,'synthetic-owner',0,0)",
                   (request_id, self.old, state, self.old))

    def test_later_eligible_row_below_watermark_is_not_deleted(self):
        db.execute('UPDATE request_logs SET request_id=NULL WHERE request_id=?', ('synthetic-0',))
        receipt = self.archive()
        db.execute('UPDATE request_logs SET request_id=? WHERE request_id IS NULL', ('synthetic-late',))
        self.assertEqual(self.prune(receipt)['deleted_rows'], 2)
        self.assertEqual(db.query_one('SELECT request_id FROM request_logs')['request_id'], 'synthetic-late')

    def test_changed_row_stays_and_lifetime_usage_is_untouched(self):
        receipt = self.archive()
        db.execute('UPDATE request_logs SET credit=0.2 WHERE request_id=?', ('synthetic-0',))
        self.assertEqual(self.prune(receipt)['deleted_rows'], 2)
        self.assertEqual(db.query_one('SELECT credit FROM request_logs')['credit'], 0.2)
        self.assertEqual(db.query_one('SELECT used_credit_units FROM api_keys')['used_credit_units'], 123000020)

    def test_unknown_rows_stay_until_safe_later_rescan(self):
        self.reservation('synthetic-0')
        receipt = self.archive()
        self.assertEqual(receipt['rows'], 2)
        db.execute("UPDATE budget_reservations SET state='settled' WHERE request_id='synthetic-0'")
        self.assertEqual(self.prune(receipt)['deleted_rows'], 2)
        later = self.archive(restart_scan=True)
        self.assertEqual(later['rows'], 1)
        self.assertEqual(self.prune(later)['deleted_rows'], 1)

    def test_all_jsonl_lines_are_parsed_not_just_samples(self):
        target = self.root / 'corrupt.jsonl'
        encoded = b'{"id":1}\n{broken\n'
        target.write_bytes(encoded)
        result = archival.verify_archive(target, expected_sha=hashlib.sha256(encoded).hexdigest())
        self.assertFalse(result['ok'])
        self.assertEqual(result['unparsable_rows'], 1)

    def test_worker_is_bounded_and_never_releases_unknown(self):
        self.reservation('synthetic-0')
        first = archiveworker.run_once(config.DB_PATH, self.out, max_rows=1)
        self.assertEqual(first['deleted_rows'], 1)
        self.assertTrue(first['pending']['request_logs'])
        second = archiveworker.run_once(config.DB_PATH, self.out, max_rows=1)
        self.assertEqual(second['deleted_rows'], 1)
        self.assertEqual(db.query_one('SELECT request_id FROM request_logs')['request_id'], 'synthetic-0')
        self.assertEqual(db.query_one('SELECT state FROM budget_reservations')['state'], 'unknown')
        self.assertEqual(db.query_one('SELECT used_credit_units FROM api_keys')['used_credit_units'], 123000020)

    def test_no_delete_when_archive_sync_fails_or_disabled(self):
        with mock.patch.object(archival.os, 'fsync', side_effect=OSError('synthetic full disk')):
            with self.assertRaises(OSError):
                archiveworker.run_once(config.DB_PATH, self.out)
        self.assertEqual(db.query_one('SELECT COUNT(*) count FROM request_logs')['count'], 3)
        with mock.patch.object(config, 'ARCHIVE_ENABLED', False):
            db._prune_request_logs()
        self.assertEqual(db.query_one('SELECT COUNT(*) count FROM request_logs')['count'], 3)

    def test_manual_clear_cannot_bypass_archive_proof(self):
        with self.assertRaises(HTTPException) as failure:
            logs.clear_logs({'username': 'synthetic-admin'})
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(db.query_one('SELECT COUNT(*) count FROM request_logs')['count'], 3)

    def test_review_pagination_evidence_and_no_mutation(self):
        for index in range(3):
            self.reservation('synthetic-' + str(index))
        first = budgetreview.page(limit=2)
        self.assertEqual(len(first['items']), 2)
        second = budgetreview.page(limit=2, cursor=first['next_cursor'])
        self.assertEqual(len(second['items']), 1)
        self.assertIsNone(second['next_cursor'])
        self.assertIsNone(first['items'][0]['wallet_frozen'])
        evidence = budgetreview.evidence('synthetic-0')
        self.assertFalse(evidence['can_infer_zero_from_cancellation'])
        self.assertIsNone(evidence['transport_end'])
        self.assertEqual(db.query_one("SELECT COUNT(*) count FROM budget_reservations WHERE state='unknown'")['count'], 3)

    def test_review_rejects_malformed_cursor_and_bucket(self):
        for cursor in ('not-base64', 'a' * 300, 'WzFd'):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                budgetreview.page(cursor=cursor)
        with self.assertRaises(ValueError):
            budgetreview.page(bucket='clear-all')

    def test_review_archive_bucket_does_not_hide_supplier_rejection(self):
        request_id = 'synthetic-rejected'
        self.reservation(request_id)
        db.execute(
            'INSERT INTO request_logs(ts,key_id,status,request_id,terminal_state,'
            'usage_source,error) VALUES(?,?,?,?,?,?,?)',
            (self.old, 1, 502, request_id, 'upstream_error', 'unknown',
             '{"error":{"code":"no_healthy_account"}}'))
        # Preserve a historical marker that was incorrectly applied before the
        # current archive policy excluded upstream_rejected.
        db.execute(
            'UPDATE budget_reservations SET resolution=? WHERE request_id=?',
            (budget.ARCHIVE_RESOLUTION_PREFIX + 'historical marker', request_id))

        archived = budgetreview.page(bucket='archived_pending', limit=100)
        rejected = budgetreview.page(bucket='upstream_rejected', limit=100)

        self.assertNotIn(request_id, {item['request_id'] for item in archived['items']})
        item = next(item for item in rejected['items'] if item['request_id'] == request_id)
        self.assertEqual(item['class'], 'upstream_rejected')
        self.assertFalse(item['archived_pending'])

    def test_diagnostics_are_read_only_bounded_and_do_not_infer_wallet_freeze(self):
        self.reservation('synthetic-0')
        before = self.connection.total_changes
        report = diagnostics.inspect(config.DB_PATH, max_rows=1)
        self.assertTrue(report['read_only'])
        self.assertFalse(report['inference_health_proven'])
        self.assertIsNone(report['wallet_frozen'])
        self.assertEqual(report['unresolved_main_budget']['unknown'], 1)
        self.assertEqual(before, self.connection.total_changes)
        self.assertNotIn('synthetic-old', json.dumps(report))

    def test_refresh_lease_files_are_never_unlinked_by_cleanup(self):
        from deploy import asset_lifecycle
        target = self.root / 'workbuddy-synthetic.json.refresh.lock'
        self.assertIsNotNone(asset_lifecycle.denied(target, None, set()))

    def test_diagnostics_redacts_unrecognized_metadata(self):
        self.assertEqual(diagnostics._metadata('error_code', 'synthetic-sensitive-value'), 'other')
        self.assertEqual(diagnostics._metadata('protocol', 'synthetic-sensitive-value'), 'other')
        self.assertEqual(diagnostics._metadata('release_id', 'synthetic-sensitive-value'), 'other')
        self.assertEqual(diagnostics._metadata('error_code', '11128'), '11128')
