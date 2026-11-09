"""Actual fixed writer lifecycle effects, with explicit installed trust adapters.

Requires an installed tool-closure/unit verifier and coordinated frontend
publisher. Neither dependency may be a boolean or a self-declared release flag.
No CLI is exposed until the installer and first-floor initialization are bound.
"""
import json
import os
from pathlib import Path
import secrets
import stat
import time

from launch_guard import protected
from state_guard import Rejected, _json
from systemd_adapter import MAIN, JOB, TIMER
from writer_inventory import process_identity


class Host:
    def __init__(self, guard, lock, journal, authority, permit, gate, ingress, systemd,
                 inventory, identity, maintenance_host, startup, frontend, retirement):
        self.guard, self.lock, self.journal = guard, lock, journal
        self.authority, self.permit = authority, permit
        self.gate, self.ingress, self.systemd = gate, ingress, systemd
        self.inventory, self.identity, self.http = inventory, identity, maintenance_host
        self.startup, self.frontend = startup, frontend
        self.retirement = retirement
        self.intent = journal.root / 'host.json'
        self.ready_identity = None

    def owned(self):
        if self.authority.validate(self.permit) is not None:
            raise Rejected('host operation lacks publication and backup ownership')

    def current_release(self):
        pointer = self.guard.paths.releases / 'current'
        protected(pointer.parent, self.guard.paths, directory=True)
        info = pointer.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != self.guard.paths.root_uid:
            raise Rejected('protected release pointer required')
        target = pointer.resolve(strict=True)
        if target.parent != self.guard.paths.releases:
            raise Rejected('current release escapes fixed release root')
        return target

    def _load(self):
        self.owned()
        value = _json(self.journal._read(self.intent), 'writer host intent')
        required = {'schema', 'version', 'original_process', 'predecessor', 'candidate',
                    'candidate_sha256', 'timer_active', 'job_previous_pid'}
        if (type(value) is not dict or set(value) != required or value['schema'] != 'zhihui.writer-host'
                or value['version'] != 1 or type(value['timer_active']) is not bool
                or type(value['job_previous_pid']) is not int or value['job_previous_pid'] < 0
                or type(value['original_process']) is not dict
                or set(value['original_process']) != {'pid', 'starttime'}
                or any(type(v) is not int or v <= 0 for v in value['original_process'].values())):
            raise Rejected('invalid host migration intent')
        return value

    def _bound(self, record, candidate=None):
        value = self._load()
        if any(value[k] != record[k] for k in ('candidate', 'candidate_sha256', 'predecessor')):
            raise Rejected('host intent differs from migration identity')
        if candidate is not None and (candidate.release_id != value['candidate']
                                      or candidate.manifest_sha256 != value['candidate_sha256']):
            raise Rejected('host effect candidate differs from intent')
        return value

    def preflight(self, candidate, predecessor):
        self.owned()
        self.startup.verify_tools()
        self.retirement.preflight(self.permit)
        if not self.journal.root.parent.exists():
            protected(self.guard.paths.floor.parent, self.guard.paths, directory=True)
            self.journal.root.parent.mkdir(mode=0o700)
        protected(self.journal.root.parent, self.guard.paths, directory=True)
        if not self.journal.root.exists():
            self.journal.root.mkdir(mode=0o700)
        protected(self.journal.root, self.guard.paths, directory=True)
        self.frontend.preflight(candidate)
        state = self.systemd.show(MAIN)
        if (state['ActiveState'] != 'active' or state['SubState'] != 'running'
                or state['KillMode'] != 'control-group' or not state['MainPID'].isdigit()
                or state['ControlGroup'] != '/system.slice/' + MAIN):
            raise Rejected('verified live predecessor required')
        identity = process_identity(self.inventory.proc, int(state['MainPID']))
        if identity is None or self.current_release() != predecessor.root:
            raise Rejected('predecessor changed during preparation')
        current_cwd = os.readlink(self.inventory.proc / str(identity['pid']) / 'cwd')
        if current_cwd not in ('/opt/workbuddy-manager', str(predecessor.root)):
            raise Rejected('predecessor source binding differs')
        rows = self.inventory.processes()
        if [r['pid'] for r in rows] != [identity['pid']]:
            raise Rejected('unknown or additional writer blocks first migration')
        self.http.listeners(identity)
        self.http.upstream()
        job = self.systemd.show(JOB)
        if job['MainPID'] != '0' or job['ActiveState'] not in ('inactive', 'failed'):
            raise Rejected('scheduled writer must finish before preparation')
        timer = self.systemd.show(TIMER)
        if timer['ActiveState'] not in ('active', 'inactive'):
            raise Rejected('scheduled trigger is transitioning')
        proposed = {'schema': 'zhihui.writer-host', 'version': 1, 'original_process': identity,
                    'predecessor': predecessor.release_id, 'candidate': candidate.release_id,
                    'candidate_sha256': candidate.manifest_sha256,
                    'timer_active': timer['ActiveState'] == 'active',
                    'job_previous_pid': int(job.get('ExecMainPID', '0'))}
        if self.intent.exists() or self.intent.is_symlink():
            if self._load() != proposed:
                raise Rejected('previous host intent conflicts; never overwrite')
        else:
            self.journal._atomic(self.intent, (json.dumps(proposed, sort_keys=True) + '\n').encode())
        self.ingress.prepare(self.permit)

    def _process(self, record):
        value = self._bound(record)
        state = self.systemd.show(MAIN)
        pid = int(state['MainPID'])
        if pid == 0:
            if state['ActiveState'] not in ('inactive', 'failed'):
                raise Rejected('primary is transitioning')
            return None
        actual = process_identity(self.inventory.proc, pid)
        if actual == value['original_process']:
            if self.current_release().name != value['predecessor']:
                raise Rejected('old writer remains alive after pointer change')
            return actual
        candidate = self.guard.inspect_release(self.guard.paths.releases / value['candidate'])
        if candidate.manifest_sha256 != value['candidate_sha256']:
            raise Rejected('candidate changed during process binding')
        return self.identity.verify(pid, candidate)

    def fence(self, record):
        self._bound(record)
        self.gate.block(self.permit)
        self.gate.verify_blocked(self.permit, self.systemd.conditions)
        self.ingress.apply(self.permit, 'fenced')
        self.ingress.verify(self.permit, fenced=True)

    def drain(self, record):
        self._bound(record)
        self.ingress.verify(self.permit, fenced=True)
        identity = self._process(record)
        if identity is None:
            return
        self.http.listeners(identity)
        state = self.http.drain()
        if not state['draining']:
            self.http.drain('begin')
        deadline = time.monotonic() + 120
        while True:
            self.owned()
            if self._process(record) != identity:
                raise Rejected('primary changed while draining')
            state = self.http.drain()
            if not state['draining']:
                raise Rejected('drain state was lost')
            if state['total_in_flight'] == 0:
                return
            if time.monotonic() >= deadline:
                raise Rejected('in-flight operations did not drain; never force-stop')
            time.sleep(.25)

    def stop_writers(self, record):
        self._bound(record)
        self.ingress.verify(self.permit, fenced=True)
        self.gate.verify_blocked(self.permit, self.systemd.conditions)
        identity = self._process(record)
        if identity is not None and self.http.drain()['total_in_flight']:
            raise Rejected('requests appeared after drain')
        deadline = time.monotonic() + 90
        attempt = 0
        while True:
            attempt += 1
            try:
                self.systemd.command(['stop', TIMER])
                self.systemd.command(['stop', JOB])
                self.systemd.command(['stop', MAIN])
                self.lock.quiescence.validate(None)
                # The publication lease excludes old publishers. Retire their
                # callable paths only after draining writers, before persisting
                # a stronger floor.
                self.retirement.retire(self.permit)
                self.retirement.verify(self.permit)
                break
            except Exception as exc:
                try:
                    with open('/opt/zhihui-release/writer-state/stop-writers-retry.log', 'a') as handle:
                        handle.write('%d %s %r\n' % (attempt, time.strftime('%H:%M:%S'), exc))
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def point(self, record, candidate):
        self._bound(record, candidate)
        self.retirement.verify(self.permit)
        self.gate.verify_blocked(self.permit, self.systemd.conditions)
        self.startup.install(self.permit)
        self.startup.verify(MAIN)
        self.startup.verify(JOB)
        if self.current_release() not in (candidate.root, self.guard.paths.releases / record['predecessor']):
            raise Rejected('unexpected pointer before atomic activation')
        pointer = self.guard.paths.releases / 'current'
        temporary = pointer.with_name('.writer-current-' + secrets.token_hex(16))
        try:
            temporary.symlink_to(candidate.release_id, target_is_directory=True)
            os.replace(temporary, pointer)
            fd = os.open(pointer.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary.is_symlink():
                temporary.unlink()
        if self.current_release() != candidate.root:
            raise Rejected('atomic pointer activation not confirmed')

    def start(self, record, candidate):
        self._bound(record, candidate)
        self.retirement.verify(self.permit)
        self.ingress.verify(self.permit, fenced=True)
        self.startup.verify(MAIN)
        self.gate.release(self.permit, MAIN, self.startup)
        self.systemd.command(['reset-failed', MAIN])
        self.systemd.command(['start', MAIN])

    def _ready(self, record, candidate):
        self._bound(record, candidate)
        self.retirement.verify(self.permit)
        self.startup.verify(MAIN)
        state = self.systemd.show(MAIN)
        if (state['ActiveState'] != 'active' or state['SubState'] != 'running'
                or state['ControlGroup'] != '/system.slice/' + MAIN):
            raise Rejected('candidate primary not running in bound service')
        identity = self.identity.verify(int(state['MainPID']), candidate)
        self.http.listeners(identity)
        code, body = self.http.http(7866, '/api/readyz')
        value = _json(body, 'readiness')
        if code != 200 or type(value) is not dict or value.get('ok') is not True:
            raise Rejected('candidate readiness not confirmed')
        with self.lock.acquire() as permit:
            observed, floor = self.guard.observe(permit), self.guard.load_floor()
            self.guard.require_launch(candidate, observed, floor)
            if observed.capabilities != candidate.capabilities:
                raise Rejected('candidate startup migration incomplete')
        return identity

    def ready(self, record, candidate):
        self.ingress.verify(self.permit, fenced=True)
        deadline = time.monotonic() + 90
        while True:
            try:
                self.ready_identity = self._ready(record, candidate)
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise Rejected('candidate failed bounded readiness verification') from None
                time.sleep(.5)

    def resume(self, record, candidate):
        original = self._bound(record, candidate)
        if self._ready(record, candidate) != self.ready_identity:
            raise Rejected('candidate changed between ready and resume')
        self.frontend.activate(self.permit, record, candidate)
        self.frontend.verify(candidate)
        self.startup.verify(JOB)
        self.gate.release(self.permit, JOB, self.startup)
        self.systemd.command(['start', JOB])
        deadline = time.monotonic() + 90
        attempt = 0
        while True:
            attempt += 1
            try:
                job = self.systemd.show(JOB)
                if (job.get('ExecMainCode') != '1' or job.get('ExecMainStatus') != '0'
                        or int(job.get('ExecMainPID', '0')) <= 0
                        or int(job['ExecMainPID']) == original['job_previous_pid']
                        or job['ActiveState'] != 'inactive' or job['MainPID'] != '0'):
                    raise Rejected('guarded scheduled reconciliation did not complete')
                if self._ready(record, candidate) != self.ready_identity:
                    raise Rejected('candidate changed during scheduled verification')
                self.ingress.apply(self.permit, 'original')
                self.ingress.verify(self.permit, fenced=False)
                if original['timer_active']:
                    self.systemd.command(['start', TIMER])
                break
            except Exception as exc:
                try:
                    with open('/opt/zhihui-release/writer-state/resume-retry.log', 'a') as handle:
                        handle.write('%d %s %r\n' % (attempt, time.strftime('%H:%M:%S'), exc))
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def verify_complete(self, record, candidate):
        original = self._bound(record, candidate)
        self.ingress.verify(self.permit, fenced=False)
        self._ready(record, candidate)
        self.frontend.verify(candidate)
        timer = self.systemd.show(TIMER)
        if timer['ActiveState'] != ('active' if original['timer_active'] else 'inactive'):
            raise Rejected('original timer state not restored')
