import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

SCHEMA = '''
CREATE TABLE IF NOT EXISTS provider_dispatches_v2(
 dispatch_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS provider_dispatch_request_v2 ON provider_dispatches_v2(request_id);
CREATE TABLE IF NOT EXISTS provider_attempt_events_v2(
 request_id TEXT NOT NULL,dispatch_id TEXT NOT NULL,rotation INTEGER NOT NULL,
 provider_attempt INTEGER NOT NULL,phase TEXT NOT NULL,ts INTEGER NOT NULL,
 account_uid TEXT NOT NULL,model TEXT NOT NULL,realm TEXT NOT NULL,path TEXT NOT NULL,
 status INTEGER NOT NULL,event_sha256 TEXT NOT NULL,
 PRIMARY KEY(request_id,dispatch_id,rotation,provider_attempt,phase)
);
CREATE INDEX IF NOT EXISTS provider_attempt_request_v2 ON provider_attempt_events_v2(request_id);
CREATE TRIGGER IF NOT EXISTS provider_attempt_v2_no_update BEFORE UPDATE ON provider_attempt_events_v2
 BEGIN SELECT RAISE(ABORT,'provider attempt evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS provider_attempt_v2_no_delete BEFORE DELETE ON provider_attempt_events_v2
 BEGIN SELECT RAISE(ABORT,'provider attempt evidence is immutable'); END;
CREATE TABLE IF NOT EXISTS provider_evidence_conflicts_v2(
 event_key TEXT NOT NULL,event_sha256 TEXT NOT NULL,ts INTEGER NOT NULL,
 PRIMARY KEY(event_key,event_sha256)
);
CREATE TRIGGER IF NOT EXISTS provider_conflict_v2_no_update BEFORE UPDATE ON provider_evidence_conflicts_v2
 BEGIN SELECT RAISE(ABORT,'provider evidence conflict is immutable'); END;
CREATE TRIGGER IF NOT EXISTS provider_conflict_v2_no_delete BEFORE DELETE ON provider_evidence_conflicts_v2
 BEGIN SELECT RAISE(ABORT,'provider evidence conflict is immutable'); END;
'''

IDENTITY = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
PHASES = ('sending', 'headers', 'transport_unknown')
FILENAME = re.compile(r'[A-Za-z0-9_.:-]{1,128}-[0-9]{2}-[0-9]{2}-(?:sending|headers|transport_unknown)\.json\Z')
COLLECTION_STALE_SECONDS = 90
_last_collection = {'at': None, 'error': None}


def _directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    information = path.lstat()
    if not stat.S_ISDIR(information.st_mode) or os.name != 'nt' and information.st_mode & 0o022 or path.is_symlink():
        raise ValueError('unsafe provider evidence directory')


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def record(value):
    from . import db
    if not isinstance(value, dict) or value.get('event') != 'workbuddy_provider_attempt_v2':
        return False
    fields = ('request_id', 'dispatch_id', 'rotation', 'provider_attempt', 'phase', 'ts',
              'account_uid', 'model', 'realm', 'path', 'status')
    if set(value) != set(fields) | {'event'}:
        raise ValueError('invalid provider evidence fields')
    if not all(isinstance(value[name], str) and IDENTITY.fullmatch(value[name]) for name in ('request_id', 'dispatch_id')):
        raise ValueError('invalid provider evidence identity')
    if any(type(value[name]) is not int or not 1 <= value[name] <= 64 for name in ('rotation', 'provider_attempt')):
        raise ValueError('invalid provider evidence attempt')
    if (value['phase'] not in PHASES or value['realm'] not in ('cn', 'global')
            or type(value['status']) is not int or not 0 <= value['status'] <= 599
            or type(value['ts']) is not int or not 0 < value['ts'] <= time.time() + 300
            or value['path'] not in ('/v2/chat/completions', '/console/chat/completions')):
        raise ValueError('invalid provider evidence state')
    if not all(isinstance(value[name], str) and 0 < len(value[name]) <= 128
               and not any(ord(character) < 32 for character in value[name]) for name in ('account_uid', 'model')):
        raise ValueError('invalid provider evidence account/model')
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    key = tuple(value[name] for name in ('request_id', 'dispatch_id', 'rotation', 'provider_attempt', 'phase'))
    with db.transaction() as connection:
        prior = connection.execute('SELECT event_sha256 FROM provider_attempt_events_v2 WHERE request_id=? AND dispatch_id=? AND rotation=? AND provider_attempt=? AND phase=?', key).fetchone()
        if prior and prior['event_sha256'] != digest:
            connection.execute('INSERT OR IGNORE INTO provider_evidence_conflicts_v2 VALUES(?,?,?)',
                               (json.dumps(key), digest, int(time.time())))
            return False
        connection.execute('INSERT OR IGNORE INTO provider_attempt_events_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           tuple(value[name] for name in fields) + (digest,))
    return True


def _collect(limit):
    from . import config
    root = config.UPSTREAM_DIR / 'data/provider-events-v2'
    if not root.exists():
        return 0
    _directory(root)
    imported = 0
    with os.scandir(root) as entries:
        for entry in entries:
            if imported >= limit:
                break
            if not entry.name.endswith('.json'):
                continue
            if not FILENAME.fullmatch(entry.name):
                raise ValueError('invalid provider evidence filename')
            path = Path(entry.path)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as source:
                information = os.fstat(source.fileno())
                if not stat.S_ISREG(information.st_mode) or information.st_size > 8192 or not 1 <= information.st_nlink <= 2:
                    raise ValueError('unsafe provider evidence file')
                raw = source.read(8193)
            if not record(json.loads(raw)):
                raise ValueError('provider evidence conflict; original retained')
            processed = root / 'processed'
            _directory(processed, create=True)
            destination = processed / time.strftime('%Y%m%d')
            _directory(destination, create=True)
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (information.st_dev, information.st_ino, information.st_size, information.st_mtime_ns):
                raise ValueError('provider evidence changed during collection')
            target = destination / path.name
            if target.exists() or target.is_symlink():
                if target.is_symlink() or target.read_bytes() != raw:
                    raise ValueError('processed provider evidence conflict')
                path.unlink()
            else:
                os.rename(path, target)
            _sync_directory(destination)
            _sync_directory(processed)
            _sync_directory(root)
            imported += 1
    return imported


def collect(limit=1000):
    try:
        result = _collect(max(1, min(int(limit), 1000)))
    except Exception:
        _last_collection.update(at=int(time.time()), error='provider evidence collection failed')
        raise
    _last_collection.update(at=int(time.time()), error=None)
    return result


def health():
    from . import config, db
    pending, oldest = 0, None
    try:
        root = config.UPSTREAM_DIR / 'data/provider-events-v2'
        if root.exists():
            _directory(root)
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.name.endswith('.json'):
                        continue
                    pending += 1
                    stamp = entry.stat(follow_symlinks=False).st_mtime
                    oldest = stamp if oldest is None else min(oldest, stamp)
                    if pending >= 1001:
                        break
        conflicts = db.query_one('SELECT COUNT(*) count FROM provider_evidence_conflicts_v2')['count']
        age = max(0, int(time.time() - oldest)) if oldest is not None else 0
        sampled_at = _last_collection.get('at')
        collection_age = (max(0, int(time.time() - sampled_at))
                          if isinstance(sampled_at, int) else None)
        fresh = (collection_age is not None
                 and collection_age <= COLLECTION_STALE_SECONDS)
        return {'ok': (fresh and not _last_collection['error']
                       and not conflicts and age < 90 and pending <= 1000),
                'fresh': fresh,
                'collection_age_seconds': collection_age,
                'stale_after_seconds': COLLECTION_STALE_SECONDS,
                'pending': pending, 'pending_count_capped': pending > 1000, 'oldest_seconds': age,
                'conflicts': conflicts, 'collection': dict(_last_collection)}
    except Exception:
        return {'ok': False, 'fresh': False, 'pending': None, 'conflicts': None,
                'collection_age_seconds': None,
                'stale_after_seconds': COLLECTION_STALE_SECONDS,
                'collection': dict(_last_collection)}


def report(request_id):
    from . import db
    rows = db.query('SELECT * FROM provider_attempt_events_v2 WHERE request_id=? ORDER BY ts,dispatch_id,rotation,provider_attempt,phase LIMIT 501', (request_id,))
    count = db.query_one('SELECT COUNT(*) count FROM (SELECT dispatch_id,rotation,provider_attempt FROM provider_attempt_events_v2 WHERE request_id=? GROUP BY dispatch_id,rotation,provider_attempt)', (request_id,))['count']
    dispatches = db.query_one('SELECT COUNT(*) count FROM provider_dispatches_v2 WHERE request_id=?', (request_id,))['count']
    legacy = db.query_one('SELECT COUNT(*) count FROM attempt_events WHERE request_id=?', (request_id,))['count']
    return {'request_id': request_id, 'attempt_count': None if dispatches and not count else int(count),
            'events': [dict(row) for row in rows[:500]], 'events_truncated': len(rows) > 500,
            'dispatch_count': dispatches,
            'attempt_source': 'provider_attempt_events_v2',
            'legacy_attempt_events': int(legacy),
            'legacy_source_note': ('attempt_events is read-only history after the v2 cutover; '
                                   'do not add its count to v2 attempt_count'),
            'per_attempt_billing': 'not_derived', 'complete': False,
            'note': 'durable pre-send/headers evidence, not supplier billing or cancellation proof'}
