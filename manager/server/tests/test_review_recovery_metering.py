"""R3/R4/R5: fair recovery with real wallet writes and bounded, honest reports."""
import json
import time
import tracemalloc
import unittest
from unittest import mock

from server import db, ledger, metering, portalbilling, portalstore as store, portalwallet as wallet
from server.tests import test_review_usage_repairs as usage_fixtures


class RecoveryMeteringRepairs(unittest.TestCase):
    setUp = usage_fixtures.UsageRepairs.setUp

    def test_manual_and_auto_close_have_one_immutable_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        rid=self.queue(prefix=0)
        barrier=threading.Barrier(2)
        def resolve(actor,amount):
            barrier.wait(timeout=5)
            try:
                wallet.resolve_unknown(rid,amount,actor,'synthetic race reason','synthetic race evidence')
                return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending=[workers.submit(resolve,wallet.AUTO_CLOSE_ACTOR,0),
                     workers.submit(resolve,'synthetic-human',0.0002)]
            self.assertEqual(sum(task.result(timeout=10) for task in pending),1)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_spend_resolutions WHERE request_id=?',(rid,)).fetchone()[0],1)
            settlements=conn.execute('SELECT COUNT(*) FROM portal_settlements WHERE request_id=?',(rid,)).fetchone()[0]
            unknown=conn.execute('SELECT COUNT(*) FROM portal_unknown_supplier_costs WHERE request_id=?',(rid,)).fetchone()[0]
            self.assertEqual(settlements+unknown,1)
            self.assertEqual(conn.execute('SELECT state FROM portal_spends WHERE request_id=?',(rid,)).fetchone()[0],'settled')

    def test_early_eligible_row_is_not_starved_by_fifty_deferred_tail_rows(self):
        self.queue(prefix=249)
        rid='repair-queue-150'
        db.execute("UPDATE request_logs SET status=499,terminal_state='client_cancelled' WHERE request_id=?",(rid,))
        for _ in range(3):wallet.auto_close_client_cancelled(limit=100)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT state FROM portal_spends WHERE request_id=?',(rid,)).fetchone()[0],'settled')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_spend_resolutions WHERE request_id=?',(rid,)).fetchone()[0],1)

    def test_failed_batch_processing_is_revisited_in_a_bounded_sweep(self):
        rid=self.queue(prefix=5)
        with mock.patch.object(wallet,'resolve_unknown',side_effect=RuntimeError('synthetic interrupted worker')):
            for _ in range(3):wallet.auto_close_client_cancelled(limit=2)
        for _ in range(4):wallet.auto_close_client_cancelled(limit=2)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT state FROM portal_spends WHERE request_id=?',(rid,)).fetchone()[0],'settled')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_spend_resolutions WHERE request_id=?',(rid,)).fetchone()[0],1)

    def test_backdated_end_is_eventually_swept_despite_continuously_growing_tail(self):
        rid=self.queue(prefix=5)
        stamp=int(time.time())
        self.assertEqual(wallet.auto_close_client_cancelled(limit=2),0)
        with store.transaction() as conn:
            conn.execute('UPDATE portal_request_ends SET ended_at=? WHERE request_id=?',(stamp-2000,rid))
        with mock.patch.object(store,'now',return_value=stamp+100):
            for sweep in range(12):
                with store.transaction() as conn:
                    for number in range(2):
                        new=f'continuous-tail-{sweep:02d}-{number}'
                        conn.execute("INSERT INTO portal_spends(request_id,owner_id,key_id,worker,state,created_at,held_units) VALUES(?,'synthetic-owner',1,'synthetic-worker','unknown',?,0)",(new,stamp+sweep))
                        conn.execute('INSERT INTO portal_request_ends VALUES(?,?,?,?,?)',(new,'synthetic-owner',1,'synthetic-worker',stamp+sweep))
                wallet.auto_close_client_cancelled(limit=2)
                with store.transaction() as conn:
                    if conn.execute('SELECT state FROM portal_spends WHERE request_id=?',(rid,)).fetchone()[0]=='settled':break
            else:self.fail('a growing tail starved the backdated ended request')

    def queue(self, prefix=100, *, known_credit=None, end=True):
        stamp = int(time.time())
        with store.transaction() as conn:
            conn.execute("INSERT INTO portal_users(id,username,pwd_hash,created_at) VALUES('synthetic-owner','synthetic','unused',?)", (stamp,))
            for number in range(prefix + 1):
                rid = f'repair-queue-{number:03d}'
                target = number == prefix
                conn.execute("INSERT INTO portal_spends(request_id,owner_id,key_id,worker,state,created_at,held_units) "
                             "VALUES(?, 'synthetic-owner',1,'synthetic-worker','unknown',?,?)", (rid, stamp-1000+number, 1000 if target else 0))
                if not target or end:
                    conn.execute('INSERT INTO portal_request_ends VALUES(?,?,?,?,?)',
                                 (rid,'synthetic-owner',1,'synthetic-worker',stamp-1000+number))
                if target:
                    conn.execute('INSERT INTO portal_spend_policies VALUES(?,?)', (rid,portalbilling.POLICY))
                    conn.execute("INSERT INTO portal_bonus_grants VALUES('synthetic-grant','synthetic-owner',1000,0,?,?,'synthetic','synthetic evidence',30)", (stamp-1000,stamp+1000))
                    conn.execute("INSERT INTO portal_bonus_allocations VALUES(?,'synthetic-grant',1000)", (rid,))
                db.add_request_log(ts=stamp,request_id=rid,key_id=1,model='synthetic',status=499 if target else 502,
                                   terminal_state='client_cancelled' if target else 'transport_error')
        db.execute(wallet.RECEIPTS)
        if known_credit is not None:
            db.execute('INSERT INTO portal_spend_receipts VALUES(?,?,?)',
                       (rid,'synthetic-owner',json.dumps({'amount_units':known_credit})))
        return rid

    def test_cancel_candidate_after_hundred_is_actually_released_exactly_once(self):
        rid = self.queue()
        self.assertEqual(wallet.auto_close_client_cancelled(limit=100), 0)
        self.assertEqual(wallet.auto_close_client_cancelled(limit=100), 1)
        for _ in range(3):
            self.assertEqual(wallet.auto_close_client_cancelled(limit=100), 0)
        with store.transaction() as conn:
            self.assertEqual(tuple(conn.execute('SELECT state,charged_units FROM portal_spends WHERE request_id=?', (rid,)).fetchone()), ('settled',0))
            self.assertEqual(conn.execute("SELECT remaining_units FROM portal_bonus_grants WHERE id='synthetic-grant'").fetchone()[0],1000)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_spend_resolutions WHERE request_id=?',(rid,)).fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_spends WHERE state='unknown'").fetchone()[0],100)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_settlements WHERE request_id=?',(rid,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_unknown_supplier_costs WHERE request_id=?',(rid,)).fetchone()[0],1)

    def test_late_nonzero_receipt_still_prevents_automatic_release(self):
        rid = self.queue(known_credit=500)
        self.assertEqual(wallet.auto_close_client_cancelled(limit=100), 0)
        self.assertEqual(wallet.auto_close_client_cancelled(limit=100), 0)
        with store.transaction() as conn:
            self.assertEqual(conn.execute('SELECT state FROM portal_spends WHERE request_id=?',(rid,)).fetchone()[0],'unknown')
            self.assertEqual(conn.execute("SELECT remaining_units FROM portal_bonus_grants WHERE id='synthetic-grant'").fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM portal_spend_resolutions').fetchone()[0],0)

    def test_end_proof_and_quarantine_and_spool_guards_are_retained(self):
        rid = self.queue(prefix=0,end=False)
        self.assertEqual(wallet.auto_close_client_cancelled(),0)
        with store.transaction() as conn:
            conn.execute('INSERT INTO portal_request_ends VALUES(?,?,?,?,?)',(rid,'synthetic-owner',1,'synthetic-worker',int(time.time())-1000))
            conn.execute("INSERT INTO portal_settings VALUES('restore_quarantine','true')")
        self.assertEqual(wallet.auto_close_client_cancelled(),0)
        with store.transaction() as conn:
            conn.execute("UPDATE portal_settings SET value='false' WHERE key='restore_quarantine'")
        with mock.patch.object(wallet,'replay_problem',return_value='synthetic pending receipt'):
            self.assertEqual(wallet.auto_close_client_cancelled(),0)
        self.assertEqual(wallet.auto_close_client_cancelled(),1)

    def test_corrupt_scan_cursor_resets_without_releasing_other_unknowns(self):
        self.queue(prefix=1)
        with store.transaction() as conn:
            conn.execute("INSERT INTO portal_settings VALUES('client_cancel_scan_cursor_v1','not JSON')")
        self.assertEqual(wallet.auto_close_client_cancelled(limit=1),0)
        self.assertEqual(wallet.auto_close_client_cancelled(limit=1),1)

    def log(self,rid,*,complete=1,pt=0,ct=0):
        db.add_request_log(ts=int(time.time()),request_id=rid,key_id=1,model='synthetic',status=200,
                           endpoint='/v1/chat/completions',protocol='openai',terminal_state='completed',
                           prompt_tokens=pt,completion_tokens=ct,usage_source='upstream',usage_complete=complete)

    def test_incomplete_usage_and_unknown_budget_never_report_settled(self):
        self.log('complete-zero')
        self.log('incomplete-zero',complete=0)
        self.log('incomplete-positive',complete=0,pt=7,ct=3)
        self.log('unknown-budget',complete=1,pt=1,ct=1)
        db.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) "
                   "VALUES('unknown-budget',1,0,'unknown',16,0,0,'synthetic',1,0)")
        before = db.connect().total_changes
        report = metering.reconcile(db.connect())
        self.assertEqual(report['spend_classes'][metering.SPEND_SETTLED],1)
        self.assertEqual(report['spend_classes'][metering.SPEND_UNKNOWN],3)
        self.assertEqual(db.connect().total_changes,before)
        self.assertEqual(db.query_one("SELECT state FROM budget_reservations WHERE request_id='unknown-budget'")[0],'unknown')

    def test_queries_fetch_only_selected_ids_and_bounded_orphan_window(self):
        stamp = int(time.time())
        for number in range(121):
            rid = f'related-{number:03d}'
            self.log(rid,pt=1,ct=2)
            ledger.record(rid,1,'synthetic','cn',1,2,0,True,'completed','synthetic')
            db.execute('INSERT INTO request_outcomes(request_id,ts,terminal_state,http_status) VALUES(?,?,?,?)',(rid,stamp,'completed',200))
        db.execute('INSERT INTO request_outcomes(request_id,ts,terminal_state,http_status) VALUES(?,?,?,?)',('recent-orphan',stamp+1,'completed',200))
        traced=[]
        conn=db.connect(); conn.set_trace_callback(traced.append)
        try:
            report=metering.reconcile(conn,limit=1)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(report['scanned_logs'],1)
        self.assertEqual(report['tables_seen']['usage_events'],1)
        self.assertEqual(report['tables_seen']['request_outcomes'],1)
        self.assertEqual(report['scope']['orphan_candidates'],1)
        self.assertEqual(report['findings']['outcome_without_log']['count'],1)
        self.assertFalse(report['scope']['complete_history'])
        for sql in traced:
            if any('FROM '+table in sql for table in ('usage_events','budget_reservations','forwarding_events','request_outcomes','request_logs')):
                self.assertIn('LIMIT',sql.upper(),sql)
                if any('FROM '+table in sql for table in ('usage_events','budget_reservations','forwarding_events')):
                    self.assertIn('request_id IN',sql,sql)

    def test_zero_negative_and_large_limits_are_bounded(self):
        self.log('synthetic-log')
        self.assertEqual(metering.reconcile(db.connect(),limit=0)['scanned_logs'],0)
        self.assertEqual(metering.reconcile(db.connect(),limit=-1)['scanned_logs'],0)
        self.assertEqual(metering.reconcile(db.connect(),limit=10**9)['scope']['effective_limit'],5000)

    def test_legacy_and_since_counts_are_explicit_window_counts(self):
        db.add_request_log(ts=1,model='synthetic',status=200)
        self.log('recent')
        self.assertEqual(metering.reconcile(db.connect(),limit=1)['findings']['log_without_request_id']['count'],0)
        self.assertEqual(metering.reconcile(db.connect(),limit=2)['findings']['log_without_request_id']['count'],1)
        self.assertEqual(metering.reconcile(db.connect(),since_ts=2)['findings']['log_without_request_id']['count'],0)

    def test_reconcile_heap_and_sql_work_do_not_scale_with_unselected_history(self):
        conn=db.connect()
        def grow(start,end):
            with db.transaction():
                conn.executemany("INSERT INTO request_logs(request_id,ts,model,prompt_tokens,completion_tokens,usage_source,usage_complete,terminal_state) "
                                 "VALUES(?,?,'synthetic',1,2,'upstream',1,'completed')",((f'scale-{n}',n) for n in range(start,end)))
                conn.executemany("INSERT INTO usage_events(event_id,request_id,attempt,event_type,ts,prompt_tokens,completion_tokens,counted,provenance) "
                                 "VALUES(?,?,1,'usage',?,1,2,1,'synthetic')",((f'scale-event-{n}',f'scale-{n}',n) for n in range(start,end)))
                conn.executemany("INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) "
                                 "VALUES(?,1,?,'settled',3,0,?,'synthetic',1,0)",((f'scale-{n}',n,n) for n in range(start,end)))
                conn.executemany("INSERT INTO forwarding_events(request_id,attempt,phase,ts) VALUES(?,1,'prepared',?)",((f'scale-{n}',n) for n in range(start,end)))
                conn.executemany("INSERT INTO request_outcomes(request_id,ts,terminal_state,http_status) VALUES(?,?,'completed',200)",((f'scale-{n}',n) for n in range(start,end)))
        def profile():
            work=0
            def progress():
                nonlocal work
                work+=100
                return 0
            conn.set_progress_handler(progress,100)
            tracemalloc.start()
            try:
                result=metering.reconcile(conn,limit=1)
                peak=tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
                conn.set_progress_handler(None,0)
            self.assertEqual(result['scanned_logs'],1)
            self.assertTrue(all(value==1 for value in result['tables_seen'].values()))
            return {'rows_per_table':conn.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0],
                    'sqlite_vm_steps':work,'python_peak_bytes':peak}
        grow(0,1000)
        small=profile()
        grow(1000,50000)
        large=profile()
        self.assertLessEqual(large['sqlite_vm_steps'],small['sqlite_vm_steps']*3+1000,(small,large))
        self.assertLessEqual(large['python_peak_bytes'],small['python_peak_bytes']+250000,(small,large))
        print('synthetic_reconcile_scale='+json.dumps({'small':small,'large':large}))
