#!/usr/bin/python3 -I
"""Root-only maintenance admission fence. No production path overrides or shell."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

INSTALL = Path('/opt/zhihui-release/container-maintenance')
STATE = Path('/var/lib/zhihui-container-maintenance')
JOURNAL = STATE / 'journal.json'
LOCK = Path('/run/zh-release.lock')
PUBLIC = Path('/etc/nginx/sites-enabled/zhihui.kaogong.art')
LOOPBACK = Path('/etc/nginx/conf.d/zh-manager-loopback.conf')
UPSTREAM = Path('/etc/nginx/conf.d/zh-manager-upstream.conf')
USERS = Path('/opt/workbuddy-manager/data/users.json')
COMMANDS = {
    'nginx_test': ['/usr/sbin/nginx', '-t'],
    'nginx_reload': ['/usr/bin/systemctl', 'reload', 'nginx.service'],
    'primary_pid': ['/usr/bin/systemctl', 'show', 'workbuddy-web.service', '--property=MainPID', '--value'],
    'nginx_pid': ['/usr/bin/systemctl', 'show', 'nginx.service', '--property=MainPID', '--value'],
    'listeners': ['/usr/bin/ss', '-H', '-lntp'],
    'connections': ['/usr/bin/ss', '-H', '-nt'],
}
SERVICES = {
    'zhihui-workbuddy': (7863, [('/healthz', 200), ('/v1/models', 401)]),
    'zhihui-qoder': (8790, [('/ping', 200), ('/v1/models', 401)]),
    'zhihui-trae': (8080, [('/health', 200), ('/v1/models', 401)]),
    'trae-bff': (8792, [('/api/healthz', 200)]),
    'zcode-push': (8788, [('/zpush/healthz', 200), ('/zpush/poll', 401)]),
}
MARKER = b'# zh-container-maintenance-v1'
PUBLIC_RULE = b'if ($uri ~ ^/(api|v1|v2|qoder|trae|wb|workbuddy|zpush)(/|$)) { return 503; }'
LOCAL_RULE = b'if ($uri !~ ^/(healthz|readyz|api/healthz|api/readyz)/?$) { return 503; }'


class Failure(RuntimeError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def trusted(path, missing=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Failure('untrusted_path')
    for item in [path, *path.parents]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            if missing and item == path:
                continue
            raise Failure('missing_trusted_path')
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Failure('unsafe_root_path')
    return path


@contextlib.contextmanager
def locked():
    trusted(LOCK, missing=True)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
            raise Failure('unsafe_release_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('release_lock_busy')
        yield
    finally:
        os.close(fd)


def atomic(path, raw, mode):
    trusted(path.parent); trusted(path, missing=True)
    temporary = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def save(journal, phase=None):
    if phase is not None:
        journal['phase'] = phase
    journal['updated_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    atomic(JOURNAL, (json.dumps(journal, indent=2) + '\n').encode(), 0o600)


def load():
    trusted(JOURNAL)
    if JOURNAL.stat().st_mode & 0o077:
        raise Failure('journal_not_private')
    journal = json.loads(JOURNAL.read_bytes())
    if journal.get('schema') != 1 or journal.get('commands') != COMMANDS:
        raise Failure('journal_schema_or_commands_mismatch')
    if journal.get('paths') != [str(PUBLIC), str(LOOPBACK)]:
        raise Failure('journal_paths_mismatch')
    if [item.get('path') for item in journal.get('files', [])] != journal['paths']:
        raise Failure('snapshot_paths_mismatch')
    for item in journal['files']:
        if type(item.get('mode')) is not int or item['mode'] & ~0o755 or item['mode'] & 0o022:
            raise Failure('snapshot_mode_unsafe')
        original = base64.b64decode(item['original_base64'], validate=True)
        fenced = base64.b64decode(item['fenced_base64'], validate=True)
        if digest(original) != item['original_sha256'] or digest(fenced) != item['fenced_sha256']:
            raise Failure('snapshot_hash_mismatch')
        rule, count = (PUBLIC_RULE, 2) if item['path'] == str(PUBLIC) else (LOCAL_RULE, 1)
        if fenced_bytes(original, rule, count) != fenced:
            raise Failure('snapshot_gate_contract_mismatch')
    return journal


def tokens(raw):
    """Small Nginx lexical scanner: comments/quoted braces are not block braces."""
    result = []; index = 0
    while index < len(raw):
        char = raw[index]
        if char in b' \t\r\n':
            index += 1; continue
        if char == 35:
            end = raw.find(b'\n', index); index = len(raw) if end < 0 else end + 1; continue
        start = index
        if char in (34, 39):
            quote = char; index += 1
            while index < len(raw):
                if raw[index] == 92:
                    index += 2; continue
                if raw[index] == quote:
                    index += 1; break
                index += 1
            else:
                raise Failure('unclosed_nginx_string')
        elif char in b'{};':
            index += 1
        else:
            while index < len(raw) and raw[index] not in b' \t\r\n{};#':
                if raw[index] == 92:
                    index += 1
                index += 1
        result.append((raw[start:index], start, index))
    return result


def fenced_bytes(raw, rule, expected_blocks):
    if MARKER in raw:
        raise Failure('preexisting_unjournaled_fence')
    parts = tokens(raw); depth = 0; openings = []
    for index, (token, _, end) in enumerate(parts):
        if token == b'{':
            if index and parts[index-1][0] == b'server':
                if depth != 0:
                    raise Failure('unexpected_nested_server')
                openings.append(end)
            depth += 1
        elif token == b'}':
            depth -= 1
            if depth < 0:
                raise Failure('unbalanced_nginx')
    if depth or len(openings) != expected_blocks:
        raise Failure('unexpected_server_block_count')
    result = raw
    insertion = b'\n    ' + MARKER + b'\n    ' + rule + b'\n'
    for offset in reversed(openings):
        result = result[:offset] + insertion + result[offset:]
    return result


class Host:
    def command(self, key):
        argv = COMMANDS[key]; trusted(argv[0])
        result = subprocess.run(argv, capture_output=True, timeout=20,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise Failure('command_failed_' + key)
        return result.stdout.decode('utf-8', errors='strict')

    def identity(self, pid):
        try:
            raw = (Path('/proc') / str(pid) / 'stat').read_text()
        except FileNotFoundError:
            return None
        tail = raw[raw.rfind(')') + 2:].split()
        return {'pid': int(pid), 'starttime': int(tail[19])}

    def primary(self, require_background_off=True):
        text = self.command('primary_pid').strip()
        if not text.isdigit() or int(text) <= 1:
            raise Failure('primary_not_running')
        identity = self.identity(int(text))
        if identity is None:
            raise Failure('primary_disappeared')
        if require_background_off:
            raw = (Path('/proc') / text / 'environ').read_bytes()
            selected = {k: v for entry in raw.split(b'\0') for k, _, v in [entry.partition(b'=')]
                        if k in (b'WB_BACKGROUND_JOBS', b'WB_USERS_FILE', b'WB_DATA_DIR')}
            if selected.get(b'WB_BACKGROUND_JOBS') != b'0':
                raise Failure('primary_background_jobs_not_explicitly_zero')
            if selected.get(b'WB_USERS_FILE', str(USERS).encode()) != str(USERS).encode():
                raise Failure('unexpected_users_path')
            if selected.get(b'WB_DATA_DIR', str(USERS.parent).encode()) != str(USERS.parent).encode():
                raise Failure('unexpected_data_path')
        return identity

    def workers(self):
        text = self.command('nginx_pid').strip()
        if not text.isdigit() or int(text) <= 1:
            raise Failure('nginx_master_absent')
        master = self.identity(int(text)); output = []
        childfile = Path('/proc') / text / 'task' / text / 'children'
        for child in childfile.read_text().split():
            try:
                command = (Path('/proc') / child / 'cmdline').read_bytes()
            except FileNotFoundError:
                continue
            if command.startswith(b'nginx: worker process'):
                identity = self.identity(int(child))
                if identity:
                    output.append(dict(identity, shutting_down=b'shutting down' in command))
        if not master or not output:
            raise Failure('nginx_workers_absent')
        return master, output

    def barrier(self, journal, timeout=120):
        deadline = time.monotonic() + timeout
        while True:
            master, workers = self.workers()
            if master != journal['nginx_master']:
                raise Failure('nginx_master_identity_changed')
            old_alive = any(self.identity(row['pid']) == {'pid': row['pid'], 'starttime': row['starttime']}
                            for row in journal['old_workers'])
            if not old_alive and not any(row['shutting_down'] for row in workers):
                return
            if time.monotonic() >= deadline:
                raise Failure('old_nginx_workers_alive_no_force_kill')
            time.sleep(.25)

    def listeners(self, primary):
        rows = self.command('listeners').splitlines(); live = []
        for row in rows:
            fields = row.split()
            if len(fields) < 5:
                raise Failure('unrecognized_ss_listener_format')
            port = endpoint_port(fields[3])
            if port == 7865:
                raise Failure('helper_7865_still_listening')
            if port == 7866:
                if fields[3] != '127.0.0.1:7866' or 'pid=' + str(primary['pid']) + ',' not in row:
                    raise Failure('primary_listener_identity_mismatch')
                live.append(row)
        if len(live) != 1:
            raise Failure('primary_listener_not_unique')

    def upstream(self):
        trusted(UPSTREAM)
        values = [part[0] for part in tokens(UPSTREAM.read_bytes())]
        expected = [b'upstream', b'zhihui_mgr', b'{', b'server', b'127.0.0.1:7866', b'max_fails=0', b';', b'}']
        expected_keepalive = [b'upstream', b'zhihui_mgr', b'{', b'server', b'127.0.0.1:7866', b'max_fails=0', b';', b'keepalive', b'64', b';', b'}']
        if values not in (expected, expected_keepalive):
            raise Failure('upstream_not_exclusively_7866')

    def connections(self, service):
        port = SERVICES[service][0]; count = 0
        for row in self.command('connections').splitlines():
            fields = row.split()
            if len(fields) < 5:
                raise Failure('unrecognized_ss_connection_format')
            if fields[0] == 'ESTAB' and port in (endpoint_port(fields[3]), endpoint_port(fields[4])):
                count += 1
        return count

    def cookie(self):
        # The application's credential file is app-owned, not an executable trust anchor.
        for item in [USERS, *USERS.parents]:
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, 10001) or info.st_mode & 0o022:
                raise Failure('unsafe_users_path')
        fd = os.open(USERS, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4 * 1024 * 1024:
                raise Failure('unsafe_users_file')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                cfg = json.load(stream)
        finally:
            os.close(fd)
        return session_cookie(cfg, int(time.time()))

    def http(self, port, path, method='GET', body=None, authenticated=False):
        headers = {'Connection': 'close'}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode(); headers['Content-Type'] = 'application/json'
        if authenticated:
            headers['Cookie'] = 'wb_session=' + self.cookie()
            headers['Origin'] = 'http://127.0.0.1:7866'
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse(); data = response.read(65537)
            if len(data) > 65536:
                raise Failure('oversized_local_response')
            return response.status, data
        finally:
            connection.close()

    def drain(self, action=None):
        code, raw = self.http(7866, '/api/system/drain', 'POST' if action else 'GET',
                              {'action': action} if action else None, authenticated=True)
        if code != 200:
            raise Failure('manager_drain_http_failed')
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get('draining')) is not bool:
            raise Failure('invalid_manager_drain_state')
        total = value.get('total_in_flight'); counts = value.get('in_flight')
        if type(total) is not int or total < 0 or not isinstance(counts, dict) or any(
                type(v) is not int or v < 0 for v in counts.values()) or sum(counts.values()) != total:
            raise Failure('invalid_manager_inflight')
        return value


def endpoint_port(value):
    try:
        return int(value.rsplit(':', 1)[1])
    except (IndexError, ValueError):
        raise Failure('unrecognized_ss_endpoint')


def session_cookie(cfg, now):
    user = next((row for row in cfg['users'] if row.get('role') == 'admin' and row.get('username')), None)
    if not user or not isinstance(cfg.get('secret'), str) or not cfg['secret']:
        raise Failure('no_admin_session_signing_contract')
    payload = json.dumps({'username': user['username'], 'role': 'admin', 'sv': int(user.get('sv') or 0),
                          'iat': now, 'orig': now, 'exp': now + 60})
    signature = hmac.new(cfg['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode((payload + '|' + signature).encode()).decode()


def assert_files(journal, allow_original=False):
    for item in journal['files']:
        path = trusted(item['path']); actual = digest(path.read_bytes())
        allowed = {item['fenced_sha256']}
        if allow_original:
            allowed.add(item['original_sha256'])
        if actual not in allowed:
            raise Failure('nginx_gate_file_drift')


def write_files(journal, variant):
    assert_files(journal, allow_original=True)
    for item in journal['files']:
        atomic(Path(item['path']), base64.b64decode(item[variant + '_base64']), item['mode'])


def verify_fence(host, journal, require_primary=True):
    assert_files(journal)
    host.barrier(journal, timeout=0)
    host.upstream()
    if require_primary:
        primary = host.primary()
        if primary != journal.get('primary'):
            raise Failure('bound_primary_identity_changed')
        host.listeners(primary)
    return True


def reload_and_wait(host, journal, phase):
    host.command('nginx_test')
    master, workers = host.workers()
    if master != journal['nginx_master']:
        raise Failure('nginx_master_identity_changed')
    # Retain every still-possible old generation across retries.
    known = {(row['pid'], row['starttime']): row for row in journal['old_workers'] + workers}
    journal['old_workers'] = list(known.values())
    save(journal, phase + '-reload-pending')
    host.command('nginx_reload')
    save(journal, phase + '-reload-accepted')
    host.barrier(journal)
    save(journal, phase + '-workers-exited')


def begin(host):
    if JOURNAL.exists():
        journal = load()
        if journal['phase'] in ('resumed', 'restored-before-cutover'):
            raise Failure('completed_journal_requires_manual_archive_before_next_window')
        assert_files(journal, allow_original=True)
    else:
        trusted(STATE, missing=True); STATE.mkdir(mode=0o700, exist_ok=True); trusted(STATE)
        primary = host.primary(False); initial = host.drain()
        master, workers = host.workers(); files = []
        for path, rule, count in ((PUBLIC, PUBLIC_RULE, 2), (LOOPBACK, LOCAL_RULE, 1)):
            trusted(path); original = path.read_bytes(); fenced = fenced_bytes(original, rule, count)
            files.append({'path': str(path), 'mode': stat.S_IMODE(path.stat().st_mode),
                          'original_base64': base64.b64encode(original).decode(),
                          'fenced_base64': base64.b64encode(fenced).decode(),
                          'original_sha256': digest(original), 'fenced_sha256': digest(fenced)})
        journal = {'schema': 1, 'commands': COMMANDS, 'paths': [str(PUBLIC), str(LOOPBACK)],
                   'files': files, 'original_primary': primary, 'original_draining': initial['draining'],
                   'nginx_master': master, 'old_workers': workers, 'cutover_authorized': False,
                   'drain_started': False, 'primary': None}
        save(journal, 'prepared-snapshots-durable')
    if journal.get('primary') is not None:
        verify_fence(host, journal); return {'phase': journal['phase'], 'already_active': True}
    if journal['phase'] == 'fence-workers-exited':
        verify_fence(host, journal, require_primary=False)
        return {'phase': journal['phase'], 'already_active': True}
    write_files(journal, 'fenced'); save(journal, 'fence-files-written')
    reload_and_wait(host, journal, 'fence')
    return {'phase': journal['phase'], 'next': 'trusted-publish-background-off-then-rebind-primary'}


def rebind(host):
    journal = load(); verify_fence(host, journal, require_primary=False)
    primary = host.primary(); host.listeners(primary)
    if journal['primary'] not in (None, primary):
        raise Failure('already_bound_to_different_primary')
    journal['primary'] = primary
    save(journal, 'primary-bound-background-off')
    return {'phase': journal['phase'], 'primary': primary}


def check_drain(host, service):
    journal = load(); verify_fence(host, journal)
    state = host.drain()
    if not state['draining']:
        if journal['drain_started']:
            raise Failure('manager_drain_was_unexpectedly_resumed')
        host.drain('begin'); state = host.drain()
    if not state['draining']:
        raise Failure('manager_not_actually_draining')
    if host.primary() != journal['primary']:
        raise Failure('primary_changed_during_drain')
    journal['drain_started'] = True
    # A successful helper can authorize a cutover. Early restore must now refuse.
    journal['cutover_authorized'] = True
    save(journal, 'draining-container-cutover-authorized')
    total = state['total_in_flight'] + host.connections(service)
    return {'draining': True, 'inflight': total}


def check_ready(host, service):
    journal = load(); verify_fence(host, journal)
    if not host.drain()['draining']:
        raise Failure('manager_not_draining_during_readiness')
    if host.http(7866, '/healthz')[0] != 200:
        raise Failure('manager_health_failed')
    port, paths = SERVICES[service]
    for path, expected in paths:
        if host.http(port, path)[0] != expected:
            raise Failure('backend_health_or_anonymous_auth_failed')
    return {'ok': True}


def refence(host, journal):
    """Best-effort safety compensation; failures are recorded, never hidden."""
    failures = []
    try:
        if host.primary(False) == journal.get('primary'):
            host.drain('begin')
    except Exception:
        failures.append('manager_redrain_failed')
    try:
        write_files(journal, 'fenced')
        reload_and_wait(host, journal, 'refence')
    except Exception:
        failures.append('nginx_refence_not_confirmed')
    journal['compensation_failures'] = failures
    save(journal, 'failed-closed-maintenance-review-required')


def finish(host, restore=False):
    journal = load()
    if journal['phase'] in ('resumed', 'restored-before-cutover'):
        for item in journal['files']:
            if digest(trusted(item['path']).read_bytes()) != item['original_sha256']:
                raise Failure('completed_restore_drift')
        identity = journal['primary'] if journal['phase'] == 'resumed' else journal['original_primary']
        if host.primary(False) != identity or host.drain()['draining']:
            raise Failure('completed_restore_runtime_changed')
        return {'phase': journal['phase'], 'already_complete': True}
    if restore:
        assert_files(journal, allow_original=True)
        if journal['cutover_authorized'] or journal['drain_started'] or journal['original_draining']:
            raise Failure('early_restore_not_permitted_after_drain_or_cutover')
        if host.primary(False) != journal['original_primary'] or host.drain()['draining']:
            raise Failure('early_restore_primary_changed_or_draining')
    else:
        verify_fence(host, journal)
        for service in SERVICES:
            check_ready(host, service)
        if host.drain()['total_in_flight']:
            raise Failure('manager_inflight_before_resume')
    save(journal, 'restoring-before-cutover' if restore else 'resuming-admission')
    try:
        if not restore:
            host.drain('resume')
            if host.drain()['draining'] or host.primary() != journal['primary']:
                raise Failure('resume_not_confirmed_same_primary')
        write_files(journal, 'original')
        reload_and_wait(host, journal, 'restore-original')
        save(journal, 'restored-before-cutover' if restore else 'resumed')
    except Exception:
        refence(host, journal)
        raise Failure('restore_failed_compensation_recorded')
    return {'phase': journal['phase'], 'mutable_state_rolled_back': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    for command in ('begin', 'rebind-primary', 'status'):
        sub.add_parser(command)
    for command in ('resume', 'restore'):
        child = sub.add_parser(command); child.add_argument('--confirm', action='store_true', required=True)
    for command in ('check-drain', 'check-ready'):
        child = sub.add_parser(command); child.add_argument('service', choices=tuple(SERVICES))
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise Failure('root_required')
        trusted(Path(__file__).absolute())
        if Path(__file__).absolute().parent != INSTALL:
            raise Failure('install_path_mismatch')
        host = Host()
        # Helpers are called while container_recreate owns the release lock.
        with contextlib.nullcontext() if args.mode.startswith('check-') else locked():
            if args.mode == 'begin': result = begin(host)
            elif args.mode == 'rebind-primary': result = rebind(host)
            elif args.mode == 'resume': result = finish(host)
            elif args.mode == 'restore': result = finish(host, restore=True)
            elif args.mode == 'check-drain': result = check_drain(host, args.service)
            elif args.mode == 'check-ready': result = check_ready(host, args.service)
            else:
                journal = load()
                result = {key: journal.get(key) for key in ('phase', 'primary', 'original_primary',
                          'cutover_authorized', 'drain_started', 'compensation_failures')}
        print(json.dumps(result, separators=(',', ':'))); return 0
    except Exception as error:
        # Never print HTTP responses, request headers, credentials, subprocess stderr or env.
        code = str(error) if isinstance(error, Failure) else type(error).__name__
        if not args.mode.startswith('check-'):
            try:
                journal = load(); journal['last_failure'] = code; save(journal)
            except Exception:
                pass
        print(json.dumps({'ok': False, 'error': code}, separators=(',', ':'))); return 1


if __name__ == '__main__':
    raise SystemExit(main())
