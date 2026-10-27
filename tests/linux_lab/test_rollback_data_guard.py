"""Snapshot rollback must preserve post-switch business writes."""
import importlib.util
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('shadow_guard_test', HERE/'r4_parallel_shadow.py')
r4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r4)


class RollbackDataContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db, self.snap = self.root/'main.db', self.root/'before.db'
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE request_logs(id INTEGER PRIMARY KEY, tokens INTEGER, secret BLOB)')
            c.execute("INSERT INTO request_logs VALUES(1,5,X'00FF')")
        backup = r4.backup_main_db(self.db, self.snap)
        self.assertTrue(backup['ok'])
        self.sha = backup['sha256']

    def restore(self):
        return r4.restore_main_db(self.db, self.snap, expected_sha256=self.sha)

    def test_post_switch_insert_refuses_overwrite_and_preserves_rows(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('INSERT INTO request_logs VALUES(2,9,NULL)')
        result = self.restore()
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'business_data_changed_since_snapshot')
        with closing(sqlite3.connect(self.db)) as c, c:
            self.assertEqual(c.execute('SELECT count(*) FROM request_logs').fetchone()[0], 2)

    def test_update_same_row_count_refuses_overwrite(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE request_logs SET tokens=100 WHERE id=1')
        self.assertFalse(self.restore()['ok'])
        with closing(sqlite3.connect(self.db)) as c, c:
            self.assertEqual(c.execute('SELECT tokens FROM request_logs').fetchone()[0],100)

    def test_new_nonempty_business_table_refuses_overwrite(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE new_billing(amount INTEGER)')
            c.execute('INSERT INTO new_billing VALUES(7)')
        self.assertFalse(self.restore()['ok'])

    def test_lease_and_migration_metadata_only_can_restore(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE instance_leases(owner TEXT)')
            c.execute("INSERT INTO instance_leases VALUES('local-test-owner')")
            c.execute('CREATE TABLE schema_migrations(version INTEGER)')
            c.execute('INSERT INTO schema_migrations VALUES(8)')
        self.assertTrue(self.restore()['ok'])
        with closing(sqlite3.connect(self.db)) as c, c:
            self.assertEqual(c.execute('SELECT tokens FROM request_logs').fetchone()[0], 5)

    def test_failed_db_restore_does_not_start_a_or_reload_entry(self):
        view=SimpleNamespace(root=self.root)
        entry=SimpleNamespace(conf=self.root/'nginx.conf',wait_front=lambda _: {'front_200': True})
        entry.conf.write_bytes(b'B-authority')
        with patch.object(r4,'restore_main_db',return_value={'ok':False,'error':'business_data_changed_since_snapshot'}), patch.object(r4,'start_instance') as start, patch.object(r4.switch_upstream,'reload_nginx') as reload:
            report=r4.restore_a_service(MockExperiment(),view,entry,old_port=33333,original_conf=b'A-authority',timeout=.01,rel_ms=lambda:0,db_snapshot=str(self.snap),db_path=str(self.db),db_recovery_mode='snapshot_if_unchanged',writer_processes=[])
        self.assertFalse(report['pass'])
        self.assertTrue(report['manual_intervention_required'])
        start.assert_not_called()
        reload.assert_not_called()
        self.assertEqual(entry.conf.read_bytes(),b'B-authority')


class MockExperiment:
    pass


class RollbackControllerContracts(unittest.TestCase):
    def test_preserving_recovery_never_calls_snapshot_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'instance-A2-recover.json').write_text('{}')
            proc=Mock();proc.poll.return_value=0
            instance=SimpleNamespace(proc=proc)
            entry=SimpleNamespace(conf=root/'nginx.conf',state={},wait_front=lambda _: {'front_200':True})
            entry.conf.write_bytes(b'B')
            started={'instance':instance,'ready':{'ready':True},'login_status':200}
            with patch.object(r4,'probe_old_reader',return_value={'readable':True}), patch.object(r4,'restore_main_db') as restore, patch.object(r4,'start_instance',return_value=started), patch.object(r4,'wait_port_free',return_value={'free':True,'port':33333}), patch.object(r4,'port_closed',return_value=True), patch.object(r4.switch_upstream,'reload_nginx'):
                result=r4.restore_a_service(MockExperiment(),SimpleNamespace(root=root,source=root),entry,old_port=33333,original_conf=b'A',timeout=.01,rel_ms=lambda:0,db_snapshot='old-snapshot',db_path='current-db',writer_processes=[])
            self.assertTrue(result['pass'])
            self.assertFalse(result['db_restored'])
            self.assertEqual(result['db_recovery_mode'],'preserve')
            restore.assert_not_called()

    def test_running_writer_prevents_reader_start_and_entry_change(self):
        proc=Mock();proc.poll.return_value=None
        writer=SimpleNamespace(tag='B',proc=proc)
        with patch.object(r4,'start_instance') as start, patch.object(r4,'probe_old_reader') as probe:
            result=r4.restore_a_service(MockExperiment(),None,None,old_port=33333,original_conf=None,timeout=.01,rel_ms=lambda:0,db_path='current-db',writer_processes=[writer])
        self.assertFalse(result['pass'])
        self.assertIn('writer_still_running',result['errors'])
        start.assert_not_called();probe.assert_not_called()

    def test_readyz_503_is_not_overridden_by_health_200(self):
        response=Mock();response.status_code=503
        client=Mock();client.get.side_effect=lambda path:SimpleNamespace(status_code=503 if path=='/api/readyz' else 200)
        context=Mock();context.__enter__=Mock(return_value=client);context.__exit__=Mock(return_value=False)
        instance=SimpleNamespace(url='http://127.0.0.1:1',proc=None)
        with patch('httpx.Client',return_value=context):
            result=r4.wait_ready(instance,.001)
        self.assertFalse(result['ready'])

    def test_live_a_is_resumed_before_any_entry_reload(self):
        proc=Mock();proc.poll.return_value=None
        live=SimpleNamespace(proc=proc)
        experiment=SimpleNamespace(drain_action=Mock(return_value={'status':200}))
        with patch.object(r4,'wait_ready',return_value={'ready':True}), patch.object(r4,'start_instance') as start:
            result=r4.restore_a_service(experiment,None,None,old_port=33333,original_conf=None,timeout=.01,rel_ms=lambda:0,live_instance=live,writer_processes=[])
        self.assertTrue(result['pass']);self.assertEqual(result['branch'],'resume_live_a')
        experiment.drain_action.assert_called_once_with(live,'resume');start.assert_not_called()
