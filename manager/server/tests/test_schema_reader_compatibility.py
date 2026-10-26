"""Lossless rollback to the unmodified v7 reader after v8/v9 business writes."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server import config, db, migrations, schemaver


class ReaderCompatibilityContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.original=config.DB_PATH
        with db._lock:db._close_connection_locked()
        config.DB_PATH=Path(self.tmp.name)/'rollback.db'
        self.addCleanup(self.cleanup_db)
        self.v8_steps=tuple(s for s in migrations.MANAGER_STEPS if s.version!=9)
        with patch.object(migrations,'MANAGER_STEPS',self.v8_steps), patch.object(migrations,'CODE_SCHEMA_VERSION',8):db.connect()
        self.conn=db.connect()

    def cleanup_db(self):
        with db._lock:db._close_connection_locked()
        config.DB_PATH=self.original
        self.tmp.cleanup()

    def upgrade(self):
        return migrations.ensure(self.conn,app_version='lossless-test')

    def test_v8_to_v9_permits_exact_v7_reader_without_rewinding_user_version(self):
        with self.assertRaises(schemaver.ReaderTooOld):schemaver.assert_readable(self.conn,7)
        result=self.upgrade()
        self.assertEqual(result.current_version,9)
        self.assertEqual(schemaver.required_reader(self.conn),7)
        schemaver.assert_readable(self.conn,7)
        old_steps=tuple(s for s in self.v8_steps if s.version<=7 or s.version==90)
        schemaver.ensure(self.conn,old_steps)
        schemaver.assert_readable(self.conn,7)
        self.assertEqual(schemaver.user_version(self.conn),9)
        with self.assertRaises(schemaver.ReaderTooOld):schemaver.assert_readable(self.conn,6)

    def test_new_logs_budgets_and_immutable_usage_are_preserved_through_old_reader(self):
        from server import budget
        budget.initialize()
        self.conn.execute("INSERT INTO request_logs(ts,key_id,model,status,request_id,prompt_tokens,completion_tokens) VALUES(1700000000,1,'fake',200,'b-billed',3,4)")
        self.conn.execute("INSERT INTO usage_events(event_id,request_id,attempt,event_type,ts,key_id,model,prompt_tokens,completion_tokens,counted,provenance) VALUES('b-event','b-billed',1,'usage',1700000000,1,'fake',3,4,1,'verified-test')")
        self.conn.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,charged_tokens,updated_at) VALUES('b-billed',1,1700000000,'settled',7,7,1700000000)")
        self.conn.commit()
        tables=('request_logs','usage_events','budget_reservations','api_keys','usage_daily')
        before={t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t)] for t in tables}
        self.upgrade()
        old_steps=tuple(s for s in self.v8_steps if s.version<=7 or s.version==90)
        schemaver.ensure(self.conn,old_steps)
        schemaver.assert_readable(self.conn,7)
        self.assertEqual(before,{t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t)] for t in tables})
        self.upgrade()
        self.assertEqual(before,{t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t)] for t in tables})

    def test_repeated_upgrade_does_not_duplicate_or_change_certificate(self):
        self.upgrade()
        before=[tuple(r) for r in self.conn.execute('SELECT * FROM schema_reader_compatibility')]
        self.assertEqual(len(before),1)
        result=self.upgrade()
        self.assertEqual(result.applied,[])
        self.assertEqual(before,[tuple(r) for r in self.conn.execute('SELECT * FROM schema_reader_compatibility')])
        for sql in ('DELETE FROM schema_reader_compatibility','UPDATE schema_reader_compatibility SET approved_reader=6'):
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):self.conn.execute(sql)

    def test_changed_lease_layout_is_not_certified(self):
        self.conn.execute('ALTER TABLE instance_leases ADD COLUMN unknown_feature TEXT')
        self.conn.commit()
        with self.assertRaises(schemaver.MigrationError):self.upgrade()
        self.assertEqual(schemaver.required_reader(self.conn),8)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_reader_compatibility'").fetchone())

    def test_missing_lease_index_is_not_certified(self):
        self.conn.execute('DROP INDEX idx_instance_leases_host')
        self.conn.commit()
        with self.assertRaises(schemaver.MigrationError):self.upgrade()
        self.assertEqual(schemaver.required_reader(self.conn),8)

    def test_unknown_future_migration_is_not_certified(self):
        self.conn.execute("INSERT INTO schema_migrations(version,name,phase,state,required_reader,checksum,app_version,started_at) VALUES(10,'unknown','expand','applied',10,'unknown','future',1)")
        self.conn.commit()
        with self.assertRaises(schemaver.MigrationError):self.upgrade()
        self.assertEqual(self.conn.execute('SELECT required_reader FROM schema_migrations WHERE version=8').fetchone()[0],8)

    def test_applied_contract_is_not_certified(self):
        self.conn.execute("UPDATE schema_migrations SET state='applied' WHERE version=90")
        self.conn.commit()
        with self.assertRaises(schemaver.MigrationError):self.upgrade()
        self.assertEqual(self.conn.execute('SELECT required_reader FROM schema_migrations WHERE version=8').fetchone()[0],8)

    def test_uncertified_lowered_reader_floor_is_rejected(self):
        self.conn.execute('UPDATE schema_migrations SET required_reader=7 WHERE version=8')
        self.conn.commit()
        with self.assertRaisesRegex(schemaver.MigrationError,'without a certificate'):self.upgrade()

    def test_failed_certificate_write_rolls_back_all_annotations_and_can_retry(self):
        self.conn.execute("CREATE TRIGGER deny_floor BEFORE UPDATE OF required_reader ON schema_migrations WHEN OLD.version=8 BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
        self.conn.commit()
        with self.assertRaises(schemaver.MigrationError):self.upgrade()
        self.assertEqual(self.conn.execute('SELECT required_reader FROM schema_migrations WHERE version=8').fetchone()[0],8)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_reader_compatibility'").fetchone())
        self.conn.execute('DROP TRIGGER deny_floor');self.conn.commit()
        self.upgrade()
        schemaver.assert_readable(self.conn,7)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM schema_reader_compatibility').fetchone()[0],1)

    def test_startup_rejects_a_changed_certified_floor(self):
        self.upgrade()
        self.conn.execute('UPDATE schema_migrations SET required_reader=8 WHERE version=8');self.conn.commit()
        with self.assertRaisesRegex(schemaver.MigrationError,'inconsistent'):self.upgrade()

    def test_new_key_has_stable_zero_credit_projection_across_reopen(self):
        from server import keysvc
        self.upgrade()
        key=keysvc.create_key('new-key-zero-projection')
        before=tuple(self.conn.execute('SELECT used_tokens,used_credit,used_credit_units FROM api_keys WHERE id=?',(key['id'],)).fetchone())
        self.assertEqual(before,(0,0.0,0))
        with db._lock:db._close_connection_locked()
        self.conn=db.connect()
        after=tuple(self.conn.execute('SELECT used_tokens,used_credit,used_credit_units FROM api_keys WHERE id=?',(key['id'],)).fetchone())
        self.assertEqual(before,after)
