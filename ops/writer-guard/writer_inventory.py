"""Read-only Linux writer/quiescence inventory; no service mutation or signals.

Only process identities and matching reasons escape this module. Never return
command lines, environments, database contents or open-file contents.
"""
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from state_guard import Rejected

UNITS = ('workbuddy-web.service', 'wb-budget-autoreconcile.service')
TIMER = 'wb-budget-autoreconcile.timer'
PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlGroup',
              'KillMode', 'UnitFileState')
DATA = '/opt/workbuddy-manager/data'
RELEASES = '/opt/workbuddy-manager/releases/'
SCRIPTS = (b'/usr/local/sbin/wb-budget-autoreconcile.py', b'backup_application.py')


def process_identity(proc, pid):
    try:
        raw = (proc / str(pid) / 'stat').read_text()
    except FileNotFoundError:
        return None
    end = raw.rfind(')')
    fields = raw[end + 2:].split()
    if end < 0 or len(fields) < 20 or not fields[19].isdigit():
        raise Rejected('unreadable process start identity')
    return {'pid': int(pid), 'starttime': int(fields[19])}


class LinuxInventory:
    def __init__(self):
        self.proc, self.cgroup = Path('/proc'), Path('/sys/fs/cgroup')

    def unit(self, name):
        if name not in (*UNITS, TIMER):
            raise Rejected('unrecognized writer unit')
        result = subprocess.run(['/usr/bin/systemctl', 'show', name, '--no-pager',
                                 '-p', ','.join(PROPERTIES)], capture_output=True, timeout=10,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode or len(result.stdout) > 65536:
            raise Rejected('writer service metadata unavailable')
        rows = {}
        for line in result.stdout.decode('utf-8', errors='strict').splitlines():
            key, sep, value = line.partition('=')
            if not sep or key not in PROPERTIES or key in rows:
                raise Rejected('invalid writer service metadata')
            rows[key] = value
        required = set(PROPERTIES) - ({'MainPID', 'ControlGroup', 'KillMode'} if name == TIMER else set())
        if not required <= set(rows) or rows['Id'] != name:
            raise Rejected('writer service identity mismatch')
        return rows

    def cgroup_members(self, name):
        path = PurePosixPath(name)
        if (not name.startswith('/system.slice/') or '..' in path.parts
                or path.as_posix() != name):
            raise Rejected('unexpected writer cgroup')
        root = self.cgroup / name.lstrip('/')
        try:
            info = root.lstat()
        except FileNotFoundError:
            return []
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise Rejected('unsafe writer cgroup')
        members = set()
        for parent, dirs, files in os.walk(root, followlinks=False):
            for child in dirs:
                if (Path(parent) / child).is_symlink():
                    raise Rejected('unsafe nested writer cgroup')
            if 'cgroup.procs' in files:
                for value in (Path(parent) / 'cgroup.procs').read_text().split():
                    if not value.isdigit():
                        raise Rejected('invalid writer cgroup member')
                    members.add(int(value))
        return sorted(members)

    def listeners(self):
        ports = set()
        for name in ('tcp', 'tcp6'):
            text = (self.proc / 'net' / name).read_text()
            for line in text.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 4 or ':' not in fields[1]:
                    raise Rejected('unreadable kernel socket table')
                if fields[3] == '0A':
                    ports.add(int(fields[1].rsplit(':', 1)[1], 16))
        return sorted(ports & {7865, 7866})

    def processes(self):
        found = []
        for path in self.proc.iterdir():
            if not path.name.isdigit() or int(path.name) == os.getpid():
                continue
            before = process_identity(self.proc, int(path.name))
            if before is None:
                continue
            reasons = set()
            try:
                command = (path / 'cmdline').read_bytes()
                if len(command) > 4 * 1024 * 1024:
                    raise Rejected('process command exceeds inspection bounds')
                if (b'server.main:app' in command or b'/opt/workbuddy-manager/' in command
                        or any(script in command for script in SCRIPTS)):
                    reasons.add('manager-command')
                try:
                    cwd = os.readlink(path / 'cwd')
                    if cwd == '/opt/workbuddy-manager' or cwd.startswith(RELEASES):
                        reasons.add('manager-working-directory')
                except FileNotFoundError:
                    pass  # Kernel threads and exited processes have no cwd.
                for fd in (path / 'fd').iterdir():
                    try:
                        target = os.readlink(fd)
                    except FileNotFoundError:
                        continue
                    if target == DATA or target.startswith(DATA + '/'):
                        reasons.add('open-shared-data')
                        break
                # A process can mmap a SQLite file then close its descriptor.
                maps = (path / 'maps').read_text()
                if DATA + '/' in maps:
                    reasons.add('mapped-shared-data')
                after = process_identity(self.proc, int(path.name))
            except FileNotFoundError:
                if process_identity(self.proc, int(path.name)) is not None:
                    raise Rejected('live process became uninspectable') from None
                continue
            except PermissionError:
                raise Rejected('process inspection permission denied') from None
            if after is not None and after != before:
                raise Rejected('process identity changed during inventory')
            if after is not None and reasons:
                found.append({**before, 'reasons': sorted(reasons)})
        return sorted(found, key=lambda p: p['pid'])

    def snapshot(self):
        units = {name: self.unit(name) for name in UNITS}
        timer = self.unit(TIMER)
        # Inspect the canonical cgroup even when systemd returns an empty path.
        groups = {name: self.cgroup_members('/system.slice/' + name) for name in UNITS}
        return {'units': units, 'timer': timer, 'groups': groups,
                'listeners': self.listeners(), 'processes': self.processes()}


class Quiescence:
    def __init__(self, inventory, frozen_processes=(), start_gate=None):
        self.inventory = inventory
        self.frozen = tuple(frozen_processes)
        self.start_gate = start_gate

    def validate(self, permit):
        state = self.inventory.snapshot()
        timer = state['timer']
        if timer['ActiveState'] != 'inactive':
            raise Rejected('writer trigger must be masked and stopped')
        if self.start_gate is None:
            if timer['UnitFileState'] not in ('masked', 'masked-runtime'):
                raise Rejected('writer trigger must be masked and stopped')
        elif self.start_gate.validate() is not None:
            raise Rejected('persistent start gate must verify or raise')
        for name, unit in state['units'].items():
            if (unit['MainPID'] != '0' or unit['ActiveState'] not in ('inactive', 'failed')
                    or unit['SubState'] not in ('dead', 'failed') or unit['KillMode'] != 'control-group'
                    or unit['LoadState'] not in ('loaded', 'masked')):
                raise Rejected('writer service is not stopped')
            if state['groups'][name]:
                raise Rejected('writer cgroup still contains processes')
        if state['listeners'] or state['processes']:
            raise Rejected('unwrapped writer or listener still active')
        for identity in self.frozen:
            if process_identity(self.inventory.proc, identity['pid']) == identity:
                raise Rejected('bound predecessor process still alive')
