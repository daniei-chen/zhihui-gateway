"""Durable first-writer migration controller; host effects remain explicit.

No CLI and no implicit production paths: a reviewed host adapter must provide
publication/backup ownership, ingress fencing and complete process quiescence.
Recovery repeats the safe fence/drain/stop sequence, never restores old data or
automatically launches an incompatible predecessor.
"""
import json
import os
from pathlib import Path
import re
import secrets
import stat

from state_guard import Capabilities, Rejected, _json
from launch_guard import protected

_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
PHASES = {'prepared', 'fencing', 'fenced', 'draining', 'drained', 'stopping',
          'stopped', 'floor-pending', 'floor-persisted', 'pointing', 'pointed',
          'starting', 'started', 'checking-ready', 'ready', 'resuming', 'complete', 'failed'}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


class Journal:
    def __init__(self, paths, authority, transaction=None):
        self.paths, self.authority = paths, authority
        self.root = paths.floor.parent / 'migration'
        if transaction is not None:
            if type(transaction) is not str or not _LABEL.fullmatch(transaction):
                raise Rejected('invalid isolated migration transaction')
            self.root = paths.floor.parent / 'transactions' / transaction
        self.path = self.root / 'active.json'

    def _authorized(self, permit):
        if self.authority.validate(permit) is not None:
            raise Rejected('publication and backup authority must raise on failure')

    def _read(self, path):
        protected(path, self.paths)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384
                    or os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o600
                    or (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino)):
                raise Rejected('unsafe migration journal')
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise Rejected('oversized migration journal')
        return raw

    def _atomic(self, path, raw):
        protected(path.parent, self.paths, directory=True)
        temporary = path.with_name('.migration-' + secrets.token_hex(16) + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name == 'posix':
                dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, permit):
        self._authorized(permit)
        witness = _json(self._read(self.root / 'initialized.json'), 'migration initialization')
        value = _json(self._read(self.path), 'migration journal')
        required = {'schema', 'version', 'transaction', 'epoch', 'phase', 'candidate',
                    'candidate_sha256', 'predecessor', 'predecessor_sha256', 'failure',
                    'compensation_confirmed'}
        if (type(value) is not dict or set(value) != required
                or value['schema'] != 'zhihui.writer-migration' or type(value['version']) is not int
                or value['version'] != 1 or type(value['epoch']) is not int or value['epoch'] < 1
                or value['phase'] not in PHASES or type(value['compensation_confirmed']) is not bool
                or value['failure'] not in (None, 'effect-or-persistence-unconfirmed')
                or any(type(value[k]) is not str or not _LABEL.fullmatch(value[k])
                       for k in ('transaction', 'candidate', 'predecessor'))
                or any(type(value[k]) is not str or not _SHA.fullmatch(value[k])
                       for k in ('candidate_sha256', 'predecessor_sha256'))):
            raise Rejected('invalid migration journal contract')
        if witness != {'schema': 'zhihui.migration-initialized', 'transaction': value['transaction']}:
            raise Rejected('migration initialization identity mismatch')
        return value

    def create(self, permit, transaction, candidate, predecessor):
        self._authorized(permit)
        if type(transaction) is not str or not _LABEL.fullmatch(transaction):
            raise Rejected('invalid migration transaction')
        protected(self.paths.floor.parent, self.paths, directory=True)
        if self.root.parent != self.paths.floor.parent:
            if not self.root.parent.exists():
                self.root.parent.mkdir(mode=0o700)
            protected(self.root.parent, self.paths, directory=True)
        if not self.root.exists():
            self.root.mkdir(mode=0o700)
        protected(self.root, self.paths, directory=True)
        if self.path.exists() or self.path.is_symlink():
            raise Rejected('active migration must be recovered or explicitly archived')
        witness = self.root / 'initialized.json'
        if witness.exists() or witness.is_symlink():
            raise Rejected('migration history missing; never silently restart')
        fd = os.open(witness, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(canonical({'schema': 'zhihui.migration-initialized', 'transaction': transaction}))
            handle.flush()
            os.fsync(handle.fileno())
        value = {'schema': 'zhihui.writer-migration', 'version': 1, 'transaction': transaction,
                 'epoch': 1, 'phase': 'prepared', 'candidate': candidate.release_id,
                 'candidate_sha256': candidate.manifest_sha256, 'predecessor': predecessor.release_id,
                 'predecessor_sha256': predecessor.manifest_sha256,
                 'failure': None, 'compensation_confirmed': False}
        self._atomic(self.path, canonical(value))
        return self.load(permit)

    def save(self, permit, expected, phase, **changes):
        self._authorized(permit)
        if phase not in PHASES or set(changes) - {'failure', 'compensation_confirmed'}:
            raise Rejected('invalid migration transition')
        if self.load(permit) != expected:
            raise Rejected('migration journal compare-and-swap conflict')
        value = {**expected, **changes, 'epoch': expected['epoch'] + 1, 'phase': phase}
        self._atomic(self.path, canonical(value))
        return self.load(permit)


class Controller:
    def __init__(self, guard, runtime_lock, journal, host, *, first_install=None):
        if guard.lock is not runtime_lock or guard.paths != journal.paths:
            raise Rejected('migration authorities do not match')
        self.guard, self.lock, self.journal, self.host = guard, runtime_lock, journal, host
        if first_install is not None and (first_install.guard is not guard or first_install.journal is not journal):
            raise Rejected('first installation authorities do not match')
        self.first_install = first_install

    @staticmethod
    def _effect(call, *args):
        if call(*args) is not None:
            raise Rejected('host effects must verify completion or raise')

    def _release(self, record, name):
        release = self.guard.inspect_release(self.guard.paths.releases / record[name])
        if release.manifest_sha256 != record[name + '_sha256']:
            raise Rejected('bound release identity drift')
        return release

    def prepare(self, authority, transaction, candidate_root):
        self.journal._authorized(authority)
        candidate = self.guard.inspect_release(candidate_root)
        predecessor = self.guard.inspect_release(self.host.current_release())
        if self.first_install is None:
            floor = self.guard.load_floor()
            if not candidate.capabilities.covers(floor.capabilities):
                raise Rejected('candidate below retained writer floor')
        self._effect(self.host.preflight, candidate, predecessor)
        if self.first_install is not None:
            self.first_install.prepare(authority, transaction, candidate, predecessor)
        return self.journal.create(authority, transaction, candidate, predecessor)

    def run(self, authority):
        record = self.journal.load(authority)
        candidate = self._release(record, 'candidate')
        if record['phase'] == 'complete':
            self._effect(self.host.verify_complete, record, candidate)
            return record
        if self.host.current_release() not in (
                candidate.root, self.guard.paths.releases / record['predecessor']):
            raise Rejected('release pointer changed outside migration')
        try:
            # On recovery restart from fencing, not a possibly stale persisted
            # phase. Every effect must be idempotent against its bound intent.
            for pending, done, effect in (
                    ('fencing', 'fenced', self.host.fence),
                    ('draining', 'drained', self.host.drain),
                    ('stopping', 'stopped', self.host.stop_writers)):
                record = self.journal.save(authority, record, pending)
                self._effect(effect, record)
                record = self.journal.save(authority, record, done)
            record = self.journal.save(authority, record, 'floor-pending')
            with self.lock.acquire(exclusive=True) as permit:
                self.journal._authorized(authority)
                candidate = self._release(record, 'candidate')
                observed = self.guard.observe(permit)
                if self.first_install is not None:
                    floor = self.first_install.ensure(authority, record, candidate, observed, permit)
                else:
                    floor = self.guard.advance_floor(self.guard.load_floor(), observed, candidate,
                                                      permit, record['transaction'])
                self.guard.require_launch(candidate, observed, floor)
                record = self.journal.save(authority, record, 'floor-persisted')
                record = self.journal.save(authority, record, 'pointing')
                self._effect(self.host.point, record, candidate)
                self.lock.validate(permit, exclusive=True)
                record = self.journal.save(authority, record, 'pointed')
            # Exclusive lease MUST be gone before launcher takes its shared FD.
            record = self.journal.save(authority, record, 'starting')
            self._effect(self.host.start, record, candidate)
            record = self.journal.save(authority, record, 'started')
            record = self.journal.save(authority, record, 'checking-ready')
            self._effect(self.host.ready, record, candidate)
            record = self.journal.save(authority, record, 'ready')
            record = self.journal.save(authority, record, 'resuming')
            self._effect(self.host.resume, record, candidate)
            self._effect(self.host.verify_complete, record, candidate)
            return self.journal.save(authority, record, 'complete', failure=None,
                                     compensation_confirmed=False)
        except BaseException:
            confirmed = False
            try:
                self.journal._authorized(authority)
                self._effect(self.host.fence, record)
                confirmed = True
            except BaseException:
                pass
            try:
                # Reload: a successful rename with failed directory fsync may
                # have advanced the journal even though save raised.
                latest = self.journal.load(authority)
                self.journal.save(authority, latest, 'failed',
                                  failure='effect-or-persistence-unconfirmed',
                                  compensation_confirmed=confirmed)
            except BaseException:
                pass
            raise
