#!/usr/bin/env python3
"""Reproduce local audit microbenchmarks against an unmodified checkout.

This uses synthetic records in a TemporaryDirectory, never an existing ledger
or an upstream service. It measures local functions, not model inference speed.

Example (from the repository root, using the audit/test Python environment)::

    python tests/benchmarks/audit_hotpaths.py --baseline /path/to/baseline
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def measure_pair(before, after, *, rounds: int, batch: int = 1):
    samples = {'before_ms': [], 'after_ms': []}
    for iteration in range(rounds):
        order = [('before_ms', before), ('after_ms', after)]
        if iteration % 2:
            order.reverse()
        for label, function in order:
            started = time.perf_counter()
            for _ in range(batch):
                function()
            samples[label].append((time.perf_counter() - started) * 1000 / batch)
    old = statistics.median(samples['before_ms'])
    new = statistics.median(samples['after_ms'])
    return {**samples, 'before_median_ms': old, 'after_median_ms': new,
            'speedup': old / new, 'rounds': rounds, 'batch': batch}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--keys', type=int, default=1000)
    parser.add_argument('--logs', type=int, default=100000)
    parser.add_argument('--rounds', type=int, default=7)
    args = parser.parse_args()
    if min(args.keys, args.logs, args.rounds) < 1:
        parser.error('keys, logs and rounds must be positive')
    baseline = args.baseline.resolve()
    for name in ('portalstore.py', 'routers/stats.py'):
        if not (baseline / 'manager/server' / name).is_file():
            parser.error(f'Baseline is missing manager/server/{name}')
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / 'manager'))

    with tempfile.TemporaryDirectory(prefix='zh-audit-bench-') as directory:
        # Set all data paths before importing any server module. Do not inherit
        # operator paths from the shell that happens to launch this benchmark.
        data = Path(directory)
        for key, value in {
            'WB_DATA_DIR': data, 'WB_DB': data / 'manager.db',
            'WB_USERS_FILE': data / 'users.json', 'WB_AUTH_DIR': data / 'auths',
            'WB_UPSTREAM_CONFIG': data / 'upstream.json',
            'WB_UPSTREAM_DIR': data / 'upstream',
        }.items():
            os.environ[key] = str(value)
        from server import db, portalstore
        from server.routers import stats

        old_stats = load_module('server.routers._audit_baseline_stats',
                                baseline / 'manager/server/routers/stats.py')
        old_portal = load_module('server._audit_baseline_portalstore',
                                 baseline / 'manager/server/portalstore.py')
        conn = db.connect()
        with db.transaction() as transaction:
            transaction.executemany(
                'INSERT INTO api_keys(id,name,key_hash,prefix,created_at) VALUES(?,?,?,?,?)',
                ((i, f'synthetic-{i}', f'synthetic-hash-{i}', 'test', 1700000000)
                 for i in range(1, args.keys + 1)),
            )
            # Retain NULL, true zero, orphan and live-key samples. Request IDs
            # are synthetic; no raw prompt, credential or customer record exists.
            transaction.executemany(
                'INSERT INTO request_logs(ts,key_id,credit,status) VALUES(?,?,?,?)',
                ((1700000000 + i, None if i % 43 == 0 else 1 + i % (args.keys + 3),
                  None if i % 7 == 0 else (i % 31) / 1000, 200)
                 for i in range(args.logs)),
            )
        before_result = old_stats.credit_reconcile(user={})
        after_result = stats.credit_reconcile(user={})
        if before_result != after_result:
            raise AssertionError('Reconciliation responses differ')
        changes = conn.total_changes
        reconcile = measure_pair(lambda: old_stats.credit_reconcile(user={}),
                                 lambda: stats.credit_reconcile(user={}),
                                 rounds=args.rounds)
        if conn.total_changes != changes:
            raise AssertionError('Read-only reconciliation modified the ledger')
        reconcile.update(keys=args.keys, logs=args.logs,
                         response_sha256=hashlib.sha256(json.dumps(
                             after_result, sort_keys=True).encode()).hexdigest(),
                         responses_equal=True, ledger_unchanged=True)

        def portal_read(module):
            with module.transaction() as transaction:
                return transaction.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0]

        assert portal_read(old_portal) == portal_read(portalstore) == 0
        portal = measure_pair(lambda: portal_read(old_portal),
                              lambda: portal_read(portalstore), rounds=args.rounds, batch=100)
        with portalstore.transaction() as transaction:
            portal['sqlite_settings'] = {
                name: transaction.execute(f'PRAGMA {name}').fetchone()[0]
                for name in ('synchronous', 'foreign_keys', 'journal_mode')
            }
        report = {
            'scope': 'Local synthetic microbenchmarks; no upstream or end-to-end latency claim',
            'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'platform': platform.platform(), 'credit_reconcile': reconcile,
            'portal_read_transaction': portal,
        }
        conn.close()
        db._conn = None
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
