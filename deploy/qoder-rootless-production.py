#!/usr/bin/python3
"""Start the reviewed Qoder release and hand the data plane to UID 10001.

The release broker remains root-owned because it must verify and atomically
switch the signed ``current`` pointer.  The serving process is deliberately
started through ``setpriv`` after that verification, so application code never
runs with the launcher's root identity or inherited capabilities.

This file is a host deployment asset.  It expects the Qoder release store at
``/opt/qoder-release`` and the persistent state contract used by the gateway.
It does not read credentials itself; Qoder reads them from ``ACCOUNTS_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


BASE = Path('/opt/qoder-release')
STATE = Path('/opt/workbuddy-manager/engines')
TRUST_ENTRY = BASE / 'bin/qoder_trust.py'
CACHE = BASE / 'python-cache'
PORT = '8799'


def safe_environment() -> dict[str, str]:
    return {
        'PATH': '/usr/bin:/bin',
        'HOME': str(STATE),
        'LANG': 'C.UTF-8',
        'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONSAFEPATH': '1',
        'ACCOUNTS_DIR': str(STATE / 'accounts'),
        'QD_PROXY_USAGE_DIR': str(STATE / 'usage'),
        'QD_LOG_DIR': str(STATE / 'logs'),
    }


def current_release() -> Path:
    root = (BASE / 'current').resolve(strict=True)
    if root.parent != BASE / 'releases':
        raise SystemExit('current release escaped the fixed release store')
    return root


def runtime() -> None:
    if os.geteuid() != 10001 or os.getegid() != 10001 or os.getgroups():
        raise SystemExit('rootless identity contract failed')
    root = current_release()
    os.environ.clear()
    os.environ.update(safe_environment())
    os.chdir(root)
    sys.path.insert(0, str(root))
    entry = root / 'qoder_proxy.py'
    sys.argv = [
        str(entry), '--host', '127.0.0.1', '--port', PORT,
        '--accounts-dir', str(STATE / 'accounts'),
        '--usage-dir', str(STATE / 'usage'),
    ]
    runpy.run_path(str(entry), run_name='__main__')


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == 'runtime':
        runtime()
        return
    if os.geteuid() != 0:
        raise SystemExit('root verification phase required')
    os.environ.clear()
    os.environ.update({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    verifier = runpy.run_path(str(TRUST_ENTRY))
    verifier['verify_release'](current_release())
    os.execve(
        '/usr/bin/setpriv',
        [
            '/usr/bin/setpriv', '--reuid=10001', '--regid=10001',
            '--clear-groups', '--no-new-privs', '--inh-caps=-all',
            '--ambient-caps=-all', '/usr/bin/python3', '-I', '-S', '-B', '-X',
            'pycache_prefix=' + str(CACHE), str(Path(__file__).resolve()),
            'runtime',
        ],
        safe_environment(),
    )


if __name__ == '__main__':
    main()
