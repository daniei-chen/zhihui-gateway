"""External writer-state guard. No application imports, CLI, or implicit setup.

The installed caller must supply its trusted release verifier and lifecycle lock
verifier. The latter must establish quiescence, not merely possess a lockfile.
This module checks signed bytes and real state, but a signed capability is still
a claim whose implementation must be covered by the release gate. No service is
started here. A persisted floor must never be removed to recover an older writer.
"""

from dataclasses import dataclass, field
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Protocol


DESCRIPTOR = 'server/writer-capabilities.json'
ADD_ONLY = 'account-add-only-default-deny'
_INITIALIZED = b'{"schema":"zhihui.writer-initialized","version":1}\n'
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z')


class Rejected(RuntimeError):
    """Messages identify a contract, never user rows, credentials, or SQL data."""


class ReleaseVerifier(Protocol):
    def verify_release(self, root: Path) -> dict: ...


class LockVerifier(Protocol):
    def validate(self, permit: object, *, exclusive: bool) -> None:
        """Raise unless the runtime lease is live; exclusive means no writers."""
        ...


@dataclass(frozen=True)
class Capabilities:
    role_policy: int = 0
    key_daily_usage: int = 0

    def __post_init__(self):
        if (type(self.role_policy) is not int or self.role_policy not in (0, 2)
                or type(self.key_daily_usage) is not int or self.key_daily_usage not in (0, 1)):
            raise Rejected('unsupported writer capabilities')

    def join(self, other):
        if type(other) is not Capabilities:
            raise Rejected('invalid writer capabilities')
        return Capabilities(max(self.role_policy, other.role_policy),
                            max(self.key_daily_usage, other.key_daily_usage))

    def covers(self, other):
        return (type(other) is Capabilities and self.role_policy >= other.role_policy
                and self.key_daily_usage >= other.key_daily_usage)


@dataclass(frozen=True)
class Paths:
    root: Path
    root_uid: int
    application_uid: int
    synthetic: bool = False

    def __post_init__(self):
        root = Path(self.root)
        if (not root.is_absolute() or '..' in root.parts or root != root.absolute()
                or type(self.root_uid) is not int or self.root_uid < 0
                or type(self.application_uid) is not int or self.application_uid < 0):
            raise Rejected('invalid fixed paths')
        if not self.synthetic and (root != Path('/') or self.root_uid != 0 or self.application_uid != 10001):
            raise Rejected('production paths are fixed')

    @classmethod
    def production(cls):
        if os.name != 'posix':
            raise Rejected('production guard requires POSIX')
        return cls(Path('/'), 0, 10001)

    @classmethod
    def synthetic_tree(cls, root):
        root = Path(root).absolute()
        if not root.name.startswith('writer-guard-test-'):
            raise Rejected('explicit synthetic test root required')
        uid = os.getuid() if hasattr(os, 'getuid') else 0
        return cls(root, uid, uid, True)

    @property
    def users(self):
        return self.root / 'opt/workbuddy-manager/data/users.json'

    @property
    def database(self):
        return self.root / 'opt/workbuddy-manager/data/manager.db'

    @property
    def floor(self):
        return self.root / 'opt/zhihui-release/writer-state/floor.json'

    @property
    def releases(self):
        return self.root / 'opt/workbuddy-manager/releases'

    @property
    def initialized(self):
        return self.floor.with_name('initialized.json')


@dataclass(frozen=True)
class VerifiedRelease:
    root: Path
    manifest_sha256: str
    release_id: str
    capabilities: Capabilities
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class ObservedState:
    capabilities: Capabilities
    users_sha256: str
    database_identity: tuple
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class FloorRecord:
    epoch: int
    capabilities: Capabilities
    transaction_id: str
    sha256: str
    _issuer: object = field(repr=False, compare=False)


def _json(raw, contract):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise Rejected('duplicate JSON key in ' + contract)
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise Rejected('invalid JSON in ' + contract) from None


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _identity(info):
    # Windows 3.12 stat/fstat disagree on ctime after overwrite; birthtime is
    # consistent there. The production POSIX check keeps change-time protection.
    change = info.st_birthtime_ns if os.name == 'nt' else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change)


class Guard:
    def __init__(self, paths: Paths, verifier: ReleaseVerifier, lock: LockVerifier):
        if type(paths) is not Paths or not callable(getattr(verifier, 'verify_release', None)) or not callable(getattr(lock, 'validate', None)):
            raise Rejected('trusted guard dependencies required')
        self.paths, self.verifier, self.lock = paths, verifier, lock
        self._issuer = object()

    def _locked(self, permit, *, exclusive=False):
        if self.lock.validate(permit, exclusive=exclusive) is not None:
            raise Rejected('lock verifier must raise on rejection, not return a flag')

    def _path(self, path, *, application=False):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(self.paths.root):
            raise Rejected('path escapes fixed root')
        data = self.paths.users.parent
        for current in (path, *path.parents):
            if self.paths.synthetic and not current.is_relative_to(self.paths.root):
                break
            try:
                info = current.lstat()
            except OSError:
                raise Rejected('required protected path unavailable') from None
            uid = self.paths.application_uid if application and (current == data or current.parent == data) else self.paths.root_uid
            permissions_unsafe = info.st_mode & 0o022 if os.name == 'posix' else False
            if (stat.S_ISLNK(info.st_mode) or info.st_uid != uid or permissions_unsafe
                    or (current != path and not stat.S_ISDIR(info.st_mode))):
                raise Rejected('unsafe protected path')
        return path

    def _file(self, path, *, application=False, limit=None):
        path = self._path(path, application=application)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        try:
            fd = os.open(path, flags)
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                uid = self.paths.application_uid if application else self.paths.root_uid
                permissions_unsafe = info.st_mode & 0o022 if os.name == 'posix' else False
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid
                        or permissions_unsafe or _identity(info) != _identity(path.lstat())):
                    raise Rejected('unsafe protected file')
                if limit is None:
                    return _identity(info)
                if info.st_size > limit:
                    raise Rejected('protected file exceeds contract size')
                raw = handle.read(limit + 1)
                if (len(raw) > limit or _identity(info) != _identity(os.fstat(handle.fileno()))
                        or _identity(info) != _identity(path.lstat())):
                    raise Rejected('protected file changed during read')
                return raw
        except OSError:
            raise Rejected('protected file unavailable') from None

    def inspect_release(self, root):
        root = Path(root).absolute()
        if root.parent != self.paths.releases or not _LABEL.fullmatch(root.name):
            raise Rejected('release must be an exact fixed release directory')
        self._path(root)
        path = root / 'deploy/managed-release.json'
        before = self._file(path, limit=8 * 1024 * 1024)
        try:
            manifest = self.verifier.verify_release(root)
        except Exception:
            raise Rejected('external release verification failed') from None
        after = self._file(path, limit=8 * 1024 * 1024)
        if (before != after or type(manifest) is not dict or manifest != _json(after, 'manifest')
                or manifest.get('signature_required') is not True
                or manifest.get('signature_algorithm') != 'ed25519'
                or manifest.get('version') != root.name or type(manifest.get('files')) is not dict):
            raise Rejected('verified release identity mismatch')
        descriptor = root / DESCRIPTOR
        entry = manifest['files'].get(DESCRIPTOR)
        if entry is None:
            if descriptor.exists() or descriptor.is_symlink():
                raise Rejected('unsigned writer descriptor')
            capabilities = Capabilities()
        else:
            raw = self._file(descriptor, limit=16384)
            if (type(entry) is not dict or not _HEX.fullmatch(str(entry.get('candidate_sha256', '')))
                    or _digest(raw) != entry['candidate_sha256']
                    or type(entry.get('mode')) is not int
                    or stat.S_IMODE(descriptor.stat().st_mode) != entry['mode']):
                raise Rejected('writer descriptor is not bound to signed bytes')
            value = _json(raw, 'writer descriptor')
            required = {'schema', 'version', 'release_id', 'role_policy', 'key_daily_usage', 'account_admin_routes'}
            if (type(value) is not dict or set(value) != required
                    or value['schema'] != 'zhihui.writer-release' or type(value['version']) is not int
                    or value['version'] != 1 or value['release_id'] != root.name):
                raise Rejected('unsupported writer descriptor')
            capabilities = Capabilities(value['role_policy'], value['key_daily_usage'])
            if value['account_admin_routes'] != (ADD_ONLY if capabilities.role_policy == 2 else None):
                raise Rejected('writer authorization semantics mismatch')
        return VerifiedRelease(root, _digest(after), root.name, capabilities, self._issuer)

    def _database_snapshot(self):
        path = self.paths.database
        identities = []
        for file in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
            if file == path or file.exists() or file.is_symlink():
                identity = self._file(file, application=True)
                # Read-only SQLite updates SHM read marks and may fchmod WAL
                # (ctime only). Check permissions on every read; compare inode,
                # size and DB/WAL mtime for concurrent business writes.
                identities.append(identity[:3] if file.name.endswith('-shm') else identity[:4])
            else:
                identities.append(None)
        if (identities[1] is None) != (identities[2] is None):
            raise Rejected('incomplete SQLite WAL sidecar pair')
        return tuple(identities)

    @staticmethod
    def _table(conn, name):
        row = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None:
            return None
        if row[0] != 'table':
            raise Rejected('required database object is not a table')
        return {row[1]: row for row in conn.execute('PRAGMA table_info("' + name + '")')}

    def _daily_state(self):
        try:
            with closing(sqlite3.connect(self.paths.database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('PRAGMA trusted_schema=OFF')
                conn.execute('BEGIN')
                settings = self._table(conn, 'settings')
                keys = self._table(conn, 'api_keys')
                if settings is None or keys is None or not {'key', 'value'} <= set(settings):
                    raise Rejected('required existing database contract missing')
                rows = conn.execute("SELECT value FROM settings WHERE key='quota.daily-seed.v1' LIMIT 2").fetchall()
                daily = self._table(conn, 'key_daily_usage')
                added = {'daily_token_quota', 'daily_credit_quota', 'assigned_to'} & set(keys)
                if not rows:
                    if daily is not None or added:
                        raise Rejected('partial daily migration cannot authorize a legacy writer')
                    return 0
                if len(rows) != 1 or type(_json(rows[0][0], 'daily seed')) is not int or _json(rows[0][0], 'daily seed') != 1:
                    raise Rejected('unsupported daily seed marker')
                expected = {'key_id': ('INTEGER', 1, 1), 'day': ('TEXT', 1, 2),
                            'tokens': ('INTEGER', 1, 0), 'credit_units': ('INTEGER', 1, 0)}
                if daily is None or set(daily) != set(expected):
                    raise Rejected('daily counter layout mismatch')
                for name, want in expected.items():
                    row = daily[name]
                    if (row[2].upper(), row[3], row[5]) != want:
                        raise Rejected('daily counter constraints mismatch')
                for name, kind in [('daily_token_quota', 'INTEGER'), ('daily_credit_quota', 'REAL'), ('assigned_to', 'TEXT')]:
                    row = keys.get(name)
                    if row is None or row[2].upper() != kind or row[3] != 1:
                        raise Rejected('daily key columns mismatch')
                return 1
        except sqlite3.Error:
            raise Rejected('existing database is not safely readable') from None

    def observe(self, permit):
        self._locked(permit)
        raw = self._file(self.paths.users, application=True, limit=8 * 1024 * 1024)
        users = _json(raw, 'users')
        if (type(users) is not dict or type(users.get('users')) is not list
                or type(users.get('secret')) is not str or not users['secret']):
            raise Rejected('invalid existing users contract')
        policy = users.get('role_policy_version', 0)
        if type(policy) is not int or policy not in (0, 2) or ('role_policy_version' in users and policy == 0):
            raise Rejected('unsupported users policy')
        seen = set()
        for user in users['users']:
            if type(user) is not dict:
                raise Rejected('invalid existing users contract')
            name, role, sv = user.get('username'), user.get('role'), user.get('sv', 0)
            allowed = ('admin', 'account_admin') if policy == 2 else ('admin', 'viewer')
            if (type(name) is not str or not name.strip() or name in seen or role not in allowed
                    or type(sv) is not int or sv < 0 or type(user.get('pwd_hash')) is not str or not user['pwd_hash']
                    or (name == 'admin' and role != 'admin')):
                raise Rejected('invalid existing user identity policy')
            seen.add(name)
        if 'admin' not in seen:
            raise Rejected('root account missing')
        before = self._database_snapshot()
        daily = self._daily_state()
        if before != self._database_snapshot() or raw != self._file(self.paths.users, application=True, limit=8 * 1024 * 1024):
            raise Rejected('observed state changed during inspection')
        self._locked(permit)
        return ObservedState(Capabilities(policy, daily), _digest(raw), before, self._issuer)

    def load_floor(self):
        if self._file(self.paths.initialized, limit=1024) != _INITIALIZED:
            raise Rejected('invalid writer initialization witness')
        raw = self._file(self.paths.floor, limit=16384)
        value = _json(raw, 'writer floor')
        required = {'schema', 'version', 'epoch', 'role_policy', 'key_daily_usage', 'transaction_id'}
        if (type(value) is not dict or set(value) != required or value['schema'] != 'zhihui.writer-floor'
                or type(value['version']) is not int or value['version'] != 1
                or type(value['epoch']) is not int or value['epoch'] < 1
                or type(value['transaction_id']) is not str or not _LABEL.fullmatch(value['transaction_id'])):
            raise Rejected('invalid writer floor')
        return FloorRecord(value['epoch'], Capabilities(value['role_policy'], value['key_daily_usage']),
                           value['transaction_id'], _digest(raw), self._issuer)

    def require_release(self, release, observed, floor):
        self._owned(release, VerifiedRelease)
        self._owned(observed, ObservedState)
        self._owned(floor, FloorRecord)
        if not release.capabilities.covers(observed.capabilities.join(floor.capabilities)):
            raise Rejected('release cannot enforce retained writer state')

    def require_launch(self, release, observed, floor):
        self.require_release(release, observed, floor)
        if (not floor.capabilities.covers(observed.capabilities)
                or not floor.capabilities.covers(release.capabilities)):
            raise Rejected('startup migration requires a durably retained writer floor')

    def _owned(self, value, kind):
        if type(value) is not kind or value._issuer is not self._issuer:
            raise Rejected('fresh guard-owned evidence required')

    def initialize_floor(self, observed, release, permit, transaction_id):
        return self._persist(None, observed, release, permit, transaction_id)

    def advance_floor(self, expected, observed, release, permit, transaction_id):
        self._owned(expected, FloorRecord)
        return self._persist(expected, observed, release, permit, transaction_id)

    def _persist(self, expected, observed, release, permit, transaction_id):
        self._owned(observed, ObservedState)
        self._owned(release, VerifiedRelease)
        if type(transaction_id) is not str or not _LABEL.fullmatch(transaction_id):
            raise Rejected('invalid transaction identity')
        self._locked(permit, exclusive=True)
        self._path(self.paths.floor.parent)
        if expected is None:
            if self.paths.floor.exists() or self.paths.floor.is_symlink():
                raise Rejected('writer floor already exists')
            if self.paths.initialized.exists() or self.paths.initialized.is_symlink():
                raise Rejected('writer initialization already attempted; never reset retained state')
            retained = Capabilities()
        else:
            if self.load_floor() != expected:
                raise Rejected('writer floor compare-and-swap conflict')
            retained = expected.capabilities
        if self.observe(permit) != observed or self.inspect_release(release.root) != release:
            raise Rejected('state or release evidence is stale')
        retained = retained.join(observed.capabilities)
        if not release.capabilities.covers(retained):
            raise Rejected('release cannot enforce retained writer state')
        target = retained.join(release.capabilities)
        payload = {'schema': 'zhihui.writer-floor', 'version': 1,
                   'epoch': expected.epoch + 1 if expected else 1,
                   'role_policy': target.role_policy, 'key_daily_usage': target.key_daily_usage,
                   'transaction_id': transaction_id}
        raw = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if expected is None:
            # Retain the witness even on interruption. A missing floor afterward
            # is a repair case, never permission to initialize a lower floor.
            try:
                fd = os.open(self.paths.initialized, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(_INITIALIZED)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._sync_directory()
            except OSError:
                raise Rejected('writer initialization persistence unconfirmed') from None
        temporary = self.paths.floor.with_name('.floor-' + secrets.token_hex(16) + '.tmp')
        fd = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as handle:
                fd = None
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            self._locked(permit, exclusive=True)
            if expected is not None and self.load_floor() != expected:
                raise Rejected('writer floor compare-and-swap conflict')
            if expected is None and (self.paths.floor.exists() or self.paths.floor.is_symlink()):
                raise Rejected('writer floor appeared during initialization')
            os.replace(temporary, self.paths.floor)
            self._sync_directory()
            return self.load_floor()
        except OSError:
            raise Rejected('writer floor persistence unconfirmed; inspect retained state') from None
        finally:
            if fd is not None:
                os.close(fd)
            if temporary.exists():
                temporary.unlink()

    def _sync_directory(self):
        if os.name == 'posix':
            directory = os.open(self.paths.floor.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
