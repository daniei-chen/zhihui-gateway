"""Private subprocess fixtures only; no network or real SMTP credentials."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from server.services import smtp_transport


class SMTPWorkerTests(unittest.TestCase):
    settings = {'host': 'smtp.qq.com', 'port': 465,
                'username': 'sender@example.com', 'password': 'synthetic-private-fixture'}

    def test_fixed_worker_private_input_and_no_inherited_credentials(self):
        output = {'status': 'accepted', 'code': 'smtp_accepted_not_inbox_proof'}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'not-inherited', 'HTTP_PROXY': 'not-inherited'}), \
                patch.object(smtp_transport.subprocess, 'run', return_value=Mock(
                    returncode=0, stdout=json.dumps(output).encode())) as run:
            self.assertEqual(smtp_transport.send(self.settings, 'to@example.com', b'fixture'), output)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], '--smtp-worker')
        self.assertIn('-I', args[0])
        self.assertNotIn(self.settings['password'], ' '.join(args[0]))
        self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
        self.assertNotIn('HTTP_PROXY', kwargs['env'])
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(kwargs['timeout'], 45)
        value = json.loads(kwargs['input'])
        self.assertEqual(value['settings'], self.settings)

    def test_deadline_or_invalid_worker_receipt_never_claims_safe_retry(self):
        for response in (Mock(returncode=1, stdout=b''), Mock(returncode=0, stdout=b'invalid'),
                         Mock(returncode=0, stdout=b'{"status":"accepted","code":"invented"}'),
                         Mock(returncode=0, stdout=b'{"status":[],"code":"invented"}'),
                         Mock(returncode=0, stdout=b'x' * 513)):
            with self.subTest(response=response), patch.object(smtp_transport.subprocess, 'run', return_value=response):
                self.assertEqual(smtp_transport.send(self.settings, 'to@example.com', b'fixture'),
                                 {'status': 'unknown', 'code': 'smtp_worker_outcome_unknown'})
        with patch.object(smtp_transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 45)):
            self.assertEqual(smtp_transport.send(self.settings, 'to@example.com', b'fixture'),
                             {'status': 'unknown', 'code': 'smtp_total_deadline_unknown'})

    def test_worker_spawn_failure_is_pre_effect_retryable(self):
        with patch.object(smtp_transport.subprocess, 'run', side_effect=OSError('private environment')):
            self.assertEqual(smtp_transport.send(self.settings, 'to@example.com', b'fixture'),
                             {'status': 'retry', 'code': 'smtp_worker_unavailable'})

    def test_existing_163_sender_uses_verified_tls_without_changing_recipient(self):
        settings = {**self.settings, 'host': 'smtp.163.com'}
        transport = Mock()
        transport.mail.return_value = transport.rcpt.return_value = transport.data.return_value = (250, b'fixture')
        factory = Mock(return_value=transport)
        result = smtp_transport.send(settings, 'recipient@qq.com', b'fixture', smtp_factory=factory)
        self.assertEqual(result['status'], 'accepted')
        args, kwargs = factory.call_args
        self.assertEqual(args, ('smtp.163.com', 465))
        self.assertTrue(kwargs['context'].check_hostname)
        self.assertEqual(kwargs['context'].verify_mode, smtp_transport.ssl.CERT_REQUIRED)
        transport.rcpt.assert_called_once_with('recipient@qq.com')
        transport.starttls.assert_not_called()

    def test_endpoint_allowlist_rejects_unreviewed_hosts_ports_and_bad_types(self):
        for host, port in (('smtp.163.com', 587), ('SMTP.163.COM', 465),
                           ('smtp.qq.com.evil.invalid', 465), ('127.0.0.1', 465),
                           ('smtp.qq.com', True), ([], 465), ('smtp.qq.com', '465')):
            with self.subTest(host=host, port=port), patch.object(smtp_transport.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    smtp_transport.send({**self.settings, 'host': host, 'port': port}, 'to@example.com', b'fixture')
                run.assert_not_called()

    def test_validation_before_spawn_and_finite_message(self):
        for settings, recipient, raw in (
                ({**self.settings, 'host': 'evil.invalid'}, 'to@example.com', b'x'),
                ({**self.settings, 'port': True}, 'to@example.com', b'x'),
                (self.settings, 'to@example.com\r\nBcc:private@invalid', b'x'),
                (self.settings, 'to@example.com', b'x' * (smtp_transport.MAX_MESSAGE_BYTES + 1))):
            with self.subTest(recipient=recipient), patch.object(smtp_transport.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    smtp_transport.send(settings, recipient, raw)
                run.assert_not_called()

    def test_real_deadline_kills_and_reaps_owned_child(self):
        real_run, real_popen = subprocess.run, subprocess.Popen
        processes = []
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'synthetic_worker.py'
            fixture.write_text('import time\ntime.sleep(30)\n', encoding='utf-8')

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            def finite_run(args, **kwargs):
                return real_run([*args[:5], str(fixture), *args[6:]], **kwargs)

            started = time.monotonic()
            with patch.object(smtp_transport, 'TOTAL_TIMEOUT_SECONDS', .3), \
                    patch.object(smtp_transport.subprocess, 'run', side_effect=finite_run), \
                    patch.object(smtp_transport.subprocess, 'Popen', side_effect=launch):
                value = smtp_transport.send(self.settings, 'to@example.com', b'fixture')
            self.assertEqual(value, {'status': 'unknown', 'code': 'smtp_total_deadline_unknown'})
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertNotEqual(processes[0].returncode, 0)

    def test_real_private_worker_refuses_invalid_endpoint_without_network(self):
        # Exercise the actual isolated CLI, but invalid input stops before DNS.
        value = {'settings': {**self.settings, 'host': 'synthetic.invalid'},
                 'recipient': 'to@example.com', 'message': 'Zml4dHVyZQ=='}
        result = subprocess.run([os.sys.executable, '-I', '-B', '-X', 'utf8',
                                 str(Path(smtp_transport.__file__)), '--smtp-worker'],
                                input=json.dumps(value).encode(), capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(self.settings['password'].encode(), result.stderr)

    def test_actual_worker_serializes_synthetic_smtp_without_importing_application(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture_worker.py'
            real = str(Path(smtp_transport.__file__).resolve())
            fixture.write_text('''import runpy, smtplib
class FixtureSMTP:
    def __init__(self, host, port, **kwargs):
        assert host == 'smtp.qq.com' and port == 465
    def ehlo(self): return 250, b'fixture'
    def login(self, username, password):
        assert username == 'sender@example.com' and password == 'synthetic-private-fixture'
    def mail(self, sender): return 250, b'fixture'
    def rcpt(self, recipient): return 250, b'fixture'
    def data(self, raw):
        assert raw == b'synthetic finite mail'
        return 250, b'fixture'
    def close(self): pass
smtplib.SMTP_SSL = FixtureSMTP
runpy.run_path(''' + repr(real) + ''', run_name='__main__')
''', encoding='utf-8')
            value = {'settings': self.settings, 'recipient': 'to@example.com',
                     'message': 'c3ludGhldGljIGZpbml0ZSBtYWls'}
            result = subprocess.run([os.sys.executable, '-I', '-B', '-X', 'utf8',
                                     str(fixture), '--smtp-worker'], input=json.dumps(value).encode(),
                                    capture_output=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {'status': 'accepted', 'code': 'smtp_accepted_not_inbox_proof'})
            self.assertEqual(result.stderr, b'')

    def test_linux_parent_death_stops_actual_private_smtp_worker(self):
        if os.sys.platform != 'linux':
            self.assertNotEqual(os.sys.platform, 'linux')
            return  # The strict Linux gate executes the native lifetime proof.
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / 'fixture_child.py'
            child.write_text('''import runpy, smtplib, time
class FixtureSMTP:
    def __init__(self, *args, **kwargs):
        print('smtp-fixture-ready', flush=True)
        time.sleep(60)
smtplib.SMTP_SSL = FixtureSMTP
runpy.run_path(''' + repr(str(Path(smtp_transport.__file__).resolve())) + ''', run_name='__main__')
''', encoding='utf-8')
            parent = Path(directory) / 'fixture_parent.py'
            value = {'settings': self.settings, 'recipient': 'to@example.com', 'message': 'Zml4dHVyZQ=='}
            parent.write_text('''import json, subprocess, sys, time
p = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', ''' + repr(str(child)) + ''', '--smtp-worker'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
p.stdin.write(''' + repr(json.dumps(value).encode()) + '''); p.stdin.close()
assert p.stdout.readline() == b'smtp-fixture-ready\\n'
print(p.pid, flush=True)
time.sleep(60)
''', encoding='utf-8')
            process = subprocess.Popen([os.sys.executable, '-I', '-B', str(parent)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child_pid = None
            child_identity = None
            def child_record():
                try:
                    fields = Path(f'/proc/{child_pid}/stat').read_text().rsplit(')', 1)[1].split()
                    return fields[0], fields[19]
                except FileNotFoundError:
                    return None
            try:
                import select
                self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'private fixture not ready')
                child_pid = int(process.stdout.readline())
                child_identity = child_record()[1]
                process.kill()
                process.wait(timeout=5)
                def child_running():
                    # A dead orphan can await namespace init reaping; it must
                    # not remain live or continue an SMTP exchange.
                    row = child_record()
                    return row is not None and row[1] == child_identity and row[0] not in ('Z', 'X')
                deadline = time.monotonic() + 3
                while child_running() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(child_running(), 'SMTP child survived its parent')
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
                row = child_record() if child_pid is not None else None
                if row is not None and row[1] == child_identity and row[0] not in ('Z', 'X'):
                    import signal
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    unittest.main()
