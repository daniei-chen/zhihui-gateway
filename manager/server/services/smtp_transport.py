"""Finite SMTP worker: credentials cross a private pipe, never argv or disk."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess
import sys

TOTAL_TIMEOUT_SECONDS = 45
MAX_MESSAGE_BYTES = 131072
MAX_INPUT_BYTES = 262144
ENDPOINTS = {'smtp.qq.com': frozenset((465, 587)), 'smtp.163.com': frozenset((465,))}
RESULTS = frozenset({
    ('accepted', 'smtp_accepted_not_inbox_proof'),
    ('retry', 'smtp_envelope_rejected'), ('rejected', 'smtp_envelope_rejected'),
    ('retry', 'smtp_data_rejected'), ('rejected', 'smtp_data_rejected'),
    ('retry', 'smtp_explicit_rejection'), ('rejected', 'smtp_explicit_rejection'),
    ('unknown', 'smtp_acceptance_unknown'), ('retry', 'smtp_connection_failed'),
})


def validate_endpoint(host, port):
    if (not isinstance(host, str) or type(port) is not int
            or host not in ENDPOINTS or port not in ENDPOINTS[host]):
        raise ValueError('invalid_smtp_endpoint')


def _validate(settings, recipient, raw):
    if (type(settings) is not dict or set(settings) != {'host', 'port', 'username', 'password'}
            or not isinstance(settings['password'], str) or not 0 < len(settings['password']) <= 4096
            or not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MESSAGE_BYTES):
        raise ValueError('invalid_smtp_request')
    validate_endpoint(settings['host'], settings['port'])
    for address in (settings['username'], recipient):
        if (not isinstance(address, str) or len(address) > 254
                or not re.fullmatch(r'[^\s<>\x00-\x1f\x7f@]+@[^\s<>\x00-\x1f\x7f@]+\.[A-Za-z]{2,63}', address)):
            raise ValueError('invalid_smtp_address')


def _exchange(settings, recipient, raw, *, smtp_factory=None):
    """One attempt only; a missing DATA acknowledgement is never safe to replay."""
    _validate(settings, recipient, raw)
    sending_data = False
    transport = None
    try:
        factory = smtp_factory or (smtplib.SMTP_SSL if settings['port'] == 465 else smtplib.SMTP)
        kwargs = {'timeout': 15}
        if settings['port'] == 465:
            kwargs['context'] = ssl.create_default_context()
        transport = factory(settings['host'], settings['port'], **kwargs)
        transport.ehlo()
        if settings['port'] == 587:
            transport.starttls(context=ssl.create_default_context())
            transport.ehlo()
        transport.login(settings['username'], settings['password'])
        for step in (lambda: transport.mail(settings['username']), lambda: transport.rcpt(recipient)):
            code, _reply = step()
            if not 200 <= code < 300:
                return {'status': 'retry' if 400 <= code < 500 else 'rejected', 'code': 'smtp_envelope_rejected'}
        sending_data = True
        code, _reply = transport.data(raw)
        if 200 <= code < 300:
            return {'status': 'accepted', 'code': 'smtp_accepted_not_inbox_proof'}
        return {'status': 'retry' if 400 <= code < 500 else 'rejected', 'code': 'smtp_data_rejected'}
    except smtplib.SMTPResponseException as error:
        return {'status': 'retry' if 400 <= error.smtp_code < 500 else 'rejected', 'code': 'smtp_explicit_rejection'}
    except Exception:
        return {'status': 'unknown' if sending_data else 'retry',
                'code': 'smtp_acceptance_unknown' if sending_data else 'smtp_connection_failed'}
    finally:
        if transport is not None:
            try:
                transport.close()
            except (OSError, smtplib.SMTPException):
                pass


def send(settings, recipient, raw, *, smtp_factory=None):
    _validate(settings, recipient, raw)
    if smtp_factory is not None:
        return _exchange(settings, recipient, raw, smtp_factory=smtp_factory)
    request = json.dumps({'settings': settings, 'recipient': recipient,
                          'message': base64.b64encode(raw).decode('ascii')}, ensure_ascii=True).encode('ascii')
    if len(request) > MAX_INPUT_BYTES:
        raise ValueError('smtp_request_too_large')
    # DNS and slow-drip replies are not bounded by smtplib's socket timeout.
    # An owned child can be killed and reaped at the total deadline, without
    # releasing a live sending thread or exposing secrets in command arguments.
    environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
    try:
        result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8',
                                 str(Path(__file__).resolve()), '--smtp-worker'],
                                input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=TOTAL_TIMEOUT_SECONDS, env=environment, close_fds=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired:
        return {'status': 'unknown', 'code': 'smtp_total_deadline_unknown'}
    except OSError:
        return {'status': 'retry', 'code': 'smtp_worker_unavailable'}
    try:
        if result.returncode or len(result.stdout) > 512:
            raise ValueError('smtp_worker_failed')
        value = json.loads(result.stdout)
        if (type(value) is not dict or set(value) != {'status', 'code'}
                or (value['status'], value['code']) not in RESULTS):
            raise ValueError('invalid_smtp_receipt')
        return value
    except (ValueError, TypeError):
        return {'status': 'unknown', 'code': 'smtp_worker_outcome_unknown'}


def main():
    if sys.argv[1:] != ['--smtp-worker'] or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ValueError('private_smtp_worker_only')
    if os.name == 'posix':
        import resource
        import signal
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.alarm(TOTAL_TIMEOUT_SECONDS)
        if sys.platform == 'linux':
            import ctypes
            parent = os.getppid()
            # A killed Manager must not leave a live SMTP child outside the
            # next worker's lease. No child-created files or daemon processes.
            if parent == 1 or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
                raise RuntimeError('smtp_parent_lifetime_unavailable')
            if os.getppid() != parent:
                raise RuntimeError('smtp_parent_identity_changed')
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('smtp_request_too_large')
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {'settings', 'recipient', 'message'}:
        raise ValueError('invalid_smtp_request')
    message = base64.b64decode(value['message'], validate=True)
    result = _exchange(value['settings'], value['recipient'], message)
    sys.stdout.write(json.dumps(result, separators=(',', ':')) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
