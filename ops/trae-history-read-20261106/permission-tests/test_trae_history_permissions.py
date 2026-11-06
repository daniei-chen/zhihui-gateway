"""Actual Linux UID/group/SQLite fixture; no production paths or data."""
import hashlib,importlib.util,os,sqlite3,subprocess,tempfile,unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('history_permissions',Path(__file__).parents[1]/'repair_trae_history_read.py')
repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)


class HistoryReadPermissions(unittest.TestCase):
    def setUp(self):
        if os.geteuid()!=0:raise RuntimeError('real root-only isolated fixture required')
        self.scratch=tempfile.TemporaryDirectory(prefix='zhihui-history-perms-',dir='/var/lib')
        self.root=Path(self.scratch.name);self.root.chmod(0o711)
        self.base=self.root/'data';self.base.mkdir(mode=0o700)
        self.inner=self.base/'data';self.inner.mkdir(mode=0o700)
        self.db=self.inner/'aiwork.sqlite';self.conn=sqlite3.connect(self.db)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('CREATE TABLE api_usage(bucket,day,data); CREATE TABLE credits_daily(date,total,earned,consumed); CREATE TABLE checkin_results(day,uid,name,status,updated_at);')
        self.conn.commit()
        for p in self.inner.iterdir():p.chmod(0o600)

    def tearDown(self):
        self.conn.close();self.scratch.cleanup()

    def test_minimum_group_role_enables_read_and_exact_metadata_rollback(self):
        self.assertNotEqual(repair.check_read(self.db).returncode,0)
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        rows=repair.targets(self.base);repair.apply(rows)
        self.assertEqual(repair.check_read(self.db).stdout.strip(),b'LIVE_HISTORY_READ_OK')
        self.assertEqual(self.base.stat().st_mode&0o7777,0o710)
        self.assertEqual(self.inner.stat().st_mode&0o7777,0o2710)
        self.assertEqual(self.db.stat().st_mode&0o7777,0o640)
        self.assertEqual(self.db.stat().st_uid,0)
        self.assertEqual(self.db.stat().st_gid,10001)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),before)
        denied=subprocess.run(['/usr/bin/setpriv','--reuid=10001','--regid=10001','--clear-groups',
            '/usr/bin/python3','-I','-S','-B','-c',"import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=rw',uri=True); c.execute('CREATE TABLE forbidden_write(value)'); c.commit()",str(self.db)],capture_output=True)
        self.assertNotEqual(denied.returncode,0)
        self.assertIn(b'readonly',denied.stderr)
        repair.restore(rows)
        self.assertNotEqual(repair.check_read(self.db).returncode,0)
        self.assertEqual(self.db.stat().st_mode&0o7777,0o600)
        self.assertEqual(self.db.stat().st_gid,0)

    def test_recreated_sqlite_sidecars_inherit_the_read_group(self):
        rows=repair.targets(self.base);repair.apply(rows);self.conn.close()
        self.conn=sqlite3.connect(self.db)
        self.conn.execute("INSERT INTO api_usage VALUES('synthetic','2026-11-06','{}')");self.conn.commit()
        for name in ('aiwork.sqlite-wal','aiwork.sqlite-shm'):
            p=self.inner/name
            self.assertEqual(p.stat().st_gid,10001)
            self.assertEqual(p.stat().st_mode&0o7777,0o640)
        self.assertEqual(repair.check_read(self.db).stdout.strip(),b'LIVE_HISTORY_READ_OK')

    def test_unrelated_files_refuse_any_permission_plan(self):
        (self.inner/'unrelated-secret').write_bytes(b'synthetic')
        with self.assertRaises(ValueError):repair.targets(self.base)

    def test_unrelated_private_file_is_not_shared_or_modified(self):
        private=self.inner/'unrelated-secret';private.write_bytes(b'synthetic');private.chmod(0o600)
        rows=repair.targets(self.base);repair.apply(rows)
        self.assertEqual(private.stat().st_mode&0o7777,0o600)
        self.assertEqual(private.stat().st_gid,0)
        read=subprocess.run(['/usr/bin/setpriv','--reuid=10001','--regid=10001','--clear-groups',
            '/usr/bin/python3','-I','-S','-B','-c',"from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()",str(private)],capture_output=True)
        self.assertNotEqual(read.returncode,0)
        self.assertIn(b'PermissionError',read.stderr)

    def test_unrelated_outer_readable_entry_refuses_traversal_grant(self):
        (self.base/'unrelated-secret').write_bytes(b'synthetic')
        with self.assertRaises(ValueError):repair.targets(self.base)

    def test_changed_sidecar_does_not_prevent_closing_the_read_role(self):
        rows=repair.targets(self.base);repair.apply(rows)
        sidecar=self.inner/'aiwork.sqlite-wal';old=sidecar.with_name('retained-synthetic-wal')
        sidecar.rename(old);sidecar.write_bytes(b'synthetic replacement');sidecar.chmod(0o640)
        with self.assertRaisesRegex(ValueError,'rollback incomplete'):repair.restore(rows)
        self.assertEqual(self.base.stat().st_mode&0o7777,0o700)
        self.assertEqual(self.inner.stat().st_mode&0o7777,0o700)
        self.assertEqual(self.db.stat().st_mode&0o7777,0o600)
        self.assertNotEqual(repair.check_read(self.db).returncode,0)


if __name__=='__main__':unittest.main(verbosity=2)
