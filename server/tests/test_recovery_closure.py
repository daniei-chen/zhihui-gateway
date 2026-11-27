"""Recovery evidence comes from bounded readback and isolated data checks."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest import mock

from server import backup_application, backupstatus, offsitebackup, operational

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('closure_restore_drill', ROOT / 'deploy/restore_drill.py')
drill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drill)


def archive_bytes(payloads, *, manifest=None, extra=()):
    if manifest is None:
        manifest = {'files': {name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                              for name, raw in payloads.items()},
                    'sqlite_snapshots': [{'name': name} for name in payloads if name.endswith('.db')]}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as bundle:
        for name, raw in {'recovery-manifest.json': json.dumps(manifest).encode(), **payloads}.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            bundle.addfile(member, io.BytesIO(raw))
        for member, raw in extra:
            bundle.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def real_database(root):
    path = root / 'original.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO evidence VALUES (1, ?)', ('synthetic only',))
    return path.read_bytes()


def evidence_result(now=None):
    now = int(time.time()) if now is None else now
    source_hash, cipher_hash, commit = 'a' * 64, 'b' * 64, 'c' * 40
    readback = {'method': offsitebackup.READBACK_METHOD, 'verified': True,
                'verified_at': now, 'approved_offsite_target': True, 'commit': commit,
                'source_archive_sha256': source_hash, 'cipher_sha256': cipher_hash,
                'cipher_bytes': 128, 'restore_verified': False}
    return {'restore_check': 'ok', 'bytes': 128, 'sha256': source_hash, 'verified_at': now,
            'offsite': {'verified_remote': True, 'commit': commit,
                       'source_archive_sha256': source_hash, 'cipher_sha256': cipher_hash,
                       'cipher_bytes': 128, 'readback': readback}}


class OfflineRecoveryBoundsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'reports'

    def run_archive(self, raw, *args):
        path = self.root / 'archive.tar.gz'
        path.write_bytes(raw)
        result = drill.main(['--archive', str(path), '--archive-sha256', hashlib.sha256(raw).hexdigest(),
                             '--work-dir', str(self.work), *args])
        report = json.loads((self.work / 'drill-report.json').read_text())
        self.assertEqual(list(self.work.glob('.payload-*')), [])
        return result, report

    def test_real_sqlite_restore_is_isolated_and_does_not_claim_application_recovery(self):
        database = real_database(self.root)
        sentinel = b"raise AssertionError('archived code must never execute')\n"
        raw = archive_bytes({'manager/data/manager.db': database, 'manager/server/main.py': sentinel})
        before = (self.root / 'original.db').read_bytes()
        result, report = self.run_archive(raw)
        self.assertEqual(result, 0)
        self.assertEqual(report['result'], 'data_verified')
        self.assertTrue(report['data_restore_verified'])
        self.assertFalse(report['application_start_verified'])
        self.assertFalse(report['offsite_restore_verified'])
        self.assertFalse(report['complete_restore_verified'])
        self.assertTrue(report['read_only_source'])
        self.assertEqual((self.root / 'original.db').read_bytes(), before)
        self.assertEqual(report['steps'][-1]['sqlite_verified'], 1)

    def test_corrupt_database_cannot_produce_data_evidence(self):
        result, report = self.run_archive(archive_bytes({'manager/data/manager.db': b'not sqlite'}))
        self.assertEqual(result, 5)
        self.assertFalse(report['data_restore_verified'])

    def test_file_inventory_must_cover_every_regular_member(self):
        raw = archive_bytes({'manager/data/manager.db': real_database(self.root)},
                            manifest={'files': {}, 'sqlite_snapshots': [{'name': 'manager/data/manager.db'}]})
        result, report = self.run_archive(raw)
        self.assertEqual(result, 5)
        self.assertFalse(report['data_restore_verified'])

    def test_changed_payload_digest_fails_before_sqlite_open(self):
        database = real_database(self.root)
        manifest = {'files': {'manager/data/manager.db': {'bytes': len(database), 'sha256': '0'*64}},
                    'sqlite_snapshots': [{'name': 'manager/data/manager.db'}]}
        with mock.patch.object(drill.sqlite3, 'connect', side_effect=AssertionError('must not open')):
            result, report = self.run_archive(archive_bytes({'manager/data/manager.db': database}, manifest=manifest))
        self.assertEqual(result, 5)
        self.assertEqual(report['error'], 'ValueError')

    def test_archive_links_duplicate_paths_and_traversal_fail_even_when_sqlite_skipped(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE),
                           ('hardlink', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE),
                           ('recovery-manifest.json', tarfile.REGTYPE)):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/tmp/never-follow'
                result, report = self.run_archive(archive_bytes({}, extra=[(member, b'')]), '--skip-sqlite')
                self.assertEqual(result, 5)
                self.assertFalse(report['data_restore_verified'])
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_expansion_member_and_member_count_budgets(self):
        raw = archive_bytes({'large': b'0' * 16384})
        for limits in ({'MAX_UNPACKED_BYTES': 8192}, {'MAX_MEMBER_BYTES': 8192}, {'MAX_MEMBERS': 1}):
            with self.subTest(limits=limits), mock.patch.multiple(drill, **limits):
                result, _ = self.run_archive(raw, '--skip-sqlite')
                self.assertEqual(result, 5)

    def test_oversized_pax_header_is_rejected_before_large_tarfile_allocation(self):
        member = tarfile.TarInfo('normal')
        member.pax_headers = {'comment': 'x' * (drill.CHUNK + 1)}
        raw = archive_bytes({}, extra=[(member, b'')])
        result, report = self.run_archive(raw, '--skip-sqlite')
        self.assertEqual(result, 5)
        self.assertEqual(report['error'], 'ValueError')

    def test_disk_reserve_stops_restore_without_modifying_source(self):
        raw = archive_bytes({'manager/data/manager.db': real_database(self.root)})
        with mock.patch.object(drill.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            result, report = self.run_archive(raw)
        self.assertEqual(result, 5)
        self.assertEqual(report['error'], 'ValueError')
        self.assertEqual((self.root / 'archive.tar.gz').read_bytes(), raw)

    def test_part_count_and_aggregate_bytes_fail_before_copy(self):
        part = self.root / 'snapshot.part-000.age'
        part.write_bytes(b'ciphertext')
        for meta, limits in (({'parts': [{'name': part.name}]}, {'MAX_CIPHER_BYTES': 2}),
                             ({'parts': [{'name': part.name}] * 2}, {'MAX_CIPHER_PARTS': 1})):
            with self.subTest(limits=limits), mock.patch.multiple(drill, **limits):
                output = self.root / 'joined'
                with self.assertRaises(ValueError):
                    drill._assemble_parts(self.root, meta, output)
                self.assertFalse(output.exists())

    def test_joined_parts_have_constant_memory_use(self):
        part = self.root / 'snapshot.part-000.age'
        with part.open('wb') as stream:
            for _ in range(32):
                stream.write(b'x' * 1024 * 1024)
        tracemalloc.start()
        try:
            with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('must stream')):
                digest, parts = drill._assemble_parts(self.root, {'parts': [{'name': part.name}]}, self.root / 'joined')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertEqual(parts[0]['bytes'], 32 * 1024 * 1024)
        self.assertEqual(digest, drill._hash(part))

    def test_decryption_timeout_kills_child_and_does_not_keep_staging_files(self):
        snapshot = self.root / 'snapshot'
        snapshot.mkdir()
        cipher = b'fixture'
        (snapshot / 'snapshot.part-000.age').write_bytes(cipher)
        (snapshot / 'snapshot.json').write_text(json.dumps({'parts': [{'name': 'snapshot.part-000.age'}],
            'cipher_sha256': hashlib.sha256(cipher).hexdigest(), 'archive_sha256': '0'*64}))
        key = self.root / 'fixture.key'
        key.write_text('synthetic only')
        process = mock.Mock(returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired('age', 1), (None, None)]
        with mock.patch.object(drill.subprocess, 'Popen', return_value=process), mock.patch.object(drill, '_free'):
            result = drill.main([str(snapshot), '--key', str(key), '--work-dir', str(self.work)])
        self.assertEqual(result, 5)
        process.kill.assert_called_once()
        self.assertEqual(list(self.work.glob('.payload-*')), [])
        report = json.loads((self.work / 'drill-report.json').read_text())
        self.assertFalse(report['complete_restore_verified'])

    def test_shell_wrapper_runs_only_explicit_offline_archive(self):
        raw = archive_bytes({'manager/data/manager.db': real_database(self.root)})
        path = self.root / 'archive.tar.gz'
        path.write_bytes(raw)
        result = subprocess.run(['bash', str(ROOT / 'deploy/recovery-drill.sh'), '--archive', str(path),
            '--archive-sha256', hashlib.sha256(raw).hexdigest(), '--work-dir', str(self.work)],
            capture_output=True, text=True, timeout=15, env={**os.environ, 'PYTHON': os.sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.work / 'drill-report.json').read_text())['result'], 'data_verified')


class BackupProofTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'public.json'

    def test_legacy_success_flags_cannot_publish_offsite_health(self):
        for remote in (True, {'verified_remote': True}, {'verified_remote': True, 'readback': {}}):
            with self.subTest(remote=remote):
                value = backupstatus.publish_verified_status({'restore_check': 'ok', 'bytes': 1, 'offsite': remote}, self.path)
                self.assertFalse(value['offsite'])
                self.assertFalse(value['offsite_restore_verified'])

    def test_current_exact_readback_publishes_only_nonsecret_evidence(self):
        result = evidence_result()
        result['offsite']['readback']['private_key'] = 'never-copy-this'
        value = backupstatus.publish_verified_status(result, self.path)
        self.assertTrue(backupstatus.verified_public_readback(value, time.time()))
        self.assertFalse(value['offsite_restore_verified'])
        self.assertNotIn('never-copy-this', self.path.read_text())

    def test_stale_future_different_archive_and_non_offsite_proofs_fail_closed(self):
        for change in ({'verified_at': int(time.time()) - backupstatus.MAX_EVIDENCE_AGE - 1},
                       {'verified_at': int(time.time()) + 600},
                       {'source_archive_sha256': 'f'*64}, {'approved_offsite_target': False},
                       {'commit': 'd'*40}, {'cipher_bytes': True}, {'method': 'ls-remote'}):
            with self.subTest(change=change):
                result = evidence_result()
                result['offsite']['readback'].update(change)
                self.assertFalse(backupstatus.publish_verified_status(result, self.path)['offsite'])

    def test_rewriting_status_mtime_cannot_renew_old_evidence(self):
        now = int(time.time())
        data = backupstatus.publish_verified_status(evidence_result(now), self.path)
        later = now + backupstatus.MAX_EVIDENCE_AGE + 60
        os.utime(self.path, (later, later))
        with mock.patch.object(operational.config, 'BACKUP_STATUS_FILE', self.path), \
             mock.patch.object(operational, '_offsite_configured', return_value=True), \
             mock.patch.object(operational, '_recovery_chain_alert'), mock.patch.object(operational, 'alerts', {}):
            operational.evaluate_backup_health(later)
            self.assertIn('backup_publish', operational.alerts)
            self.assertIn('backup_stale', operational.alerts)
            self.assertEqual(operational.backup_age_seconds(later), later - data['created_at'])

    def test_stale_local_verification_cannot_replace_previous_status(self):
        self.path.write_text('previous status')
        with self.assertRaises(ValueError):
            backupstatus.publish_verified_status(evidence_result(int(time.time()) - 27*3600), self.path)
        self.assertEqual(self.path.read_text(), 'previous status')


class TrustedApplicationRestoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / 'trusted'
        (self.checkout / 'server').mkdir(parents=True)
        (self.checkout / 'server/__init__.py').write_text('')
        self.code = '''
import os, sqlite3
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
@asynccontextmanager
async def lifetime(app):
    Path(os.environ['WB_DATA_DIR'], 'started').write_text('isolated')
    ACTION
    yield
app = FastAPI(lifespan=lifetime)
@app.get('/api/healthz')
def health():
    return {'ok': True}
@app.get('/readyz')
def ready():
    with sqlite3.connect(os.environ['WB_DB']) as connection:
        assert connection.execute('SELECT count(*) FROM evidence').fetchone()[0] == 1
    return {'ok': True}
'''
        self.raw = archive_bytes({'manager/data/manager.db': real_database(self.root)})
        self.archive = self.root / 'archive.tar.gz'
        self.archive.write_bytes(self.raw)
        self.git('init')

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.checkout), '-c', 'user.name=Recovery fixture',
            '-c', 'user.email=fixture@localhost', *args], capture_output=True, check=True, timeout=15)
        return result.stdout.decode().strip()

    def commit(self, action='pass'):
        (self.checkout / 'server/main.py').write_text(self.code.replace('ACTION', action))
        self.git('add', '.')
        self.git('commit', '-m', 'synthetic trusted application')
        return self.git('rev-parse', 'HEAD')

    def run_probe(self, commit):
        work = self.root / 'report'
        result = drill.main(['--archive', str(self.archive), '--archive-sha256', hashlib.sha256(self.raw).hexdigest(),
            '--work-dir', str(work), '--trusted-checkout', str(self.checkout), '--source-commit', commit])
        return result, json.loads((work / 'drill-report.json').read_text())

    def test_trusted_application_lifespan_and_readiness_use_only_restored_copy(self):
        commit = self.commit()
        original = (self.root / 'original.db').read_bytes()
        result, report = self.run_probe(commit)
        self.assertEqual(result, 0, report)
        self.assertTrue(report['data_restore_verified'])
        self.assertTrue(report['application_start_verified'])
        self.assertEqual(report['result'], 'application_verified')
        self.assertEqual(report['steps'][-1]['source_commit'], commit)
        self.assertFalse(report['complete_restore_verified'])
        self.assertFalse(report['offsite_restore_verified'])
        self.assertEqual((self.root / 'original.db').read_bytes(), original)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_external_write_is_blocked_before_any_host_mutation(self):
        protected = self.root / 'outside-state'
        commit = self.commit('Path(' + repr(str(protected)) + ').write_text("forbidden")')
        result, report = self.run_probe(commit)
        self.assertEqual(result, 5)
        self.assertFalse(report['application_start_verified'])
        self.assertFalse(protected.exists())
        self.assertGreater(report['steps'][-1]['blocked_operations']['write'], 0)

    def test_network_access_is_blocked_without_contacting_any_host(self):
        commit = self.commit('__import__("socket").create_connection(("127.0.0.1", 9))')
        result, report = self.run_probe(commit)
        self.assertEqual(result, 5)
        self.assertFalse(report['application_start_verified'])
        self.assertGreater(report['steps'][-1]['blocked_operations']['network'], 0)

    def test_swallowed_external_data_read_cannot_become_healthy_evidence(self):
        protected = self.root / 'outside-credentials'
        protected.write_text('synthetic secret must never be read')
        action = ('try:\n        Path(' + repr(str(protected))
                  + ').read_text()\n    except PermissionError:\n        pass')
        commit = self.commit(action)
        result, report = self.run_probe(commit)
        self.assertEqual(result, 5)
        self.assertFalse(report['application_start_verified'])
        self.assertEqual(report['steps'][-1]['health_status'], 200)
        self.assertGreater(report['steps'][-1]['blocked_operations']['read'], 0)

    def test_wrong_commit_or_hidden_dirty_bytes_fail_closed(self):
        commit = self.commit()
        result, _ = self.run_probe('0' * 40)
        self.assertEqual(result, 5)
        self.git('update-index', '--assume-unchanged', 'server/main.py')
        with (self.checkout / 'server/main.py').open('a') as stream:
            stream.write('\n# hidden dirty input\n')
        self.assertEqual(self.git('status', '--porcelain'), '')
        result, report = self.run_probe(commit)
        self.assertEqual(result, 5)
        self.assertFalse(report['application_start_verified'])


@unittest.skipUnless(shutil.which('git') and shutil.which('openssl'), 'Git and OpenSSL required')
class IndependentReadbackTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private, self.public = self.root/'private.pem', self.root/'public.pem'
        self.run_command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=synthetic-only', '-keyout', str(self.private), '-out', str(self.public)])
        self.remote = self.root / 'receiver.git'
        self.repository = self.root / 'producer.git'
        self.run_command(['git', 'init', '--bare', str(self.remote)])
        self.snapshot = self.root / 'snapshot.tar.gz'
        self.snapshot.write_bytes(archive_bytes({'manager/data/manager.db': real_database(self.root)}))

    def run_command(self, args):
        result = subprocess.run(args, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout

    def test_encrypted_upload_download_decrypt_and_isolated_sqlite_roundtrip(self):
        result = offsitebackup.publish(self.snapshot, self.public, self.repository, str(self.remote))
        self.assertTrue(result['readback']['verified'])
        self.assertEqual(result['readback']['method'], offsitebackup.READBACK_METHOD)
        self.assertFalse(result['verified_remote'], 'local Git receiver is a fixture, never offsite proof')
        self.assertFalse(result['complete_restore_verified'])
        fresh = self.root / 'independent.git'
        self.run_command(['git', 'init', '--bare', str(fresh)])
        self.run_command(['git', '--git-dir', str(fresh), 'fetch', '--depth=1', str(self.remote), result['branch']])
        cipher = self.root / 'download.cms'
        cipher.write_bytes(self.run_command(['git', '--git-dir', str(fresh), 'show', result['commit'] + ':snapshot.cms']))
        restored = self.root / 'download.tar.gz'
        self.run_command(['openssl', 'cms', '-decrypt', '-binary', '-inform', 'DER', '-in', str(cipher),
            '-inkey', str(self.private), '-recip', str(self.public), '-out', str(restored)])
        work = self.root / 'reports'
        self.assertEqual(drill.main(['--archive', str(restored), '--archive-sha256', result['source_archive_sha256'],
                                     '--work-dir', str(work)]), 0)
        report = json.loads((work/'drill-report.json').read_text())
        self.assertTrue(report['data_restore_verified'])
        self.assertFalse(report['offsite_restore_verified'])

    def test_push_and_matching_ref_without_independent_download_are_not_success(self):
        original = offsitebackup._run

        def fail_download(command, **kwargs):
            if 'fetch' in command and any('readback-' in str(part) for part in command):
                raise RuntimeError('synthetic download unavailable')
            return original(command, **kwargs)
        with mock.patch.object(offsitebackup, '_run', side_effect=fail_download):
            with self.assertRaisesRegex(RuntimeError, 'download unavailable'):
                offsitebackup.publish(self.snapshot, self.public, self.repository, str(self.remote))
        # The remote push happened, but the producer did not bless its local ref.
        self.assertTrue(self.run_command(['git', '--git-dir', str(self.remote), 'show-ref', '--heads']))
        refs = self.run_command(['git', '--git-dir', str(self.repository), 'for-each-ref', '--format=%(refname)'])
        self.assertNotIn(('refs/heads/' + offsitebackup.BRANCH).encode(), refs)


class BackupProducerBoundsTest(unittest.TestCase):
    def test_large_release_inventory_does_not_retain_file_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'deploy').mkdir()
            source = root / 'payload.bin'
            with source.open('wb') as stream:
                for _ in range(20):
                    stream.write(b'x' * 1024 * 1024)
            source.chmod(0o600)
            manifest = {'files': {'payload.bin': {'mode': 0o600, 'candidate_sha256': drill._hash(source)}}}
            (root/'deploy/managed-release.json').write_text(json.dumps(manifest))
            tracemalloc.start()
            try:
                records = backup_application.managed_sources(root)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 8 * 1024 * 1024)
            output = root / 'output.tar'
            with tarfile.open(output, mode='w') as bundle:
                for relative, payload, mode in records:
                    backup_application.add_release_bytes(bundle, relative, payload, mode)
            with tarfile.open(output) as bundle:
                self.assertEqual(bundle.getmember('payload.bin').size, 20 * 1024 * 1024)

    def test_producer_receipt_is_consumed_by_the_offline_restore_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = real_database(root)
            source = b'# synthetic trusted release file\n'
            manifest = {'files': {'server/main.py': {'mode': 0o600,
                'candidate_sha256': hashlib.sha256(source).hexdigest()}}}
            archive = root / 'application.tar.gz'
            with backup_application.RecoveryArchive.open(archive, 'w:gz') as bundle:
                backup_application.add_bytes(bundle, 'manager/data/manager.db', raw)
                backup_application.add_bytes(bundle, 'manager/server/main.py', source)
                backup_application.add_bytes(bundle, 'manager/deploy/managed-release.json', json.dumps(manifest).encode())
                bundle.add_recovery_manifest()
            with tarfile.open(archive) as bundle, mock.patch.object(backup_application, 'MANAGER', root):
                self.assertEqual(backup_application.verify_release_archive(bundle)['check'], 'ok')
            self.assertEqual(drill.main(['--archive', str(archive), '--archive-sha256', drill._hash(archive),
                                         '--work-dir', str(root / 'report')]), 0)
            report = json.loads((root/'report/drill-report.json').read_text())
            self.assertTrue(report['data_restore_verified'])
            self.assertFalse(report['complete_restore_verified'])

    def test_archive_write_budget_rejects_before_reading_payload(self):
        output = io.BytesIO()
        with mock.patch.object(backup_application, 'MAX_DATABASE_BYTES', 1024):
            with backup_application.RecoveryArchive.open(fileobj=output, mode='w') as archive:
                member = tarfile.TarInfo('large.db')
                member.size = 2048
                with self.assertRaisesRegex(RuntimeError, 'write budget'):
                    archive.addfile(member, mock.Mock(read=mock.Mock(side_effect=AssertionError('must not read'))))

    def test_oversized_json_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'metadata.json'
            source.write_text(' ' * 1024)
            with mock.patch.object(backup_application, 'MAX_METADATA_BYTES', 100):
                # The default argument is fixed; pass the explicit small limit to the primitive.
                with self.assertRaises(RuntimeError):
                    backup_application.bounded_bytes(source, 100)


if __name__ == '__main__':
    unittest.main()
