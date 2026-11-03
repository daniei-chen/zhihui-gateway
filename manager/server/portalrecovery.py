"""Offline-only paired-ledger/credential snapshots and quarantine restore.

No production defaults, extraction, network calls, deployment, or in-place
restore. Operators must stop ALL Manager/engine writers first. OS worker locks,
SQLite write locks and repeat file fingerprints add refusal checks; they cannot
prove an unrelated engine/process has stopped. Snapshot directories contain
secrets: keep them on protected storage, not Git or a public export.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

FORMAT = 'portal-offline-snapshot-v1'
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_FILES = 20000
FIXED = {'manager.db', 'user-portal.sqlite3', 'users.json'}
PAIR_FORMAT = 'portal-paired-online-v2'


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('拒绝符号链接、重解析点或非普通文件')
    if info.st_size > MAX_FILE:
        raise ValueError('文件超过恢复工具大小上限')
    return info


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('拒绝符号链接、重解析目录或非目录')


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('必须使用明确的绝对路径，不接受父目录跳转')
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            if ancestor.is_symlink() or getattr(ancestor.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('路径中存在符号链接或重解析点')
    return path


def _digest(path):
    _regular(path)
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _write(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _allowed(name):
    if name in FIXED or name in ('.spool-used', 'spool/.spool-used', 'spool/dead/dispositions.jsonl'):
        return True
    if re.fullmatch(r'auths/workbuddy[0-9A-Za-z_-]{0,80}\.json(?:\.disabled)?', name):
        return True
    if re.fullmatch(r'user-portal-auths/[a-f0-9]{32}\.json', name):
        return True
    return bool(re.fullmatch(r'spool/(?:[A-Za-z0-9_-]{1,128}\.json|dead/(?:processed/)?[A-Za-z0-9_.-]{1,180}\.json)', name)) and '..' not in name


def _inventory(root):
    _directory(root)
    found = {}
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            _directory(Path(current) / name)
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            info = _regular(path)
            total += info.st_size
            if total > MAX_TOTAL or len(found) >= MAX_FILES:
                raise ValueError('快照文件数量或总体积超出上限')
            found[relative] = {'sha256': _digest(path), 'size': info.st_size}
    return found


def _sqlite(path, *, writable=False):
    _regular(path)
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def _check_database(path):
    with closing(_sqlite(path)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库完整性检查未通过')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('数据库外键检查未通过')


def _check_pair(root):
    with closing(_sqlite(root / 'manager.db')) as main, closing(_sqlite(root / 'user-portal.sqlite3')) as portal:
        for link in portal.execute('SELECT key_id,key_hash FROM portal_key_links'):
            key = main.execute('SELECT key_hash FROM api_keys WHERE id=?', (link['key_id'],)).fetchone()
            if not key or key[0] != link['key_hash']:
                raise ValueError('双库密钥归属不匹配，停止恢复并人工核对')
        for account in portal.execute('SELECT a.id,a.provider_uid,p.filename FROM portal_accounts a LEFT JOIN portal_pool_links p ON p.account_id=a.id WHERE p.account_id IS NULL'):
            name = 'user-portal-auths/' + account['id'] + '.json'
            if not _allowed(name):
                raise ValueError('独立账号凭据文件名无效')
            path = root / name; _regular(path)
            raw = json.loads(path.read_text(encoding='utf-8'))
            if str(raw.get('uid')) != account['provider_uid']:
                raise ValueError('独立账号凭据与账号归属不一致')
        for link in portal.execute('SELECT p.filename,a.provider_uid FROM portal_pool_links p JOIN portal_accounts a ON a.id=p.account_id'):
            name = 'auths/' + link['filename']
            if not _allowed(name):
                raise ValueError('账号映射文件名无效')
            path = root / name
            _regular(path)
            raw = json.loads(path.read_text(encoding='utf-8'))
            if str((raw.get('account') or {}).get('uid')) != link['provider_uid']:
                raise ValueError('账号归属与公共池凭据不一致')
        allocations = """SELECT a.request_id,g.owner_id,a.amount_units,g.id,'account' AS kind FROM portal_allocations a
            JOIN portal_grants g ON g.id=a.grant_id UNION ALL
            SELECT a.request_id,g.owner_id,a.amount_units,g.id,'bonus' AS kind FROM portal_bonus_allocations a
            JOIN portal_bonus_grants g ON g.id=a.grant_id"""
        for spend in portal.execute('SELECT request_id,owner_id,held_units FROM portal_spends'):
            rows = portal.execute('SELECT owner_id,amount_units FROM (' + allocations + ') WHERE request_id=?', (spend['request_id'],)).fetchall()
            if sum(row['amount_units'] for row in rows) != spend['held_units'] or any(row['owner_id'] != spend['owner_id'] for row in rows):
                raise ValueError('冻结金额或用户批次归属不一致')
        for table, alloc in (('portal_grants', 'portal_allocations'), ('portal_bonus_grants', 'portal_bonus_allocations')):
            bad = portal.execute(f"SELECT 1 FROM {table} g WHERE remaining_units + COALESCE((SELECT SUM(a.amount_units) FROM {alloc} a "
                "JOIN portal_spends s ON s.request_id=a.request_id WHERE a.grant_id=g.id AND s.state IN ('reserved','forwarded','unknown')),0) > amount_units LIMIT 1").fetchone()
            if bad:
                raise ValueError('可用积分与冻结重复计数，拒绝恢复')
        settlements = {row['request_id']: row for row in portal.execute('SELECT * FROM portal_settlements')}
        receipts = main.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='portal_spend_receipts'").fetchone()
        if receipts:
            for receipt in main.execute('SELECT request_id,owner_id,payload FROM portal_spend_receipts'):
                settlement = settlements.get(receipt['request_id'])
                amount = json.loads(receipt['payload']).get('amount_units')
                if settlement and (settlement['owner_id'] != receipt['owner_id']
                                   or amount is not None and amount != settlement['supplier_units']):
                    raise ValueError('双账本费用回执与结算不一致，禁止恢复放行')
        for settlement in settlements.values():
            receipt = main.execute('SELECT owner_id,payload FROM portal_spend_receipts WHERE request_id=?', (settlement['request_id'],)).fetchone() if receipts else None
            resolution = portal.execute('SELECT amount_units FROM portal_spend_resolutions WHERE request_id=?', (settlement['request_id'],)).fetchone()
            if not receipt and not resolution:
                raise ValueError('钱包已结算但主账本缺少费用证据，禁止恢复放行')


def _copy_database(source, destination, *, verify=True):
    # Use a separate reader: backup() on a BEGIN IMMEDIATE connection can wait
    # forever. The caller's write lock prevents new commits while this copies.
    with closing(_sqlite(source)) as reader:
        with closing(sqlite3.connect(destination)) as target:
            reader.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
    os.chmod(destination, 0o600)
    if verify:
        _check_database(destination)
    with destination.open('r+b') as handle:
        os.fsync(handle.fileno())


def _no_live_workers(main_db):
    from . import budgetowner
    owner_dir = main_db.parent / '.budget-owners'
    if not owner_dir.exists():
        return
    _directory(owner_dir)
    for index, path in enumerate(owner_dir.iterdir()):
        if index >= MAX_FILES:
            raise ValueError('worker记录数量异常')
        _regular(path)
        if '_' not in path.name:
            raise ValueError('worker归属记录无效，未证明离线')
        host, identifier = path.name.rsplit('_', 1)
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('worker归属记录无效，未证明离线')
        # alive() uses configured DB_PATH. Probe this explicit source directory
        # directly instead, never rebind or initialize application configuration.
        import socket
        if host != socket.gethostname():
            raise ValueError('远端worker不能在本机证明已退出')
        with path.open('r+b') as handle:
            try:
                budgetowner.lock(handle)
            except OSError as exc:
                raise ValueError('仍存在存活或无法核实的worker，拒绝离线快照') from exc


def _source_files(users, auths, isolated, spool):
    sources = {'users.json': users}
    total = _regular(users).st_size
    for base, prefix in ((auths, 'auths'), (isolated, 'user-portal-auths'), (spool, 'spool')):
        _directory(base)
        for current, dirs, files in os.walk(base, followlinks=False):
            for name in dirs:
                _directory(Path(current) / name)
            for name in files:
                path = Path(current) / name
                relative = prefix + '/' + path.relative_to(base).as_posix()
                if not _allowed(relative):
                    raise ValueError('目录包含不支持或正在写入的文件，未创建一致性快照')
                total += _regular(path).st_size
                if total > MAX_TOTAL or len(sources) >= MAX_FILES:
                    raise ValueError('源文件数量或总体积超出快照上限')
                sources[relative] = path
    return sources


def create_snapshot(*, main_db, portal_db, users, auths, isolated, spool, destination, offline_confirmed=False):
    if offline_confirmed is not True:
        raise ValueError('必须先停止全部Manager与引擎写者并明确确认离线')
    paths = [_absolute(p) for p in (main_db, portal_db, users, auths, isolated, spool, destination)]
    main_db, portal_db, users, auths, isolated, spool, destination = paths
    if destination.exists():
        raise ValueError('快照目标必须是不存在的新目录，拒绝覆盖')
    if any(destination == p or destination.is_relative_to(p) or p.is_relative_to(destination) for p in paths[:-1]):
        raise ValueError('快照目标与源目录不能重叠')
    _no_live_workers(main_db)
    sources = _source_files(users, auths, isolated, spool)
    used_marker = portal_db.parent / '.spool-used'
    if used_marker.exists() or used_marker.is_symlink():
        sources['.spool-used'] = used_marker
    before = {name: _digest(path) for name, path in sources.items()}
    with ExitStack() as stack:
        for path in (main_db, portal_db):
            conn = stack.enter_context(closing(_sqlite(path, writable=True)))
            conn.execute('BEGIN IMMEDIATE')
        _no_live_workers(main_db)
        destination.mkdir(mode=0o700)
        _copy_database(main_db, destination / 'manager.db')
        _copy_database(portal_db, destination / 'user-portal.sqlite3')
        for name, path in sources.items():
            _write(destination / name, path.read_bytes())
        after_sources = _source_files(users, auths, isolated, spool)
        if used_marker.exists() or used_marker.is_symlink():
            after_sources['.spool-used'] = used_marker
        after = {name: _digest(path) for name, path in after_sources.items()}
        if before != after:
            raise ValueError('凭据/回执/身份在复制中变化，快照未完成，不可恢复')
        files = _inventory(destination)
        if any(files[name]['sha256'] != digest for name, digest in before.items()):
            raise ValueError('复制文件摘要不一致，快照未完成')
        _check_pair(destination)
        manifest = {'format': FORMAT, 'snapshot_id': secrets.token_hex(16), 'created_at': int(time.time()),
            'offline_operator_confirmed': True, 'files': files}
        _write(destination / 'manifest.json', (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode())
    return {'snapshot_id': manifest['snapshot_id'], 'file_count': len(files), 'complete': True,
        'manifest_sha256': _digest(destination / 'manifest.json')}


def create_paired_snapshot(*, main_db, portal_db, users, auths, isolated, spool, destination):
    paths = [_absolute(Path(path)) for path in (main_db, portal_db, users, auths, isolated, spool, destination)]
    main_db, portal_db, users, auths, isolated, spool, destination = paths
    if destination.exists() or any(destination == path or destination.is_relative_to(path) or path.is_relative_to(destination) for path in paths[:-1]):
        raise ValueError('配对快照必须使用不重叠的新目录')
    for directory in (auths, isolated, spool):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    before_sources = _source_files(users, auths, isolated, spool)
    used_marker = portal_db.parent / '.spool-used'
    if used_marker.exists() or used_marker.is_symlink():
        before_sources['.spool-used'] = used_marker
    before = {name: _digest(path) for name, path in before_sources.items()}
    started = time.monotonic()
    with ExitStack() as stack:
        for path in (main_db, portal_db):
            connection = stack.enter_context(closing(_sqlite(path, writable=True)))
            connection.execute('PRAGMA busy_timeout=1000')
            connection.execute('BEGIN IMMEDIATE')
        destination.mkdir(mode=0o700)
        _copy_database(main_db, destination / 'manager.db', verify=False)
        _copy_database(portal_db, destination / 'user-portal.sqlite3', verify=False)
        for name, path in before_sources.items():
            _write(destination / name, path.read_bytes())
        after_sources = _source_files(users, auths, isolated, spool)
        if used_marker.exists() or used_marker.is_symlink():
            after_sources['.spool-used'] = used_marker
        after = {name: _digest(path) for name, path in after_sources.items()}
        if before != after:
            raise ValueError('配对快照期间凭据或回执变化，快照未完成')
    locked_seconds = round(time.monotonic() - started, 3)
    for name in ('manager.db', 'user-portal.sqlite3'):
        _check_database(destination / name)
    _check_pair(destination)
    files = _inventory(destination)
    if any(files[name]['sha256'] != digest for name, digest in before.items()):
        raise ValueError('复制文件摘要不一致，配对快照未完成')
    manifest = {'format': FORMAT, 'snapshot_id': secrets.token_hex(16), 'created_at': int(time.time()),
                'offline_operator_confirmed': False, 'consistency': PAIR_FORMAT,
                'activation_quarantined': True, 'locked_seconds': locked_seconds, 'files': files}
    _write(destination / 'manifest.json', (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode())
    return {'snapshot_id': manifest['snapshot_id'], 'file_count': len(files), 'complete': True,
            'consistency': PAIR_FORMAT, 'activation_quarantined': True,
            'locked_seconds': locked_seconds,
            'manifest_sha256': _digest(destination / 'manifest.json')}


def verify_snapshot(source, manifest_sha256):
    source = _absolute(source)
    _directory(source)
    _regular(source / 'manifest.json')
    if ((source / 'manifest.json').stat().st_size > 4 * 1024 * 1024
            or not isinstance(manifest_sha256, str) or not re.fullmatch('[a-f0-9]{64}', manifest_sha256)
            or _digest(source / 'manifest.json') != manifest_sha256):
        raise ValueError('清单摘要与独立保存的快照回执不匹配，拒绝恢复')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    paired = manifest.get('consistency') == PAIR_FORMAT and manifest.get('activation_quarantined') is True
    if manifest.get('format') != FORMAT or not (manifest.get('offline_operator_confirmed') is True or paired):
        raise ValueError('快照格式或离线声明无效')
    files = manifest.get('files')
    if not isinstance(files, dict) or not FIXED.issubset(files) or not files or any(not _allowed(name) for name in files):
        raise ValueError('快照文件清单无效或缺少双库/管理身份')
    actual = _inventory(source)
    actual.pop('manifest.json', None)
    if actual != files:
        raise ValueError('快照文件缺失、多出或摘要不匹配，拒绝恢复')
    for name in ('manager.db', 'user-portal.sqlite3'):
        _check_database(source / name)
    _check_pair(source)
    return manifest


def restore_quarantine(source, destination, manifest_sha256):
    source, destination = _absolute(source), _absolute(destination)
    manifest = verify_snapshot(source, manifest_sha256)
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('只允许恢复到互不重叠、尚不存在的全新隔离目录')
    destination.mkdir(mode=0o700)
    for name in manifest['files']:
        _write(destination / name, (source / name).read_bytes())
    # Verify byte copies before any recovery-state modifications.
    if _inventory(destination) != manifest['files']:
        raise ValueError('恢复副本校验失败，隔离目录不可启用')
    portal_path = destination / 'user-portal.sqlite3'
    from . import portalstore
    with closing(sqlite3.connect(portal_path)) as conn, conn:
        conn.executescript(portalstore.SCHEMA)
        # Old log/worker evidence must not authorize a stale snapshot's balance.
        conn.execute('DELETE FROM portal_request_ends')
        conn.execute("UPDATE portal_spends SET state='unknown' WHERE state IN ('reserved','forwarded')")
        conn.execute("INSERT OR REPLACE INTO portal_settings VALUES('restore_quarantine','true')")
        conn.execute("INSERT OR REPLACE INTO portal_settings VALUES('automatic_checkin','false')")
        conn.execute("DELETE FROM portal_settings WHERE key='billing_policy'")
        conn.execute('UPDATE portal_users SET sv=sv+1')
        conn.execute("DELETE FROM portal_settings WHERE key='session_secret'")
        conn.execute("UPDATE portal_auth_sessions SET state='cancelled' WHERE state IN ('waiting','processing')")
    with closing(sqlite3.connect(destination / 'manager.db')) as conn, conn:
        conn.execute('UPDATE api_keys SET enabled=0')
        conn.execute('UPDATE api_tokens SET enabled=0')
    users_path = destination / 'users.json'
    users = json.loads(users_path.read_text(encoding='utf-8'))
    users['secret'] = secrets.token_hex(48)
    for user in users.get('users', []):
        user['sv'] = int(user.get('sv', 0)) + 1
    # Only writes our newly-created quarantine file, never an existing target.
    with users_path.open('w', encoding='utf-8') as handle:
        json.dump(users, handle, ensure_ascii=True)
        handle.flush(); os.fsync(handle.fileno())
    for directory in ('auths', 'user-portal-auths', 'spool'):
        (destination / directory).mkdir(mode=0o700, exist_ok=True)
    # Do not leave engine-watched JSONs in their usual directory. Paths are
    # verified children of this newly-created destination, never computed from
    # manifest names or moved across shells.
    sealed = destination / 'quarantined-credentials'
    sealed.mkdir(mode=0o700)
    for directory in ('auths', 'user-portal-auths'):
        source_dir, sealed_dir = destination / directory, sealed / directory
        if not source_dir.resolve().is_relative_to(destination.resolve()) or not sealed_dir.resolve().is_relative_to(destination.resolve()):
            raise ValueError('隔离凭据路径异常，停止恢复')
        source_dir.rename(sealed_dir)
        source_dir.mkdir(mode=0o700)
    _check_database(portal_path)
    _write(destination / 'RESTORED-QUARANTINE.json', json.dumps({'format': FORMAT, 'snapshot_id': manifest['snapshot_id'],
        'calls_disabled': True, 'automatic_checkin_disabled': True,
        'credentials_sealed': True,
        'warning': 'Stale backup may omit later spending. Never use as live balance without external reconciliation.'}, indent=2).encode())
    return {'snapshot_id': manifest['snapshot_id'], 'quarantined': True, 'calls_enabled': False}


def main():
    parser = argparse.ArgumentParser(description='Offline paired-ledger snapshot; NEVER restores over existing data')
    commands = parser.add_subparsers(dest='operation', required=True)
    create = commands.add_parser('snapshot')
    for name in ('main-db', 'portal-db', 'users', 'auths', 'isolated', 'spool', 'destination'):
        create.add_argument('--' + name, required=True)
    create.add_argument('--offline-confirmed', action='store_true')
    verify = commands.add_parser('verify'); verify.add_argument('--source', required=True)
    verify.add_argument('--manifest-sha256', required=True)
    restore = commands.add_parser('restore-quarantine')
    restore.add_argument('--source', required=True); restore.add_argument('--destination', required=True)
    restore.add_argument('--manifest-sha256', required=True)
    args = vars(parser.parse_args()); operation = args.pop('operation')
    if operation == 'snapshot':
        result = create_snapshot(**args)
    elif operation == 'verify':
        manifest = verify_snapshot(args['source'], args['manifest_sha256'])
        result = {'snapshot_id': manifest['snapshot_id'], 'verified': True}
    else:
        result = restore_quarantine(**args)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
