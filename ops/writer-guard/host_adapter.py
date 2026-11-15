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
from systemd_adapter import HELPER, MAIN, JOB, TIMER
from writer_inventory import process_identity


RELAY_ROUTE = Path('/opt/workbuddy-manager/data/relay-route.json')
RELAY_GID = 10001
RELAY_TARGETS = frozenset({'old', 'new'})


def _route_parent(route):
    route = Path(route)
    if not route.is_absolute() or '..' in route.parts:
        raise Rejected('relay route path is not fixed')
    parent = route.parent
    try:
        info = parent.lstat()
    except OSError:
        raise Rejected('relay route directory unavailable') from None
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid not in (0, 10001) or info.st_mode & 0o022):
        raise Rejected('relay route directory is unsafe')
    return route


def _read_route(route=RELAY_ROUTE):
    route = _route_parent(route)
    try:
        info = route.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or info.st_size > 4096
                or info.st_uid not in (0, 10001) or info.st_mode & 0o022):
            raise Rejected('relay route file is unsafe')
        value = json.loads(route.read_text(encoding='utf-8'))
    except Rejected:
        raise
    except (OSError, ValueError):
        raise Rejected('relay route file is invalid') from None
    if (type(value) is not dict or set(value) != {'epoch', 'target'}
            or type(value['epoch']) is not int or value['epoch'] < 1
            or value['target'] not in RELAY_TARGETS):
        raise Rejected('relay route file is invalid')
    return value


def _switch_route(target, route=RELAY_ROUTE):
    if target not in RELAY_TARGETS:
        raise Rejected('invalid relay route target')
    route = _route_parent(route)
    current = _read_route(route) if route.exists() else {'epoch': 0, 'target': 'old'}
    if current['target'] == target:
        return current
    value = {'epoch': current['epoch'] + 1, 'target': target}
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    temporary = route.with_name('.relay-route-' + secrets.token_hex(16) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, RELAY_GID)
        os.chmod(temporary, 0o640)
        os.replace(temporary, route)
        directory_fd = os.open(route.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return value


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
        self.rolling = False
        self._candidate_capabilities = None

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
        allowed = (required, required | {'mode'})
        if (type(value) is not dict or set(value) not in allowed or value['schema'] != 'zhihui.writer-host'
                or value['version'] != 1 or type(value['timer_active']) is not bool
                or type(value['job_previous_pid']) is not int or value['job_previous_pid'] < 0
                or type(value['original_process']) is not dict
                or set(value['original_process']) != {'pid', 'starttime'}
                or any(type(v) is not int or v <= 0 for v in value['original_process'].values())):
            raise Rejected('invalid host migration intent')
        if 'mode' not in value:
            value = {**value, 'mode': 'fenced'}
        if value['mode'] not in ('fenced', 'rolling'):
            raise Rejected('invalid host migration mode')
        return value

    def _bound(self, record, candidate=None):
        value = self._load()
        self.rolling = value.get('mode') == 'rolling'
        if any(value[k] != record[k] for k in ('candidate', 'candidate_sha256', 'predecessor')):
            raise Rejected('host intent differs from migration identity')
        if candidate is not None and (candidate.release_id != value['candidate']
                                      or candidate.manifest_sha256 != value['candidate_sha256']):
            raise Rejected('host effect candidate differs from intent')
        return value

    def preflight(self, candidate, predecessor):
        self.owned()
        self._candidate_capabilities = candidate.capabilities
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
        self.http.listeners(identity, allow_helper=self.rolling, expected_port=7866)
        self.http.upstream()
        self.rolling = self._rolling_available()
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
                    'job_previous_pid': int(job.get('ExecMainPID', '0')),
                    'mode': 'rolling' if self.rolling else 'fenced'}
        if self.intent.exists() or self.intent.is_symlink():
            if self._load() != proposed:
                raise Rejected('previous host intent conflicts; never overwrite')
        else:
            self.journal._atomic(self.intent, (json.dumps(proposed, sort_keys=True) + '\n').encode())
        self.ingress.prepare(self.permit)

    def _rolling_available(self):
        """Return whether the host can protect the relay during a Manager cutover.

        The legacy fenced migration remains the safe fallback when the helper
        unit or relay route is absent.  Capability is derived from live host
        contracts, never from a release flag.
        """
        try:
            helper = self.systemd.show(HELPER)
            if (helper.get('LoadState') != 'loaded'
                    or helper.get('ActiveState') not in ('inactive', 'failed')):
                return False
            # The writer floor is deliberately not advanced while the old
            # process still holds the shared runtime lock.  Online handoff is
            # therefore limited to patch releases with the exact same writer
            # capabilities; capability/schema changes use the fenced path.
            if self._candidate_capabilities is None:
                return False
            if self._candidate_capabilities != self.guard.load_floor().capabilities:
                return False
            route = _read_route()
            if route['target'] != 'old':
                return False
            code, _ = self.http.http(7867, '/v1/models')
            return code in (200, 401, 403)
        except Exception:
            return False

    def migration_mode(self):
        return 'rolling' if self.rolling else 'fenced'

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
        self.http.listeners(identity, allow_helper=self.rolling, expected_port=7866)
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

    def _activate_pointer(self, record, candidate):
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

    def point_rolling(self, record, candidate):
        """Activate the candidate while the old primary is still serving.

        The candidate is admitted under the already persisted writer floor and
        first enters through the isolated helper port.  The old process has
        loaded its immutable source already, so changing the pointer here does
        not change the bytes it is executing.
        """
        self._bound(record, candidate)
        if self._load().get('mode') != 'rolling':
            raise Rejected('rolling host intent required')
        self.retirement.verify(self.permit)
        self.startup.prepare(self.permit)
        self._activate_pointer(record, candidate)

    def point(self, record, candidate):
        self._bound(record, candidate)
        self.retirement.verify(self.permit)
        self.gate.verify_blocked(self.permit, self.systemd.conditions)
        self.startup.install(self.permit)
        self.startup.verify(MAIN)
        self.startup.verify(JOB)
        self._activate_pointer(record, candidate)

    def start_helper(self, record, candidate):
        self._bound(record, candidate)
        if self._load().get('mode') != 'rolling':
            raise Rejected('rolling host intent required')
        self.startup.verify(HELPER)
        marker = self.gate.marker(HELPER)
        if marker.exists() or marker.is_symlink():
            raise Rejected('helper start gate is already blocked')
        state = self.systemd.show(HELPER)
        if state['ActiveState'] == 'active' and state['SubState'] == 'running':
            return
        if state['ActiveState'] not in ('inactive', 'failed'):
            raise Rejected('helper is transitioning')
        # `systemctl reset-failed` 对"从未进入 failed"的 unit 会退化为
        # "Unit … not loaded"（没有 manager object），返回非零并让整次交接
        # 在 `start` 之前失败。`start` 本身不需要这条清理 —— 只有确实处于
        # failed 时才需要它把残留状态清掉再启动。
        if state['ActiveState'] == 'failed':
            self.systemd.command(['reset-failed', HELPER])
        self.systemd.command(['start', HELPER])

    def _helper_ready(self, record, candidate):
        self._bound(record, candidate)
        self.retirement.verify(self.permit)
        self.startup.verify(HELPER)
        state = self.systemd.show(HELPER)
        if (state['ActiveState'] != 'active' or state['SubState'] != 'running'
                or state['ControlGroup'] != '/system.slice/' + HELPER):
            raise Rejected('candidate helper not running in bound service')
        identity = self.identity.verify(int(state['MainPID']), candidate, helper=True)
        self.http.listeners(identity, allow_helper=True, expected_port=7865)
        code, body = self.http.http(7865, '/api/readyz')
        value = _json(body, 'helper readiness')
        if code != 200 or type(value) is not dict or value.get('ok') is not True:
            raise Rejected('candidate helper readiness not confirmed')
        with self.lock.acquire() as permit:
            observed, floor = self.guard.observe(permit), self.guard.load_floor()
            self.guard.require_launch(candidate, observed, floor)
            if observed.capabilities != candidate.capabilities:
                raise Rejected('candidate helper startup migration incomplete')
        return identity

    def helper_ready(self, record, candidate):
        # 返回值是候选进程身份：`_run_rolling` 用它判断"就绪已被确认"。
        # 丢掉它会让控制器在 helper 已就绪之后报 "returned no process identity"
        # 并围栏 —— 第一次真正的 rolling 就是这样失败的。
        return self._retry_helper_ready(record, candidate, 'helper-ready')

    def _retry_helper_ready(self, record, candidate, label):
        deadline = time.monotonic() + 90
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._helper_ready(record, candidate)
            except Exception as exc:
                try:
                    with open('/opt/zhihui-release/writer-state/resume-retry.log', 'a') as handle:
                        handle.write('%s %d %s %r\n' % (label, attempt, time.strftime('%H:%M:%S'), exc))
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def _probe_relay(self):
        code, _ = self.http.http(7867, '/v1/models')
        if code not in (200, 401, 403):
            raise Rejected('relay route health probe failed')

    def route_helper(self, record, candidate):
        self._bound(record, candidate)
        if _switch_route('new')['target'] != 'new':
            raise Rejected('relay helper route was not committed')
        self._probe_relay()

    def route_main(self, record, candidate):
        self._bound(record, candidate)
        if _switch_route('old')['target'] != 'old':
            raise Rejected('relay main route was not committed')
        self._probe_relay()

    def drain_old(self, record):
        """Drain the predecessor after the relay has moved to the helper."""
        self._bound(record)
        self.ingress.verify(self.permit, fenced=True)
        value = self._load()
        identity = value['original_process']
        state = self.systemd.show(MAIN)
        pid = int(state['MainPID'])
        if pid == 0:
            return
        actual = process_identity(self.inventory.proc, pid)
        if actual != identity:
            raise Rejected('predecessor changed during rolling drain')
        self.http.listeners(identity, allow_helper=True, expected_port=7866)
        state = self.http.drain()
        if not state['draining']:
            self.http.drain('begin')
        deadline = time.monotonic() + 120
        while True:
            self.owned()
            state = self.systemd.show(MAIN)
            actual = process_identity(self.inventory.proc, int(state['MainPID'])) if state['MainPID'].isdigit() else None
            if actual != identity:
                raise Rejected('predecessor changed while rolling drain')
            current = self.http.drain()
            if not current['draining']:
                raise Rejected('predecessor drain state was lost')
            if current['total_in_flight'] == 0:
                return
            if time.monotonic() >= deadline:
                raise Rejected('predecessor operations did not drain; never force-stop')
            time.sleep(.25)

    def stop_primary_rolling(self, record):
        """Stop only the old primary; the known helper remains the data plane."""
        self._bound(record)
        self.ingress.verify(self.permit, fenced=True)
        self.gate.verify_blocked(self.permit, self.systemd.conditions)
        value = self._load()
        state = self.systemd.show(MAIN)
        if state['MainPID'] != '0' and self.http.drain()['total_in_flight']:
            raise Rejected('requests appeared after rolling drain')
        self.systemd.command(['stop', TIMER])
        self.systemd.command(['stop', JOB])
        self.systemd.command(['stop', MAIN])
        deadline = time.monotonic() + 90
        while True:
            state = self.systemd.show(MAIN)
            if state['MainPID'] == '0' and state['ActiveState'] in ('inactive', 'failed'):
                break
            if time.monotonic() >= deadline:
                raise Rejected('predecessor primary did not stop')
            time.sleep(.25)
        helper = self.systemd.show(HELPER)
        if helper['ActiveState'] != 'active' or helper['SubState'] != 'running':
            raise Rejected('rolling helper disappeared while stopping predecessor')
        helper_id = process_identity(self.inventory.proc, int(helper['MainPID']))
        if helper_id is None:
            raise Rejected('rolling helper identity unavailable')
        self.http.listeners(helper_id, allow_helper=True, expected_port=7865)
        self.retirement.retire(self.permit)
        self.retirement.verify(self.permit)

    def stop_helper(self, record, candidate):
        self._bound(record, candidate)
        if _read_route()['target'] != 'old':
            raise Rejected('cannot stop helper before main route is active')
        self.systemd.command(['stop', HELPER])
        deadline = time.monotonic() + 90
        while True:
            state = self.systemd.show(HELPER)
            if state['MainPID'] == '0' and state['ActiveState'] in ('inactive', 'failed'):
                return
            if time.monotonic() >= deadline:
                raise Rejected('rolling helper did not stop')
            time.sleep(.25)

    def start(self, record, candidate):
        self._bound(record, candidate)
        self.retirement.verify(self.permit)
        self.ingress.verify(self.permit, fenced=True)
        self.startup.verify(MAIN)
        state = self.systemd.show(MAIN)
        if state['ActiveState'] == 'active' and state['SubState'] == 'running':
            return
        if state['ActiveState'] not in ('inactive', 'failed'):
            raise Rejected('primary is transitioning before start')
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
        self.http.listeners(identity, allow_helper=self.rolling, expected_port=7866)
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
        if self.rolling:
            helper = self.systemd.show(HELPER)
            if helper['ActiveState'] != 'active' or helper['SubState'] != 'running':
                raise Rejected('rolling helper disappeared before primary readiness')
            helper_id = self.identity.verify(int(helper['MainPID']), candidate, helper=True)
            self.http.listeners(helper_id, allow_helper=True, expected_port=7865)
        self.ready_identity = self._retry_ready(record, candidate, 'candidate-ready')

    def _retry_ready(self, record, candidate, label, expected=None):
        deadline = time.monotonic() + 90
        attempt = 0
        while True:
            attempt += 1
            try:
                identity = self._ready(record, candidate)
                if expected is not None and identity != expected:
                    raise Rejected('candidate changed between ready and resume')
                return identity
            except Exception as exc:
                try:
                    with open('/opt/zhihui-release/writer-state/resume-retry.log', 'a') as handle:
                        handle.write('%s %d %s %r\n' % (label, attempt, time.strftime('%H:%M:%S'), exc))
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def _resume_ready(self, record, candidate):
        return self._retry_ready(record, candidate, 'pre-ready', expected=self.ready_identity)

    def resume(self, record, candidate):
        original = self._bound(record, candidate)
        self._resume_ready(record, candidate)
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
        self._retry_ready(record, candidate, 'verify-ready')
        self.frontend.verify(candidate)
        if self.rolling:
            if _read_route()['target'] != 'old':
                raise Rejected('rolling completion route is not on primary')
            helper = self.systemd.show(HELPER)
            if helper['MainPID'] != '0' or helper['ActiveState'] not in ('inactive', 'failed'):
                raise Rejected('rolling helper remains active after completion')
        timer = self.systemd.show(TIMER)
        if timer['ActiveState'] != ('active' if original['timer_active'] else 'inactive'):
            raise Rejected('original timer state not restored')
