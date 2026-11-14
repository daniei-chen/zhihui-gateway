"""Bind actual writer execution to a verified release and retained kernel lock."""
import os
from pathlib import Path
import stat

from launch_guard import writer_command
from state_guard import Rejected
from writer_inventory import process_identity


class RuntimeIdentity:
    def __init__(self, proc=Path('/proc'), lock=Path('/run/zhihui-writer-lifetime.lock'),
                 python=Path('/opt/workbuddy-manager/venv/bin/python')):
        self.proc, self.lock, self.python = Path(proc), Path(lock), Path(python)

    def verify(self, pid, release, *, helper=False, reconcile=False, expected=None):
        if helper and reconcile:
            raise Rejected('writer profile is ambiguous')
        if type(pid) is not int or pid <= 1:
            raise Rejected('live writer PID required')
        before = process_identity(self.proc, pid)
        if before is None or expected is not None and before != expected:
            raise Rejected('writer PID generation mismatch')
        root = self.proc / str(pid)
        try:
            status = {}
            for line in (root / 'status').read_text().splitlines():
                name, sep, value = line.partition(':')
                if sep:
                    status[name] = value.strip()
            if (status.get('Uid', '').split() != ['10001'] * 4
                    or status.get('Gid', '').split() != ['10001'] * 4
                    or status.get('Groups', '').split() != ['114']
                    or int(status.get('CapEff', '-1'), 16) != 0
                    or int(status.get('CapBnd', '-1'), 16) != 0 or status.get('NoNewPrivs') != '1'):
                raise Rejected('writer kernel privilege contract differs')
            if os.readlink(root / 'cwd') != str(release.root):
                raise Rejected('writer cwd is not immutable verified release')
            command = (root / 'cmdline').read_bytes()
            expected_argv = writer_command(helper=helper, reconcile=reconcile)
            executable = '/opt/workbuddy-manager/venv/bin/python'
            expected_argv = expected_argv[expected_argv.index(executable):]
            if command != b'\0'.join(x.encode() for x in expected_argv) + b'\0':
                raise Rejected('writer command differs from fixed single-process profile')
            actual, trusted = (root / 'exe').stat(), self.python.stat()
            if (actual.st_dev, actual.st_ino) != (trusted.st_dev, trusted.st_ino):
                raise Rejected('writer executable identity mismatch')
            raw = (root / 'environ').read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise Rejected('writer environment exceeds inspection bounds')
            env = {}
            for item in raw.split(b'\0'):
                if not item:
                    continue
                key, separator, value = item.partition(b'=')
                if not separator or key in env:
                    raise Rejected('invalid writer environment')
                env[key] = value
            selected = {'PYTHONPATH': str(release.root), 'WB_DATA_DIR': '/opt/workbuddy-manager/data',
                        'WB_DB': '/opt/workbuddy-manager/data/manager.db',
                        'WB_USERS_FILE': '/opt/workbuddy-manager/data/users.json',
                        'WB_STATIC_DIR': str(release.root / 'web/out'), 'WB_MANAGER_HOST': '127.0.0.1',
                        'WB_MANAGER_PORT': '7865' if helper else '7866',
                        'PYTHONDONTWRITEBYTECODE': '1',
                        'PYTHONNOUSERSITE': '1', 'PYTHONSAFEPATH': '1',
                        'PYTHONPYCACHEPREFIX': '/opt/zhihui-release/python-cache',
                        'WB_SHARED_ADMISSION': '1'}
            if any(env.get(key.encode()) != value.encode() for key, value in selected.items()):
                raise Rejected('writer data or import binding differs')
            if (helper or reconcile) and env.get(b'WB_BACKGROUND_JOBS') != b'0':
                raise Rejected('scheduled writer background jobs must be disabled')
            forbidden = (b'GLIBC_TUNABLES', b'GCONV_PATH', b'OPENSSL_CONF', b'OPENSSL_MODULES')
            if any(key.startswith((b'LD_', b'DYLD_')) or key in forbidden for key in env):
                raise Rejected('writer loader injection detected')
            lock = self.lock.lstat()
            if (not stat.S_ISREG(lock.st_mode) or lock.st_uid != 0 or lock.st_nlink != 1
                    or stat.S_IMODE(lock.st_mode) != 0o600):
                raise Rejected('runtime lock identity is unsafe')
            retained = 0
            for fd in (root / 'fd').iterdir():
                try:
                    info = fd.stat()
                    if (info.st_dev, info.st_ino) != (lock.st_dev, lock.st_ino):
                        continue
                    rows = [r.split() for r in (root / 'fdinfo' / fd.name).read_text().splitlines()
                            if r.startswith('lock:')]
                    if len(rows) == 1 and len(rows[0]) == 9 and rows[0][2:6] == [
                            'FLOCK', 'ADVISORY', 'READ', str(pid)] and rows[0][-2:] == ['0', 'EOF']:
                        retained += 1
                except FileNotFoundError:
                    continue
            if retained < 1:
                raise Rejected('writer does not retain its runtime shared lock')
        except OSError:
            raise Rejected('writer kernel identity unavailable') from None
        after = process_identity(self.proc, pid)
        if before != after:
            raise Rejected('writer changed during identity verification')
        return before
