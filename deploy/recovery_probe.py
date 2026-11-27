#!/usr/bin/env python3
"""Internal offline ASGI probe; launched only by restore_drill after trust checks.

This is a guard for reviewed Python application code, not a sandbox for hostile
native code. Archived executables are never imported. The parent also supplies
process time/memory/file limits and a private disposable state directory.
"""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import unquote, urlsplit


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
    # traceback/inspect may read the already-running trusted probe itself.
    trusted.add(str(Path(__file__).resolve()))
    runtime_roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    result_path = state / '.application-probe.json'
    denied = {'read': 0, 'write': 0, 'network': 0, 'process': 0}
    rejected_bytecode = {'reads': 0}

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
            path = path_for(args[0])
            if path is None:
                return
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                writable(path)
            elif (str(path) not in trusted and not inside(path, (state, *runtime_roots))
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
        'QTG_DATA_DIR': str(state / 'engine'), 'QTG_STATIC_DIR': str(state / 'static'),
        'QTG_SESSION_SECRET_FILE': str(state / 'engine/session_secret'),
        'QTG_WB_DB': str(data / 'manager.db'), 'QTG_WB_CONFIG_FILE': str(state / 'gateway/config.json'),
        'QTG_WB_AUTHS_DIR': str(state / 'gateway/auths'),
        'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(state / 'tmp'),
    })
    os.chdir(state)
    sys.path.insert(0, str(checkout))
    sys.addaudithook(audit)
    result = {'schema': 1, 'ok': False, 'scope': 'manager-offline-lifespan-health-ready',
              'blocked_operations': denied, 'rejected_untrusted_bytecode': rejected_bytecode}
    try:
        from starlette.testclient import TestClient
        from server.main import app
        with TestClient(app, client=('127.0.0.1', 1)) as client:
            health = client.get('/api/healthz')
            ready = client.get('/readyz')
            result['health_status'] = health.status_code
            result['ready_status'] = ready.status_code
            result['ok'] = health.status_code == 200 and ready.status_code == 200
        if any(denied.values()):
            result['ok'] = False
    except BaseException as exc:
        result['error'] = type(exc).__name__
    result_path.write_text(json.dumps(result))
    return 0 if result['ok'] else 5


if __name__ == '__main__':
    raise SystemExit(main())
