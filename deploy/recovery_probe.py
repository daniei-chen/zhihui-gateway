#!/usr/bin/env python3
"""Internal offline ASGI probe; launched only by restore_drill after trust checks.

This is a guard for reviewed Python application code, not a sandbox for hostile
native code. Archived executables are never imported. The parent also supplies
process time/memory/file limits and a private disposable state directory.
"""
from __future__ import annotations

import json
import importlib.util
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zoneinfo


def main():
    # Apply address-space limits AFTER exec. A long-running parent test process
    # can already have >1 GiB of reserved thread stacks before the fork.
    import resource
    for kind, requested in ((resource.RLIMIT_AS, 1024**3), (resource.RLIMIT_CPU, 60),
                            (resource.RLIMIT_FSIZE, 2 * 1024**3)):
        _, hard = resource.getrlimit(kind)
        limit = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(kind, (limit, limit))
    checkout, state, inventory_file = map(lambda value: Path(value).resolve(), sys.argv[1:4])
    inventory = json.loads(inventory_file.read_text())
    trusted = {str(checkout / name) for name in inventory}
    embedded_platform_required = 'server/qtgcore/runtime.py' in inventory
    # traceback/inspect may read the already-running trusted probe itself.
    trusted.add(str(Path(__file__).resolve()))
    runtime_roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    # The hash-locked tzdata package lives in the trusted Python environment.
    # Do not consult mutable host timezone paths or broaden /usr/share access.
    zoneinfo.reset_tzpath(())
    zoneinfo.ZoneInfo.clear_cache()
    timezone_spec = importlib.util.find_spec('tzdata')
    if timezone_spec is None or not timezone_spec.origin:
        raise RuntimeError('recovery probe requires the pinned tzdata package')
    timezone_root = Path(timezone_spec.origin).resolve().parent / 'zoneinfo'
    if not any(timezone_root.is_relative_to(root) for root in runtime_roots):
        raise RuntimeError('timezone package must belong to the trusted runtime')
    own_stat = Path(f'/proc/{os.getpid()}/stat')
    release_marker = checkout / '.version'
    result_path = state / '.application-probe.json'
    denied = {'read': 0, 'write': 0, 'network': 0, 'process': 0}
    rejected_bytecode = {'reads': 0}
    runtime_reads = {'self_stat': 0, 'timezone_data': 0}
    missing_release_marker = {'reads': 0}

    def inside(path, roots):
        return any(path == root or path.is_relative_to(root) for root in roots)

    def path_for(value):
        if isinstance(value, int) or value is None:
            return None
        return Path(os.fsdecode(value)).resolve()

    def writable(value):
        path = path_for(value)
        if path is not None and not inside(path, (state,)):
            denied['write'] += 1
            raise PermissionError('recovery probe forbids external writes')

    def audit(event, args):
        if event == 'open':
            if isinstance(args[0], int) or args[0] is None:
                return
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                writable(args[0])
                return
            # A source checkout has no generated deployment .version marker.
            # Preserve native absence semantics without opening an unreviewed
            # marker. Inspect the requested name before final symlink resolution
            # so a .version symlink to a trusted VERSION file is still refused.
            requested = Path(os.path.abspath(os.fsdecode(args[0])))
            if requested.name == '.version' and requested.parent.resolve() == checkout:
                try:
                    marker_info = release_marker.lstat()
                except FileNotFoundError:
                    if str(release_marker) not in trusted:
                        missing_release_marker['reads'] += 1
                        raise FileNotFoundError(errno.ENOENT, 'release marker is absent', str(release_marker)) from None
                    denied['read'] += 1
                    raise PermissionError('reviewed release marker disappeared') from None
                if (stat.S_ISLNK(marker_info.st_mode) or str(release_marker) not in trusted
                        or not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink != 1):
                    denied['read'] += 1
                    raise PermissionError('recovery probe forbids unreviewed release markers')
            path = path_for(args[0])
            if path == own_stat:
                runtime_reads['self_stat'] += 1
                return
            if path.is_relative_to(timezone_root) and path.suffix not in ('.py', '.pyc', '.pyo'):
                runtime_reads['timezone_data'] += 1
            if (str(path) not in trusted and not inside(path, (state, *runtime_roots))
                    and path != Path('/dev/null')):
                # Python probes a cache before its reviewed source. Reject the
                # untracked bytecode, but permit importlib's fallback to the
                # exact trusted .py file without claiming a forbidden data read.
                try:
                    source = importlib.util.source_from_cache(str(path))
                except (ValueError, NotImplementedError):
                    source = None
                if source is not None and str(Path(source).resolve()) in trusted:
                    rejected_bytecode['reads'] += 1
                else:
                    denied['read'] += 1
                raise PermissionError('recovery probe forbids external data reads')
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime'):
            writable(args[0])
        elif event in ('os.rename', 'os.replace', 'os.link', 'os.symlink'):
            writable(args[0])
            writable(args[1])
        elif event == 'sqlite3.connect':
            value = args[0]
            if value != ':memory:':
                if isinstance(value, str) and value.startswith('file:'):
                    value = unquote(urlsplit(value).path)
                writable(value)
        elif event in ('socket.connect', 'socket.bind', 'socket.sendto', 'socket.getaddrinfo',
                       'socket.gethostbyname', 'socket.gethostbyaddr'):
            denied['network'] += 1
            raise PermissionError('recovery probe forbids network access')
        elif event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.fork', 'os.forkpty'):
            denied['process'] += 1
            raise PermissionError('recovery probe forbids subprocesses')

    # Set all writable state explicitly. The clean child environment contains no
    # inherited WB/QTG secrets, proxies, or production configuration overrides.
    data = state / 'manager/data'
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    (state / 'gateway/auths').mkdir(mode=0o700, parents=True, exist_ok=True)
    (state / 'tmp').mkdir(mode=0o700, exist_ok=True)
    tempfile.tempdir = str(state / 'tmp')
    os.environ.update({
        'WB_DATA_DIR': str(data), 'WB_DB': str(data / 'manager.db'),
        'WB_USERS_FILE': str(data / 'users.json'), 'WB_AUTH_DIR': str(state / 'gateway/auths'),
        'WB_UPSTREAM_CONFIG': str(state / 'gateway/config.json'),
        'WB_UPSTREAM_DIR': str(state / 'gateway'), 'WB_INSTALL_DIR': str(checkout),
        'WB_BACKGROUND_JOBS': '0', 'WB_SHARED_ADMISSION': '0', 'WB_ENABLE_DOCS': '0',
        'WB_MANAGER_HOST': '127.0.0.1', 'WB_MANAGER_PORT': '0',
        'WB_BACKUP_STATUS_FILE': str(state / 'backup-status.json'),
        'WB_QTG_IDENTITY_SECRET_FILE': str(state / 'identity.key'),
        # Explicit safe engine choices must not consult mutable host switches.
        # Startup creates the real adapters, but no upstream traffic is allowed.
        'QTG_QODER_ENGINE': 'off', 'QTG_TRAE_ENGINE': 'bff',
        'QTG_WB_ENGINE': 'on', 'QTG_DOUBAOWORK_ENGINE': 'off',
        'QTG_CHECKIN_SCHEDULER_ENABLED': '0',
        'QTG_DATA_DIR': str(state / 'engine'), 'QTG_STATIC_DIR': str(state / 'static'),
        'QTG_AUDIT_FILE': str(state / 'engine/audit.jsonl'),
        'QTG_ADMIN_TOKEN_FILE': str(state / 'engine/secrets/admin_token'),
        'QTG_EXTRA_PASSWORD_FILES': str(state / 'engine/secrets/extra_password'),
        'QTG_SESSION_SECRET_FILE': str(state / 'engine/session_secret'),
        'QTG_INTERNAL_SERVICE_TOKEN_FILE': str(state / 'engine/secrets/internal_service_token'),
        'QTG_IDENTITY_SIGNING_KEY_FILE': str(state / 'identity.key'),
        'QTG_MANAGER_SERVICE_TOKEN_FILE': str(state / 'engine/secrets/manager_service_token'),
        'QTG_MANAGER_READ_TOKEN_FILE': str(state / 'engine/secrets/manager_read_token'),
        'QTG_QODER_PASSWORD_FILE': str(state / 'qoder/secrets/panel_password'),
        'QTG_QODER_DATA_DIR': str(state / 'qoder'),
        'QTG_QODER2_PANEL_PASSWORD_FILE': str(state / 'engine/secrets/qoder2_panel_password'),
        'QTG_QODER_API_KEY_FILE': str(state / 'engine/secrets/qoder_upstream_key'),
        'QTG_TRAE_TOKEN_FILE': str(state / 'trae/secrets/admin_token'),
        'QTG_TRAE_DATA_DIR': str(state / 'trae'),
        'QTG_TRAE_API_KEY_FILE': str(state / 'engine/secrets/trae_upstream_key'),
        'QTG_AIWORK_ADMIN_TOKEN_FILE': str(state / 'engine/secrets/aiwork_admin_token'),
        'QTG_AIWORK_API_KEY_FILE': str(state / 'engine/secrets/aiwork_api_key'),
        'QTG_AIWORK_DB': str(state / 'trae/data/aiwork.sqlite'),
        'QTG_AIWORK_LOG_DIR': str(state / 'trae/logs'),
        'QTG_WB_DB': str(data / 'manager.db'), 'QTG_WB_CONFIG_FILE': str(state / 'gateway/config.json'),
        'QTG_WB_AUTHS_DIR': str(state / 'gateway/auths'),
        'QTG_WB_STATE_FILE': str(state / 'gateway/data/state.json'),
        'QTG_DOUBAOWORK_ADMIN_PASSWORD_FILE': str(state / 'doubaowork/secrets/admin_password'),
        'QTG_DOUBAOWORK_ACCOUNTS_DIR': str(state / 'doubaowork/accounts'),
        'QTG_BACKUP_SCRIPT_TRAE': str(state / 'scripts/trae-backup.sh'),
        'QTG_BACKUP_SCRIPT_QODER': str(state / 'scripts/qoder-offsite.sh'),
        'QTG_BACKUP_GLOB_TRAE': str(state / 'backups/trae/trae-backup-*'),
        'QTG_BACKUP_GLOB_QODER': str(state / 'backups/qoder/qoder-backup-*'),
        'QTG_LIMITS_FILE': str(state / 'trae/limits.json'),
        'QTG_NOTIFY_CONFIG_FILE': str(state / 'notify/config.json'),
        'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(state / 'tmp'),
    })
    os.chdir(state)
    sys.path.insert(0, str(checkout))
    sys.addaudithook(audit)
    scope = ('manager-and-embedded-platform-offline-lifespan-health-ready' if embedded_platform_required
             else 'manager-offline-lifespan-health-ready')
    result = {'schema': 1, 'ok': False, 'scope': scope,
              'embedded_platform_required': embedded_platform_required,
              'embedded_platform_start_verified': False, 'external_services_verified': False,
              'blocked_operations': denied, 'rejected_untrusted_bytecode': rejected_bytecode,
              'runtime_reads': runtime_reads, 'missing_release_marker': missing_release_marker}
    try:
        from starlette.testclient import TestClient
        from server.main import app
        with TestClient(app, client=('127.0.0.1', 1)) as client:
            if embedded_platform_required:
                from server.qtgcore.runtime import Runtime
                # Manager assigns app.state.rt only after awaiting Runtime.startup.
                # Its health routes alone still return 200 if that startup failed.
                result['embedded_platform_start_verified'] = isinstance(getattr(app.state, 'rt', None), Runtime)
            health = client.get('/api/healthz')
            ready = client.get('/readyz')
            result['health_status'] = health.status_code
            result['ready_status'] = ready.status_code
            result['ok'] = health.status_code == 200 and ready.status_code == 200
            if embedded_platform_required and not result['embedded_platform_start_verified']:
                result['ok'] = False
        if any(denied.values()):
            result['ok'] = False
    except BaseException as exc:
        result['ok'] = False
        result['error'] = type(exc).__name__
    result_path.write_text(json.dumps(result))
    return 0 if result['ok'] else 5


if __name__ == '__main__':
    raise SystemExit(main())
