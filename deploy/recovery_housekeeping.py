#!/usr/bin/python3
"""Bound local snapshot growth without deleting a current or unverified backup.

Shares the producer lock. Default is read-only; timer use is explicitly --apply.
Runtime blobs remain untouched because legacy snapshots do not record all refs.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

BASE = Path('/root/backups/zhihui-v2')
STAMP = re.compile(r'[0-9]{8}-[0-9]{6}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
LOCK_WAIT_SECONDS = 180
LOCK_RETRY_SECONDS = 5


def acquire_backup_lock(fd):
    """Wait briefly for the producer, then fail closed if it never releases."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    attempts = 0
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            if attempts == 1:
                print(json.dumps({
                    'lock': 'backup.lock',
                    'action': 'wait',
                    'retry_seconds': LOCK_RETRY_SECONDS,
                    'max_wait_seconds': LOCK_WAIT_SECONDS,
                }, sort_keys=True))
            time.sleep(min(LOCK_RETRY_SECONDS, remaining))


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('private root-owned regular file required')


def directory(path, parent=None):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
            or (parent is not None and path.resolve().parent != parent.resolve())):
        raise ValueError('private contained root-owned directory required')


def read_json(path):
    regular(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('metadata too large')
    return json.loads(path.read_text())


def check_cipher(path, metadata):
    digest = hashlib.sha256()
    count = 0
    parts = metadata.get('parts')
    if not isinstance(parts, list) or not parts:
        raise ValueError('ciphertext part inventory missing')
    for i, part in enumerate(parts):
        name = 'snapshot.part-%03d.age' % i
        if (part.get('name') != name or not SHA.fullmatch(str(part.get('sha256', '')))
                or not isinstance(part.get('bytes'), int) or isinstance(part.get('bytes'), bool)
                or part['bytes'] <= 0):
            raise ValueError('invalid ciphertext part metadata')
        source = path / name
        regular(source)
        local = hashlib.sha256()
        with source.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                local.update(block)
                digest.update(block)
                count += len(block)
        if source.stat().st_size != part['bytes'] or local.hexdigest() != part['sha256']:
            raise ValueError('ciphertext part mismatch')
    if count != metadata.get('cipher_bytes') or digest.hexdigest() != metadata.get('cipher_sha256'):
        raise ValueError('joined ciphertext mismatch')


def build_plan(base=BASE, keep=3, days=7, now=None):
    now = time.time() if now is None else now
    base = Path(base)
    directory(base)
    snapshots = base / 'snapshots'
    directory(snapshots, base)
    latest = read_json(base / 'latest.json')
    current = Path(latest['snapshot']['directory'])
    if current.parent != snapshots or not STAMP.fullmatch(current.name):
        raise ValueError('current snapshot outside approved namespace')
    directory(current, snapshots)
    proof = latest.get('local_decryption_checks', {}).get('snapshot', {})
    if (not proof.get('cipher_sha256')
            or proof['cipher_sha256'] != latest['snapshot'].get('cipher_sha256')):
        raise ValueError('latest snapshot has no exact local decryption proof')
    check_cipher(current, latest['snapshot'])
    candidates = []
    for path in sorted(snapshots.iterdir()):
        if not STAMP.fullmatch(path.name):
            continue
        directory(path, snapshots)
        # Refuse a symlink, socket or special file anywhere in a deletable tree.
        for entry in path.rglob('*'):
            info = entry.lstat()
            if (info.st_uid != 0 or stat.S_ISLNK(info.st_mode)
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise ValueError('unsafe snapshot descendant')
        meta = read_json(path / 'snapshot.json')
        if not isinstance(meta.get('restore_check'), dict):
            raise ValueError('snapshot lacks restore validation')
        check_cipher(path, meta)
        candidates.append(path)
    newest = set(candidates[-keep:])
    rows = []
    for path in candidates:
        stamp = datetime.datetime.strptime(path.name, '%Y%m%d-%H%M%S').timestamp()
        if path == current:
            reason = 'current_verified_recovery_point'
        elif path in newest:
            reason = 'newest_%d' % keep
        elif stamp >= now - days * 86400:
            reason = 'within_%d_days' % days
        else:
            reason = 'expired_verified_snapshot'
        rows.append({'name': path.name, 'action': 'prune' if reason.startswith('expired') else 'keep',
                     'reason': reason})
    return {'current': current.name, 'snapshots': rows,
            'runtime_blobs_pruned': False, 'invalid_or_unsafe_snapshot': 'fail_closed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('root required')
    directory(BASE)
    fd = os.open(BASE / 'backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        acquire_backup_lock(fd)
        plan = build_plan(BASE)
        if args.apply:
            for row in plan['snapshots']:
                if row['action'] != 'prune':
                    continue
                target = BASE / 'snapshots' / row['name']
                directory(target, BASE / 'snapshots')
                shutil.rmtree(target)
        print(json.dumps(dict(plan, applied=args.apply), sort_keys=True))
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
