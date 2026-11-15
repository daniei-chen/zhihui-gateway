"""Read-only Linux writer/quiescence inventory; no service mutation or signals.

Only process identities and matching reasons escape this module. Never return
command lines, environments, database contents or open-file contents.
"""
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from state_guard import Rejected

UNITS = ('workbuddy-web.service', 'workbuddy-web-helper.service',
         'wb-budget-autoreconcile.service')
TIMER = 'wb-budget-autoreconcile.timer'
PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlGroup',
              'KillMode', 'UnitFileState')
DATA = '/opt/workbuddy-manager/data'
RELEASES = '/opt/workbuddy-manager/releases/'
SCRIPTS = (b'/usr/local/sbin/wb-budget-autoreconcile.py', b'backup_application.py')
WRITER_MARKERS = (b'server.main:app', b'server.serve')


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


def _writable_fd(proc_path, fd_name):
    """该 fd 是否以可写方式打开（`O_ACCMODE != O_RDONLY`）。

    读不到 `fdinfo` 时保守地当成可写：迁移宁可多等一次，也不能在真的有人写库
    时放行。
    """
    try:
        for line in (proc_path / 'fdinfo' / fd_name).read_text().splitlines():
            if line.startswith('flags:'):
                return bool(int(line.split()[1], 8) & 0o3)
    except (OSError, IndexError, ValueError):
        return True
    return True


def _wal_coordination_only(target):
    """`-shm` 是 WAL 的协调段：**只读连接也会打开它**，它本身不写库。

    把它算作"在写"会让运维一条只读的 `sqlite3 db "PRAGMA quick_check"` 拦住
    整次迁移。`-wal` 不在此列：里面有尚未合并进主库的页，必须算写者。
    """
    return target.endswith('-shm')


def _writable_map(line):
    """`/proc/<pid>/maps` 的一行是否是可写的 data 映射。"""
    fields = line.split()
    if len(fields) < 6 or not DATA + '/' in line:
        return False
    perms = fields[1]
    if 'w' not in perms:
        return False  # 只读 mmap（只读 SQLite 会话就是这样读主库的）
    return not _wal_coordination_only(fields[-1])


def _parent_of(proc, pid):
    """`/proc/<pid>/stat` 里的 ppid；读不到返回 None。"""
    try:
        raw = (proc / str(pid) / 'stat').read_text()
        return int(raw[raw.rfind(')') + 2:].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _shares_process_tree(proc, pid, own_chain):
    """pid 是否与迁移进程同属一棵进程树（祖先**或**后代）。

    只排除祖先不够：迁移在验证发布时会 fork 辅助进程（`trusted_bootstrap.py`
    之类），它们的 cwd 就在 `/opt/workbuddy-manager` 下，会被自己的扫描当成
    "另一个写入者"。从候选进程向上走父链，只要碰到自己的链就是同一棵树的。
    """
    seen = set()
    while pid is not None and pid > 1 and pid not in seen:
        if pid in own_chain:
            return True
        seen.add(pid)
        pid = _parent_of(proc, pid)
    return False


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
        # 排除与迁移同属一棵进程树的进程，而不只是自己。
        #
        # `sudo`、`bash -c` 或启动脚本的 cmdline/cwd 可能正好落在
        # `/opt/workbuddy-manager` 或 `releases/` 下 —— 在同一个 shell 里先
        # `cd` 到发布目录、再跑迁移，就是这个形状。反过来，迁移在验证发布时
        # 还会 fork 辅助进程（`trusted_bootstrap.py` 等），它们同样带着
        # `/opt/workbuddy-manager` 的 cwd。两边都不是"另一个并发写者"，
        # 把它们算进来会让迁移拦下自己。
        own_chain = set()
        pid = os.getpid()
        while pid > 1 and pid not in own_chain:
            own_chain.add(pid)
            pid = _parent_of(self.proc, pid) or 0
        found = []
        for path in self.proc.iterdir():
            if not path.name.isdigit():
                continue
            if _shares_process_tree(self.proc, int(path.name), own_chain):
                continue
            before = process_identity(self.proc, int(path.name))
            if before is None:
                continue
            reasons = set()
            try:
                command = (path / 'cmdline').read_bytes()
                if len(command) > 4 * 1024 * 1024:
                    raise Rejected('process command exceeds inspection bounds')
                if (any(marker in command for marker in WRITER_MARKERS)
                        or any(script in command for script in SCRIPTS)):
                    reasons.add('manager-command')
                try:
                    cwd = os.readlink(path / 'cwd')
                    if cwd == '/opt/workbuddy-manager' or cwd.startswith(RELEASES):
                        reasons.add('manager-working-directory')
                except FileNotFoundError:
                    pass  # Kernel threads and exited processes have no cwd.
                # 判据是"以可写方式打开"，不是"打开过"：只读的 SQLite 会话
                # （运维的 `sqlite3 db "PRAGMA quick_check"`、临时检查脚本）
                # 既不写库也不持写锁，却会打开主库与 `-shm`。把它们算成写者
                # 会让迁移偶发地失败一次 —— 实测在 v1.2.31/v1.2.32 的升级里
                # 连续踩到三次。
                for fd in (path / 'fd').iterdir():
                    try:
                        target = os.readlink(fd)
                    except FileNotFoundError:
                        continue
                    if not (target == DATA or target.startswith(DATA + '/')):
                        continue
                    if _wal_coordination_only(target):
                        continue
                    if _writable_fd(path, fd.name):
                        reasons.add('open-shared-data')
                        break
                # A process can mmap a SQLite file then close its descriptor.
                for line in (path / 'maps').read_text().splitlines():
                    if _writable_map(line):
                        reasons.add('mapped-shared-data')
                        break
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
