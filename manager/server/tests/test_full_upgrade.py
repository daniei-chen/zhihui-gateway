import asyncio
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from server import budget,config,db,observability,pooledhttp,budgetowner

def _reserve_from_process(database,request_id,gate,ready,results,key_id):
    token = None
    try:
        if db._conn:
            db._conn.close()
        db._conn=None;config.DB_PATH=Path(database);db.connect()
        state=observability.RequestAudit(request_id,'/v1/chat/completions','POST','openai',time.monotonic())
        token=observability.current.set(state)
        ready.set()
        if not gate.wait(90):
            raise RuntimeError('Parent did not open the competition barrier')
        budget.prepare(dict(db.query_one('SELECT * FROM api_keys WHERE id=?',(key_id,))),{'messages':[]})
        results.put('reserved')
    except budget.BudgetRejected:results.put('rejected')
    except BaseException as error:
        results.put(('worker_error', type(error).__name__, str(error)))
        raise
    finally:
        ready.set()  # Report initialization errors through results instead of hanging.
        if token is not None:
            observability.current.reset(token)
        budgetowner.close()
        if db._conn:
            db._conn.close()
        db._conn = None

class ManagedReleaseGuardTests(unittest.TestCase):
    def test_managed_marker_blocks_all_web_update_targets(self):
        from server.services import updater
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'deploy').mkdir()
            (root / 'deploy' / 'managed-release.json').write_text('{}')
            with mock.patch.object(config, 'ROOT', root), mock.patch.object(updater.subprocess, 'Popen') as spawn:
                for target in ('manager', 'upstream', 'both'):
                    ok, message = updater.start_update(target)
                    self.assertFalse(ok)
                    self.assertIn('受控维护版本', message)
                spawn.assert_not_called()

class DurableBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=config.DB_PATH
        if db._conn: db._conn.close()
        db._conn=None;config.DB_PATH=Path(self.temp.name)/'ledger.db'
        db.connect();budget.initialize()
        db.execute("INSERT INTO api_keys(id,name,key_hash,prefix,quota,quota_credit,created_at) VALUES(1,'fixture','hash','fixture',30000,10,?)",(int(time.time()),))
        self.key=dict(db.query_one('SELECT * FROM api_keys WHERE id=1'))
        self.state=observability.RequestAudit('rid1','/v1/chat/completions','POST','openai',time.monotonic())
        self.token=observability.current.set(self.state)
    def tearDown(self):
        budgetowner.close()
        observability.current.reset(self.token)
        if db._conn: db._conn.close()
        db._conn=None;config.DB_PATH=self.old;self.temp.cleanup()
    def test_reserve_and_unknown_hold(self):
        body={'messages':[{'role':'user','content':'fixture'}]}
        budget.prepare(self.key,body);budget.forwarded();budget.finish(self.state)
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
        self.state.request_id='rid2';self.state.budget_reserved=False
        with self.assertRaises(budget.BudgetRejected):budget.prepare(self.key,body)
    def test_crash_recovery_does_not_release_forwarded(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();budgetowner.close();budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
    def test_unforwarded_crash_releases(self):
        budget.prepare(self.key,{'messages':[]});budgetowner.close();budget.recover()
        self.assertEqual(budget.unresolved(),[])
    def test_complete_settlement(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded()
        self.state.terminal_state='completed'
        with db.transaction():budget.settle({'prompt_tokens':12,'completion_tokens':4},12,4,0.5)
        self.assertEqual(budget.unresolved(),[])
    def test_partial_usage_does_not_unlock_budget(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded()
        self.state.terminal_state='protocol_incomplete'
        budget.settle({'prompt_tokens':12,'completion_tokens':4},12,4,0.5)
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
    def test_manual_resolution_is_idempotent_and_audited(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();budget.finish(self.state)
        self.assertTrue(budget.resolve('rid1',16,0.5,'admin','verified fixture usage'))
        self.assertFalse(budget.resolve('rid1',16,0.5,'admin','retry'))
        key=db.query_one('SELECT used_tokens,used_credit FROM api_keys WHERE id=1')
        self.assertEqual(key['used_tokens'],16);self.assertEqual(key['used_credit'],0.5)
        self.assertEqual(len(db.query('SELECT * FROM audit_logs')),1)
        self.assertEqual(len(db.query('SELECT * FROM adjustment_events')),1)
    def test_startup_leaves_live_owner_untouched(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'forwarded')
    def test_output_clamped_before_forward(self):
        body={'messages':[],'max_tokens':100000}
        budget.prepare(self.key,body)
        self.assertLess(body['max_tokens'],30000)
    def test_estimated_input_rejected(self):
        with self.assertRaises(budget.BudgetRejected):budget.prepare(self.key,{'messages':['x'*30000]})
    def test_transaction_rollback_keeps_hold(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();self.state.terminal_state='completed'
        with self.assertRaises(RuntimeError):
            with db.transaction():
                budget.settle({'prompt_tokens':1,'completion_tokens':1},1,1,0.1)
                raise RuntimeError('fixture commit failure')
        self.assertEqual(budget.unresolved()[0]['state'],'forwarded')
    def test_two_processes_cannot_reserve_the_same_key(self):
        context=multiprocessing.get_context('spawn')
        gate=context.Event();results=context.Queue()
        ready = [context.Event(), context.Event()]
        workers=[context.Process(target=_reserve_from_process,args=(str(config.DB_PATH),f'parallel-{i}',gate,ready[i],results,1)) for i in range(2)]
        started = time.monotonic()
        try:
            for worker in workers:worker.start()
            for signal_ready in ready:
                self.assertTrue(signal_ready.wait(60), 'Worker startup failed before the SQL race')
            startup_s = time.monotonic() - started
            gate.set()
            # The critical operation still has the original eight-second bound.
            outcomes=[results.get(timeout=8),results.get(timeout=8)]
            for worker in workers:
                worker.join(8)
                self.assertFalse(worker.is_alive(), 'reservation worker hung')
                self.assertEqual(worker.exitcode,0, outcomes)
            self.assertCountEqual(outcomes,['rejected','reserved'])
            self.assertEqual(len(budget.unresolved()),1)
            print({'spawn_startup_s':round(startup_s,3),'race_outcomes':outcomes})
        finally:
            gate.set()
            for worker in workers:
                if worker.pid is None:
                    continue
                worker.join(8)
                if worker.is_alive():
                    worker.terminate();worker.join(8)
                if worker.is_alive():
                    worker.kill();worker.join(8)
                self.assertFalse(worker.is_alive(), 'Fixture must reap its own workers')
                worker.close()
            results.close();results.join_thread()


# HTTP transport lifetime/cookie/capacity contracts live in test_actual_http_pool.py.
