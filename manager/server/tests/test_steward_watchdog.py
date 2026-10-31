"""Synthetic host-watchdog contracts; never contact SMTP, systemd or a live DB."""
from contextlib import closing, contextmanager
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

from deploy import steward_watchdog as watchdog
from server import steward_config


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='zh-steward-watchdog-'))
        self.root = Path(directory).resolve()
        self.paths = watchdog.Paths(self.root / 'fixture.db', self.root / 'state',
                                    self.root / 'smtp.json', self.root / 'release.lock')
        self.selected = steward_config.validate_policy({'enabled': True, 'recipients': ['fixture@example.com']})
        self.ready, self.held = False, False
        self.control = Mock(side_effect=lambda _: {'policy': self.selected.model_dump(), 'heartbeat': {'at': self.now}})
        self.sender = Mock(return_value={'status': 'accepted', 'code': 'smtp_accepted_not_inbox_proof'})
        self.enterContext(patch.object(steward_config, 'secret_file', return_value=json.dumps({
            'host': 'smtp.qq.com', 'port': 465, 'username': 'sender@example.com', 'password': 'synthetic'})))
        if os.name != 'posix':
            # Only storage/lock primitives are simulated here. The POSIX-only
            # native guard has a separate explicit rejection test below.
            @contextmanager
            def owned(paths):
                paths.state_dir.mkdir(exist_ok=True)
                yield
            self.enterContext(patch.object(watchdog, 'owned', owned))
            self.enterContext(patch.object(watchdog, 'atomic', side_effect=lambda path, value:
                path.write_text(json.dumps(value), encoding='utf-8')))
            self.enterContext(patch.object(watchdog, 'safe_json', side_effect=lambda path:
                json.loads(path.read_text(encoding='utf-8'))))

    def run_at(self, now):
        self.now = now
        return watchdog.run_once(self.paths, probe=lambda: self.ready, control=self.control,
            maintenance=lambda _: self.held, sender=self.sender, now=now)

    def state(self):
        return json.loads((self.paths.state_dir / 'state.json').read_text(encoding='utf-8'))

    def save(self, value):
        watchdog.atomic(self.paths.state_dir / 'state.json', value)

    def test_debounce_duplicate_recovery_and_recurrence(self):
        self.assertEqual(self.run_at(100)['processed'], 0)
        self.assertEqual(self.run_at(160)['processed'], 1)
        self.run_at(220)
        self.assertEqual(self.sender.call_count, 1)
        self.ready = True
        self.run_at(280)
        self.run_at(340)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.state()['outbox'][-1]['payload']['event'], 'recovered')
        self.ready = False
        self.run_at(400)
        self.run_at(460)
        self.assertEqual(self.state()['rules']['manager_readiness']['generation'], 2)
        self.assertEqual(self.sender.call_count, 3)

    def test_enable_during_open_fault_and_new_recipient_are_not_lost(self):
        self.selected = steward_config.Policy()
        self.run_at(100)
        self.run_at(160)
        self.assertEqual(self.sender.call_count, 0)
        self.selected = steward_config.validate_policy({'enabled': True, 'recipients': ['fixture@example.com']})
        self.run_at(220)
        self.assertEqual(self.sender.call_count, 1)
        self.selected.recipients.append('other@example.com')
        self.run_at(280)
        self.assertEqual(self.sender.call_count, 2)
        self.run_at(340)
        self.assertEqual(self.sender.call_count, 2)

    def test_publication_mutes_dispatch_and_does_not_reset_timeout(self):
        self.run_at(100)
        self.held = True
        for now in (160, 220, 1000):
            self.assertTrue(self.run_at(now)['maintenance'])
        self.assertEqual(self.sender.call_count, 0)
        self.assertFalse(self.run_at(1060)['maintenance'])
        self.run_at(1120)
        self.assertEqual(self.sender.call_count, 1)
        self.held = False
        self.run_at(1180)
        self.assertIsNone(self.state()['maintenance_since'])

    def test_transport_exception_is_unknown_and_never_retried(self):
        self.sender.side_effect = RuntimeError('private-transport-body')
        self.run_at(100)
        self.run_at(160)
        for now in (220, 600, 1000):
            self.run_at(now)
        state = self.state()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(state['outbox'][0]['status'], 'unknown')
        self.assertNotIn('private-transport-body', json.dumps(state))

    def test_pre_send_configuration_failure_has_exact_three_attempts(self):
        with patch.object(steward_config, 'secret_file', side_effect=OSError('fixture unavailable')):
            self.run_at(100)
            self.run_at(160)
            self.run_at(220)
            self.run_at(520)
            self.run_at(900)
        self.assertEqual(self.sender.call_count, 0)
        row = self.state()['outbox'][0]
        self.assertEqual((row['status'], row['attempts']), ('failed', 3))

    def test_crash_after_acceptance_preserves_sending_then_unknown(self):
        original = watchdog.atomic
        failed = []
        def interrupted(path, value):
            if path.name == 'state.json' and any(item['status'] == 'accepted' for item in value['outbox']) and not failed:
                failed.append(True)
                raise OSError('simulated finish write failure')
            return original(path, value)
        self.run_at(100)
        with patch.object(watchdog, 'atomic', side_effect=interrupted), self.assertRaises(OSError):
            self.run_at(160)
        self.assertEqual(self.state()['outbox'][0]['status'], 'sending')
        self.run_at(220)
        self.assertEqual(self.state()['outbox'][0]['status'], 'unknown')
        self.assertEqual(self.sender.call_count, 1)

    def test_recovery_cancels_old_pending_fault_even_when_recovery_mail_disabled(self):
        self.selected.send_recovery = False
        self.sender.return_value = {'status': 'retry', 'code': 'smtp_connection_failed'}
        self.run_at(100)
        self.run_at(160)
        self.ready = True
        self.run_at(180)
        self.run_at(200)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.state()['outbox'][0]['status'], 'cancelled')

    def test_public_policy_cache_expires_and_current_disable_wins(self):
        self.run_at(100)
        self.control.side_effect = sqlite3.OperationalError('locked')
        self.assertFalse(self.run_at(160)['control_current'])
        self.assertEqual(self.sender.call_count, 1)
        self.sender.return_value = {'status': 'retry', 'code': 'smtp_connection_failed'}
        self.control.side_effect = lambda _: {'policy': {**self.selected.model_dump(), 'recipients': ['other@example.com']}, 'heartbeat': None}
        self.run_at(220)
        self.control.side_effect = sqlite3.OperationalError('offline')
        self.run_at(220 + watchdog.PUBLIC_POLICY_MAX_AGE + 1)
        self.assertEqual(self.state()['outbox'][-1]['status'], 'cancelled')
        self.control.side_effect = lambda _: {'policy': {'enabled': False}, 'heartbeat': None}
        self.run_at(220 + watchdog.PUBLIC_POLICY_MAX_AGE + 2)
        self.assertFalse(self.state()['policy']['enabled'])

    def test_corrupt_current_policy_is_not_replaced_by_stale_cached_recipients(self):
        self.run_at(100)
        self.control.side_effect = lambda _: {'policy': {'enabled': 'yes'}, 'heartbeat': None}
        before = (self.paths.state_dir / 'state.json').read_bytes()
        with self.assertRaises(ValueError):
            self.run_at(160)
        self.assertEqual((self.paths.state_dir / 'state.json').read_bytes(), before)
        self.assertEqual(self.sender.call_count, 0)

    def test_corrupt_delivery_ids_recipient_and_status_fail_closed(self):
        self.run_at(100)
        self.run_at(160)
        original = self.state()
        variants = []
        for key, value in (('id', True), ('id', 10000), ('recipient', 'bad\r\nBcc:bad@example.com'),
                           ('status', 'retry'), ('attempts', -1), ('code', 'credential body')):
            changed = copy.deepcopy(original)
            changed['outbox'][0][key] = value
            variants.append(changed)
        duplicate = copy.deepcopy(original)
        duplicate['outbox'].append(copy.deepcopy(duplicate['outbox'][0]))
        variants.append(duplicate)
        for changed in variants:
            with self.subTest(changed=changed['outbox'][0]), self.assertRaises(ValueError):
                watchdog.validate_state(changed, 220)

    def test_read_control_does_not_load_snapshot_or_change_schema(self):
        with closing(sqlite3.connect(self.paths.database)) as conn, conn:
            conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            conn.executemany('INSERT INTO settings VALUES(?,?)', [
                ('steward.policy.v1', '{}'), ('steward.heartbeat.v1', '{"at":123}'),
                ('steward.snapshot.v1', 'private snapshot must not be parsed')])
        result = watchdog.read_control(self.paths)
        self.assertEqual(result, {'policy': {}, 'heartbeat': {'at': 123}})

    def test_collector_liveness_respects_selected_interval_and_unknowns(self):
        self.ready = True
        self.selected.interval_seconds = 300
        self.control.side_effect = lambda _: {'policy': self.selected.model_dump(), 'heartbeat': {'at': 100}}
        self.run_at(400)
        self.assertFalse(self.state()['rules']['collector_liveness']['active'])
        self.run_at(480)
        self.run_at(540)
        self.assertTrue(self.state()['rules']['collector_liveness']['active'])
        self.assertEqual(self.sender.call_args.args[2]['rule'], 'collector_liveness')


class NativeWatchdogBoundaryTests(unittest.TestCase):
    def test_publication_probe_uses_only_kernel_metadata_not_the_root_only_file(self):
        from types import SimpleNamespace
        info = SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1, st_dev=0, st_ino=123)
        paths = watchdog.Paths(publication_lock=Path('/run/fixture.lock'))
        for content, expected in ((b'8: FLOCK ADVISORY WRITE 456 00:00:123 0 EOF\n', True),
                                  (b'8: FLOCK ADVISORY READ 456 00:00:123 0 EOF\n', False),
                                  (b'8: FLOCK ADVISORY WRITE 456 00:00:999 0 EOF\n', False)):
            with self.subTest(expected=expected), patch.object(watchdog, 'fcntl', object()), \
                    patch.object(Path, 'lstat', return_value=info), \
                    patch.object(Path, 'open', mock_open(read_data=content)) as opened, \
                    patch.object(os, 'open', side_effect=AssertionError('must not open privileged lock')), \
                    patch.object(os, 'major', return_value=0, create=True), \
                    patch.object(os, 'minor', return_value=0, create=True):
                self.assertEqual(watchdog.publishing(paths), expected)
                opened.assert_called_once_with('rb')

    def test_native_worker_lock_is_exclusive_or_explicitly_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = watchdog.Paths(state_dir=Path(directory).resolve() / 'state')
            if os.name != 'posix':
                with self.assertRaisesRegex(RuntimeError, 'requires_posix'):
                    with watchdog.owned(paths):
                        self.fail('Windows must not pretend a Linux lifetime lock exists')
            else:
                with watchdog.owned(paths):
                    with self.assertRaises(BlockingIOError):
                        with watchdog.owned(paths):
                            self.fail('second worker acquired the same lease')
                self.assertEqual((paths.state_dir / 'worker.lock').stat().st_mode & 0o777, 0o600)

    def test_unknown_command_line_cannot_be_used_as_old_onfailure_interface(self):
        with patch('sys.argv', ['steward_watchdog.py', '--onfailure', 'private.service']), self.assertRaises(SystemExit) as caught:
            watchdog.main()
        self.assertEqual(caught.exception.code, 2)
