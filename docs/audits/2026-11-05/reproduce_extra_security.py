#!/usr/bin/env python3
"""Portable local audit proofs with explicit pre-fix/current source provenance.

Run from a checkout containing Git object 4c6b4cb8cb790698e0753909745dfe8a7c78dd9f.
Only standard-library modules and temporary synthetic files/databases are used.
There is no app import, provider/network call, SSH, deployment or real credential.

Dailyplan, keyimport and notify reproduce PRE-FIX behavior from that Git object.
Their fixed behavior is covered by the three named regression test files below;
this script does not label old counterexamples as results of the fixed source.
PG restoration reads CURRENT pgsync.py and ledger.py, with the PostgreSQL transport
replaced by a temporary SQLite source, to demonstrate the remaining trigger clash.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import types


REPO = Path(__file__).resolve().parents[3]
BASELINE = '4c6b4cb8cb790698e0753909745dfe8a7c78dd9f'
PRE_FIX = {
    'manager/server/services/dailyplan.py',
    'manager/server/services/keyimport.py',
    'manager/server/notify.py',
}
CURRENT = {
    'manager/server/services/pgsync.py',
    'manager/server/ledger.py',
}
FIXED_TESTS = (
    'manager/server/tests/test_audit_dailyplan_lifecycle.py',
    'manager/server/tests/test_audit_local_import_files.py',
    'manager/server/tests/test_audit_notify_config.py',
)
SOURCE: dict[str, bytes] = {}


def read_sources() -> None:
    for relative in sorted(PRE_FIX | CURRENT):
        if relative in PRE_FIX:
            try:
                raw = subprocess.check_output(
                    ['git', 'show', f'{BASELINE}:{relative}'], cwd=REPO,
                    stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f'Required baseline Git object is unavailable: {BASELINE}:{relative}') from exc
        else:
            raw = (REPO / relative).read_bytes()
        SOURCE[relative] = raw


def load_selected(relative, names, namespace):
    tree = ast.parse(SOURCE[relative])
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    found = {node.name for node in nodes}
    if found != names:
        raise RuntimeError(f'Source contract changed: {relative}: missing {names - found}')
    exec(compile(ast.Module(body=nodes, type_ignores=[]),
                 f'{BASELINE}:{relative}', 'exec'), namespace)
    return namespace


async def dailyplan_lost_lease():
    events = []
    renewed = asyncio.Event()

    def renew(*args):
        events.append('lease_renew_returned_false')
        renewed.set()
        return False

    async def yield_once(*args):
        await asyncio.sleep(0)

    async def upstream_work(*args):
        await renewed.wait()
        await asyncio.sleep(0)
        events.append('upstream_side_effect_after_lease_loss')
        return True, 'synthetic'

    namespace = {
        'asyncio': types.SimpleNamespace(create_task=asyncio.create_task, sleep=yield_once,
                                        CancelledError=asyncio.CancelledError, wait=asyncio.wait),
        'logger': logging.getLogger('audit.extra'),
        'tasklease': types.SimpleNamespace(
            renew=renew, release=lambda *args: events.append('lease_released')),
        'claim_task': lambda task: (True, 7), 'LEASE_TTL': 3,
        'run_task': upstream_work, 'mark_ran_fenced': lambda *args: False,
    }
    load_selected('manager/server/services/dailyplan.py', {'_lease_heartbeat', '_run_claimed'}, namespace)
    await asyncio.wait_for(namespace['_run_claimed']('checkin', {'checkin': {'hour': 0}}), 1)
    return {'tested_source': 'pre_fix_git_object', 'events': events,
            'work_continues_after_loss': 'upstream_side_effect_after_lease_loss' in events}


async def dailyplan_background_lease():
    events = []
    release_work = asyncio.Event()
    owned = []

    async def background():
        await release_work.wait()
        events.append('background_work_completed')

    async def start_async(*args):
        owned.append(asyncio.create_task(background()))
        events.append('background_work_started')
        return True, 'started'

    async def heartbeat(*args):
        await asyncio.Future()

    namespace = {
        'asyncio': asyncio, 'logger': logging.getLogger('audit.extra'),
        'tasklease': types.SimpleNamespace(release=lambda *args: events.append('lease_released')),
        'claim_task': lambda task: (True, 8), 'LEASE_TTL': 3,
        'mark_ran_fenced': lambda *args: True, '_lease_heartbeat': heartbeat,
        '_TASKS': ('checkin', 'compensation', 'claim', 'full'),
        'taskrun': types.SimpleNamespace(start_async=start_async),
    }
    load_selected('manager/server/services/dailyplan.py', {'run_task', '_run_claimed'}, namespace)
    try:
        await asyncio.wait_for(namespace['_run_claimed']('full', {'full': {'hour': 0}}), 1)
        pending_after_release = bool(owned and not owned[0].done())
    finally:
        release_work.set()
        await asyncio.gather(*owned)
    return {'tested_source': 'pre_fix_git_object', 'events': events,
            'lease_released_while_work_running': pending_after_release}


def pgsync_immutable_restore():
    with tempfile.TemporaryDirectory(prefix='audit-extra-pgsync-') as raw:
        temp = Path(raw)
        relative = 'manager/server/services/pgsync.py'
        tree = ast.parse(SOURCE[relative])
        tree.body = [node for node in tree.body
                     if not (isinstance(node, ast.ImportFrom) and node.level)]
        local_path = temp / 'local.db'
        mirror_path = temp / 'mirror.db'
        local = sqlite3.connect(local_path)
        sources = []
        try:
            ledger_tree = ast.parse(SOURCE['manager/server/ledger.py'])
            schema = next(ast.literal_eval(node.value) for node in ledger_tree.body
                          if isinstance(node, ast.Assign) and any(
                              isinstance(target, ast.Name) and target.id == 'SCHEMA'
                              for target in node.targets))
            local.executescript(schema)
            local.execute("INSERT INTO admin_events(ts,actor,action,target,outcome,previous_hash,event_hash) "
                          "VALUES(1,'synthetic','audit','','ok','','synthetic-hash')")
            local.commit()
            local.execute('VACUUM INTO ?', (str(mirror_path),))
            calls = []
            db = types.SimpleNamespace(
                _lock=threading.RLock(), connect=lambda: local,
                replace_database=lambda path: calls.append('replaced'),
                set_setting=lambda *args: calls.append('setting'))
            config = types.SimpleNamespace(DATA_DIR=temp, DB_PATH=local_path, ensure_dirs=lambda: None)
            namespace = {'__name__': 'audit_extra_pgsync', 'config': config, 'db': db, 'err_text': str}
            exec(compile(tree, str(REPO / relative), 'exec'), namespace)
            namespace['_require_psycopg'] = lambda: types.SimpleNamespace(
                connect=lambda *args, **kwargs: contextlib.nullcontext(None))

            def source(_):
                value = namespace['_SqliteSource'](mirror_path)
                sources.append(value)
                return value

            namespace['_PgSource'] = source
            reporter = types.SimpleNamespace(data={}, log=lambda *args: None,
                                             step=lambda *args, **kwargs: None)
            try:
                namespace['import_from_pg'](
                    {'host': 'synthetic.invalid', 'keep_local_backup': False}, reporter)
                error = None
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            unchanged = local.execute('SELECT COUNT(*) FROM admin_events').fetchone()[0] == 1
            return {'tested_source': 'current_worktree', 'error': error,
                    'replace_called': 'replaced' in calls, 'original_unchanged': unchanged,
                    'transport': 'temporary SQLite adapter; no PostgreSQL/network call',
                    'actual_immutable_trigger_and_import_code': True}
        finally:
            for value in sources:
                value.close()
            local.close()


def keyimport_mode_and_notify_type():
    with tempfile.TemporaryDirectory(prefix='audit-extra-files-') as raw:
        path = Path(raw) / 'provider_config.json'
        path.write_text('{"synthetic_secret":"before"}')
        path.chmod(0o600)
        namespace = {'Path': Path, 'os': os, '_BACKUP_SUFFIX': '.wbimport'}
        load_selected('manager/server/services/keyimport.py', {'_atomic_write_text'}, namespace)
        original_mask = os.umask(0o022)
        try:
            namespace['_atomic_write_text'](path, '{"synthetic_secret":"after"}')
        finally:
            os.umask(original_mask)
        notify = {'_settings': lambda: []}
        load_selected('manager/server/notify.py', {'configured_mode'}, notify)
        try:
            notify['configured_mode']()
            error = None
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        return {'tested_source': 'pre_fix_git_object', 'posix_mode_semantics': os.name == 'posix',
                'keyimport_original_mode': '0600',
                'keyimport_result_mode': oct(path.stat().st_mode & 0o777),
                'notify_list_config_error': error}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON destination; stdout is always written.')
    args = parser.parse_args()
    read_sources()
    sources = {
        relative: {'version': BASELINE if relative in PRE_FIX else 'current_worktree',
                   'sha256': hashlib.sha256(raw).hexdigest()}
        for relative, raw in sorted(SOURCE.items())
    }
    fixed_tests = {
        relative: {'sha256': hashlib.sha256((REPO / relative).read_bytes()).hexdigest(),
                   'executed_by_this_script': False}
        for relative in FIXED_TESTS
    }
    results = {
        'scope': 'Synthetic local proofs; no provider/network/production DB',
        'baseline_commit': BASELINE,
        'interpretation': 'dailyplan/keyimport/notify are pre-fix counterexamples; PG uses current source; '
                          'run the named regression tests through tests/zhihui_harness/run_tests.py '
                          'to verify fixed behavior, not through this script',
        'source_versions_and_sha256': sources,
        'fixed_regression_tests': fixed_tests,
        'dailyplan_lost_lease': asyncio.run(dailyplan_lost_lease()),
        'dailyplan_background_lease': asyncio.run(dailyplan_background_lease()),
        'pgsync_immutable_restore': pgsync_immutable_restore(),
        'keyimport_and_notify': keyimport_mode_and_notify_type(),
    }
    encoded = json.dumps(results, ensure_ascii=False, indent=2) + '\n'
    if args.output is not None:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    main()
