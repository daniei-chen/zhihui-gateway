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
          'starting', 'started', 'checking-ready', 'ready', 'resuming', 'complete', 'failed',
          'rolling-floor-pending', 'rolling-floor-persisted', 'rolling-pointing',
          'rolling-pointed', 'rolling-helper-starting', 'rolling-helper-started',
          'rolling-helper-checking', 'rolling-helper-ready', 'rolling-routing-helper',
          'rolling-routed-helper', 'rolling-fencing', 'rolling-fenced',
          'rolling-draining', 'rolling-drained', 'rolling-stopping-primary',
          'rolling-stopped-primary', 'rolling-main-starting', 'rolling-main-started',
          'rolling-main-checking', 'rolling-main-ready', 'rolling-routing-main',
          'rolling-routed-main', 'rolling-helper-stopping', 'rolling-helper-stopped'}


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
        valid_keys = (required, required | {'mode'})
        if (type(value) is not dict or set(value) not in valid_keys
                or value['schema'] != 'zhihui.writer-migration' or type(value['version']) is not int
                or value['version'] not in (1, 2) or type(value['epoch']) is not int or value['epoch'] < 1
                or value['phase'] not in PHASES or type(value['compensation_confirmed']) is not bool
                or value['failure'] not in (None, 'effect-or-persistence-unconfirmed')
                or any(type(value[k]) is not str or not _LABEL.fullmatch(value[k])
                       for k in ('transaction', 'candidate', 'predecessor'))
                or any(type(value[k]) is not str or not _SHA.fullmatch(value[k])
                       for k in ('candidate_sha256', 'predecessor_sha256'))):
            raise Rejected('invalid migration journal contract')
        if value['version'] == 1 and 'mode' in value:
            raise Rejected('legacy migration journal has unexpected mode')
        if value['version'] == 2 and (value.get('mode') not in ('fenced', 'rolling')):
            raise Rejected('invalid migration mode')
        if witness != {'schema': 'zhihui.migration-initialized', 'transaction': value['transaction']}:
            raise Rejected('migration initialization identity mismatch')
        return value

    def create(self, permit, transaction, candidate, predecessor, *, mode='fenced'):
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
        if mode not in ('fenced', 'rolling'):
            raise Rejected('invalid migration mode')
        value = {'schema': 'zhihui.writer-migration', 'version': 2, 'transaction': transaction,
                 'epoch': 1, 'phase': 'prepared', 'candidate': candidate.release_id,
                 'candidate_sha256': candidate.manifest_sha256, 'predecessor': predecessor.release_id,
                 'predecessor_sha256': predecessor.manifest_sha256,
                 'failure': None, 'compensation_confirmed': False, 'mode': mode}
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
        mode = self.host.migration_mode() if self.first_install is None else 'fenced'
        return self.journal.create(authority, transaction, candidate, predecessor, mode=mode)

    def _run_rolling(self, authority, record, candidate):
        """Run a helper-backed handoff with the relay kept available.

        The predecessor remains on 7866 until the candidate helper is ready
        and the relay route has moved to 7865.  Only then is the predecessor
        fenced and drained.  The final primary starts on 7866, the route moves
        back, and the helper is retired.  Each externally visible step has its
        own durable phase so a crash resumes from an inspected state instead
        of blindly repeating an accepted request.
        """
        try:
            if record['phase'] in ('prepared', 'rolling-floor-pending'):
                if record['phase'] == 'prepared':
                    record = self.journal.save(authority, record, 'rolling-floor-pending')
                # The old primary retains the shared runtime lease while its
                # code is serving.  Rolling mode only admits exact capability
                # matches, so the persisted floor is already sufficient and
                # does not need an exclusive advance here.
                with self.lock.acquire() as permit:
                    self.journal._authorized(authority)
                    candidate = self._release(record, 'candidate')
                    observed = self.guard.observe(permit)
                    floor = self.guard.load_floor()
                    self.guard.require_launch(candidate, observed, floor)
                    if candidate.capabilities != floor.capabilities:
                        raise Rejected('rolling migration requires exact writer capabilities')
                    record = self.journal.save(authority, record, 'rolling-floor-persisted')
                    record = self.journal.save(authority, record, 'rolling-pointing')
                    self._effect(self.host.point_rolling, record, candidate)
                    self.lock.validate(permit, exclusive=False)
                    record = self.journal.save(authority, record, 'rolling-pointed')

            if record['phase'] in ('rolling-floor-persisted', 'rolling-pointing'):
                if record['phase'] == 'rolling-floor-persisted':
                    record = self.journal.save(authority, record, 'rolling-pointing')
                with self.lock.acquire() as permit:
                    self._effect(self.host.point_rolling, record, candidate)
                    self.lock.validate(permit, exclusive=False)
                    record = self.journal.save(authority, record, 'rolling-pointed')

            if record['phase'] in ('rolling-pointed', 'rolling-helper-starting'):
                if record['phase'] == 'rolling-pointed':
                    record = self.journal.save(authority, record, 'rolling-helper-starting')
                self._effect(self.host.start_helper, record, candidate)
                record = self.journal.save(authority, record, 'rolling-helper-started')

            if record['phase'] in ('rolling-helper-started', 'rolling-helper-checking'):
                if record['phase'] == 'rolling-helper-started':
                    record = self.journal.save(authority, record, 'rolling-helper-checking')
                self._effect(self.host.helper_ready, record, candidate)
                record = self.journal.save(authority, record, 'rolling-helper-ready')

            if record['phase'] in ('rolling-helper-ready', 'rolling-routing-helper'):
                if record['phase'] == 'rolling-helper-ready':
                    record = self.journal.save(authority, record, 'rolling-routing-helper')
                self._effect(self.host.route_helper, record, candidate)
                record = self.journal.save(authority, record, 'rolling-routed-helper')

            if record['phase'] in ('rolling-routed-helper', 'rolling-fencing'):
                if record['phase'] == 'rolling-routed-helper':
                    record = self.journal.save(authority, record, 'rolling-fencing')
                self._effect(self.host.fence, record)
                record = self.journal.save(authority, record, 'rolling-fenced')

            if record['phase'] in ('rolling-fenced', 'rolling-draining'):
                if record['phase'] == 'rolling-fenced':
                    record = self.journal.save(authority, record, 'rolling-draining')
                self._effect(self.host.drain_old, record)
                record = self.journal.save(authority, record, 'rolling-drained')

            if record['phase'] in ('rolling-drained', 'rolling-stopping-primary'):
                if record['phase'] == 'rolling-drained':
                    record = self.journal.save(authority, record, 'rolling-stopping-primary')
                self._effect(self.host.stop_primary_rolling, record)
                record = self.journal.save(authority, record, 'rolling-stopped-primary')

            if record['phase'] in ('rolling-stopped-primary', 'rolling-main-starting'):
                if record['phase'] == 'rolling-stopped-primary':
                    record = self.journal.save(authority, record, 'rolling-main-starting')
                self._effect(self.host.start, record, candidate)
                record = self.journal.save(authority, record, 'rolling-main-started')

            if record['phase'] in ('rolling-main-started', 'rolling-main-checking'):
                if record['phase'] == 'rolling-main-started':
                    record = self.journal.save(authority, record, 'rolling-main-checking')
                self._effect(self.host.ready, record, candidate)
                record = self.journal.save(authority, record, 'rolling-main-ready')

            if record['phase'] in ('rolling-main-ready', 'rolling-routing-main'):
                if record['phase'] == 'rolling-main-ready':
                    record = self.journal.save(authority, record, 'rolling-routing-main')
                self._effect(self.host.route_main, record, candidate)
                record = self.journal.save(authority, record, 'rolling-routed-main')

            if record['phase'] in ('rolling-routed-main', 'rolling-helper-stopping'):
                if record['phase'] == 'rolling-routed-main':
                    record = self.journal.save(authority, record, 'rolling-helper-stopping')
                self._effect(self.host.stop_helper, record, candidate)
                record = self.journal.save(authority, record, 'rolling-helper-stopped')

            record = self.journal.save(authority, record, 'resuming')
            self._effect(self.host.resume, record, candidate)
            self._effect(self.host.verify_complete, record, candidate)
            return self.journal.save(authority, record, 'complete', failure=None,
                                     compensation_confirmed=False)
        except BaseException:
            confirmed = False
            try:
                self.journal._authorized(authority)
                # Fail closed at the public fence.  If the helper is already
                # the only live generation, the retained transaction records
                # that fact for an operator instead of starting old code.
                self._effect(self.host.fence, record)
                confirmed = True
            except BaseException:
                pass
            try:
                latest = self.journal.load(authority)
                self.journal.save(authority, latest, 'failed',
                                  failure='effect-or-persistence-unconfirmed',
                                  compensation_confirmed=confirmed)
            except BaseException:
                pass
            raise

    def run(self, authority):
        record = self.journal.load(authority)
        candidate = self._release(record, 'candidate')
        if record['phase'] == 'complete':
            self._effect(self.host.verify_complete, record, candidate)
            return record
        if self.host.current_release() not in (
                candidate.root, self.guard.paths.releases / record['predecessor']):
            raise Rejected('release pointer changed outside migration')
        if record.get('mode', 'fenced') == 'rolling':
            return self._run_rolling(authority, record, candidate)
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
