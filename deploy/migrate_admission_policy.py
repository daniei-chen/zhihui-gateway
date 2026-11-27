#!/usr/bin/env python3
"""Inspect/upgrade the resource-only admission protocol after all peers stop.

Default operation is read-only. --apply holds BEGIN IMMEDIATE, verifies every
known lifetime owner lock, and upgrades protocol 1 to 2 without touching business
balances, request logs or a writer descriptor. Never remove an active cohort DB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat


class Refused(RuntimeError):
    pass


def _dead_owners(directory: Path, lease_owners: set[str]) -> int:
    if os.name != 'posix':
        raise Refused('owner_lock_verification_requires_posix')
    import fcntl
    if not directory.is_dir() or directory.is_symlink():
        raise Refused('owner_directory_unavailable')
    known = set()
    count = 0
    for path in directory.iterdir():
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)_([0-9a-f]{32})', path.name)
        if not match or match.group(1) != socket.gethostname():
            raise Refused('unknown_owner_cannot_be_proven_dead')
        known.add(match.group(1) + ':' + match.group(2))
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise Refused('unsafe_owner_lock')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Refused('live_owner: drain and stop every old cohort peer first') from None
            finally:
                os.close(fd)
        except OSError:
            raise Refused('owner_lock_unavailable') from None
        count += 1
    if not lease_owners.issubset(known):
        raise Refused('lease_owner_has_no_verifiable_lock')
    return count


def migrate(database: Path, owners: Path, *, apply: bool = False) -> dict:
    database, owners = Path(database), Path(owners)
    if not database.is_file() or database.is_symlink():
        raise Refused('admission_database_unavailable')
    if owners.absolute() != (database.parent / '.budget-owners').absolute():
        raise Refused('owner_directory_must_match_coordinator_parent')
    mode = 'rw' if apply else 'ro'
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=' + mode,
                                 uri=True, timeout=1, isolation_level=None)
    try:
        if apply:
            connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT value FROM admission_policy WHERE id=1').fetchone()
        if row is None:
            raise Refused('admission_policy_missing')
        policy = json.loads(row[0])
        protocol = policy.get('response_budget_protocol')
        if protocol not in (1, 2):
            raise Refused('unsupported_source_protocol')
        lease_rows = connection.execute('SELECT owner FROM admission_leases').fetchall()
        dead = _dead_owners(owners, {value[0] for value in lease_rows})
        target = dict(policy, response_budget_protocol=2)
        encoded = json.dumps(target, sort_keys=True, separators=(',', ':'))
        result = {'source_protocol': protocol, 'target_protocol': 2,
                  'known_dead_owners': dead, 'stale_resource_leases': len(lease_rows),
                  'applied': False, 'business_database_changed': False,
                  'policy_sha256': hashlib.sha256(encoded.encode()).hexdigest()}
        if apply:
            # No peer can reserve while this writer transaction is held. A new
            # old-protocol peer attempting to join after commit fails its own
            # policy comparison; existing live peers were refused above.
            connection.execute('DELETE FROM admission_leases')
            connection.execute('UPDATE admission_policy SET value=? WHERE id=1', (encoded,))
            connection.commit()
            result['applied'] = True
        return result
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--owners', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = migrate(args.database, args.owners or args.database.parent / '.budget-owners',
                         apply=args.apply)
    except (Refused, OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({'ok': False, 'reason': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
