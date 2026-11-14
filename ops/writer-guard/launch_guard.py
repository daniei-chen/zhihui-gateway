"""Trusted same-PID writer launch boundary. Installation is a separate operation.

The lifecycle lock coordinates wrapped processes, not root or unwrapped legacy
writers. Exclusive state changes additionally require independent quiescence
validation. Never use this library as proof the first rollout is safe by itself.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import time

from state_guard import Guard, Paths, Rejected, VerifiedRelease

try:
    import fcntl
except ImportError:
    fcntl = None


def protected(path, paths, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(paths.root):
        raise Rejected('runtime path escapes fixed root')
    for current in (path, *path.parents):
        if paths.synthetic and not current.is_relative_to(paths.root):
            break
        try:
            info = current.lstat()
        except OSError:
            raise Rejected('protected runtime path unavailable') from None
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != paths.root_uid
                or os.name == 'posix' and info.st_mode & 0o022
                or (current != path or directory) and not stat.S_ISDIR(info.st_mode)):
            raise Rejected('unsafe runtime path')
    return path


@dataclass(frozen=True)
class Lease:
    fd: int
    pid: int
    inode: tuple
    exclusive: bool
    _issuer: object = field(repr=False, compare=False)


class RuntimeLock:
    def __init__(self, paths: Paths, quiescence=None):
        self.paths, self.quiescence = paths, quiescence
        self.path = paths.root / 'run/zhihui-writer-lifetime.lock'
        self._issuer, self._active = object(), {}

    @staticmethod
    def _inode(info):
        return info.st_dev, info.st_ino

    def _check_fd(self, fd):
        protected(self.path, self.paths)
        info, linked = os.fstat(fd), self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != self.paths.root_uid or stat.S_IMODE(info.st_mode) != 0o600
                or self._inode(info) != self._inode(linked)):
            raise Rejected('unsafe runtime lock inode')
        return self._inode(info)

    @contextmanager
    def acquire(self, *, exclusive=False, timeout=5.0):
        if fcntl is None or not 0 <= timeout <= 30:
            raise Rejected('bounded Linux runtime lock required')
        protected(self.path.parent, self.paths, directory=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            inode = self._check_fd(fd)
            deadline = time.monotonic() + timeout
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(fd, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Rejected('runtime writers prevent lock acquisition') from None
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            lease = Lease(fd, os.getpid(), inode, exclusive, self._issuer)
            self._active[fd] = lease
            self.validate(lease, exclusive=exclusive)
            yield lease
        finally:
            self._active.pop(fd, None)
            # Do not LOCK_UN: a surviving inherited FD must retain its lease.
            os.close(fd)

    def validate(self, permit, *, exclusive):
        if (type(permit) is not Lease or permit._issuer is not self._issuer
                or self._active.get(permit.fd) is not permit or permit.pid != os.getpid()
                or exclusive and not permit.exclusive):
            raise Rejected('runtime lease is not live in this process')
        try:
            inode = self._check_fd(permit.fd)
            info = Path('/proc/self/fdinfo/' + str(permit.fd)).read_text()
        except OSError:
            raise Rejected('runtime lease kernel identity unavailable') from None
        held = []
        for line in info.splitlines():
            fields = line.split()
            if len(fields) == 9 and fields[:1] == ['lock:'] and fields[2:4] == ['FLOCK', 'ADVISORY']:
                held.append(fields)
        mode = 'WRITE' if permit.exclusive else 'READ'
        if (inode != permit.inode or len(held) != 1 or held[0][4] != mode
                or held[0][5] != str(permit.pid) or held[0][-2:] != ['0', 'EOF']):
            raise Rejected('runtime kernel flock is not retained')
        if exclusive:
            if not callable(getattr(self.quiescence, 'validate', None)):
                raise Rejected('independent writer quiescence proof required')
            if self.quiescence.validate(permit) is not None:
                raise Rejected('quiescence validator must raise, not return a flag')


_PYTHON_ENV = {
    'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONSAFEPATH': '1',
    'PYTHONPYCACHEPREFIX': '/opt/zhihui-release/python-cache',
    'PYTHONPATH': '/opt/workbuddy-manager',
}

# The host bootstrap starts as root so it can verify the signed release and
# then drops the application to this fixed identity. Keep the two sensitive
# bootstrap files in the same ownership contract before the child starts;
# otherwise a root-side migration or repair can leave a 0600 root-owned
# users.json that turns every login into an HTTP 500 after the drop.
SERVICE_UID = 10001
SERVICE_GID = 10001
SERVICE_DATA_ROOT = Path('/opt/workbuddy-manager/data')
SERVICE_DATA_FILES = ('users.json', 'initial-admin-password.txt')


def normalize_service_data(root=SERVICE_DATA_ROOT, *, uid=SERVICE_UID, gid=SERVICE_GID):
    """Repair the small root/app handoff surface before dropping privileges.

    Missing files are valid during first bootstrap. Existing entries must be
    regular files owned by the runtime identity and private to that identity;
    symlinks, hard-link surprises, unsafe directories, or failed ownership
    changes reject startup before the app can serve a request.
    """
    root = Path(root)
    try:
        info = root.lstat()
    except OSError as exc:
        raise Rejected('service data directory unavailable') from exc
    # A normal directory has at least the `.` and `..` links; unlike a
    # regular file, its link count is not a hard-link safety contract.
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_nlink < 2):
        raise Rejected('service data directory is unsafe')
    try:
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.chown(root, uid, gid)
        os.chmod(root, 0o700)
        checked_root = root.lstat()
        if ((checked_root.st_uid, checked_root.st_gid) != (uid, gid)
                or stat.S_IMODE(checked_root.st_mode) != 0o700):
            raise Rejected('service data directory contract failed')
        for name in SERVICE_DATA_FILES:
            path = root / name
            try:
                item = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise Rejected('service data entry unavailable') from exc
            if (stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode)
                    or item.st_nlink != 1):
                raise Rejected('service data entry is unsafe')
            if (item.st_uid, item.st_gid) != (uid, gid):
                os.chown(path, uid, gid)
            os.chmod(path, 0o600)
            checked = path.lstat()
            if ((checked.st_uid, checked.st_gid) != (uid, gid)
                    or stat.S_IMODE(checked.st_mode) != 0o600):
                raise Rejected('service data ownership contract failed')
    except OSError as exc:
        raise Rejected('service data ownership contract failed') from exc


def writer_environment(source, release, *, helper=False, reconcile=False):
    if type(helper) is not bool:
        raise Rejected('fixed writer profile required')
    if type(reconcile) is not bool or helper and reconcile:
        raise Rejected('fixed writer profile required')
    for name, value in source.items():
        if (not isinstance(name, str) or not isinstance(value, str)
                or '\0' in name or '\0' in value):
            raise Rejected('invalid runtime environment')
        if (name.startswith(('LD_', 'DYLD_')) or name in
                {'GLIBC_TUNABLES', 'GCONV_PATH', 'OPENSSL_CONF', 'OPENSSL_MODULES',
                 'BASH_ENV', 'ENV', 'UVICORN_APP', 'WEB_CONCURRENCY'}):
            raise Rejected('runtime loader override rejected')
        if name.startswith('PYTHON') and _PYTHON_ENV.get(name) != value:
            raise Rejected('Python runtime override rejected')
    # Do not forward arbitrary inherited variables. Schema validation in the
    # application still rejects unknown WB_* configuration names.
    env = {k: v for k, v in source.items() if k.startswith(('WB_', 'WB2API_'))}
    env.update({'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8',
                'LC_ALL': 'C.UTF-8', **_PYTHON_ENV,
                'PYTHONPATH': str(release.root), 'WB_DATA_DIR': '/opt/workbuddy-manager/data',
                'WB_DB': '/opt/workbuddy-manager/data/manager.db',
                'WB_USERS_FILE': '/opt/workbuddy-manager/data/users.json',
                'WB_STATIC_DIR': str(release.root / 'web/out'),
                'WB_MANAGER_HOST': '127.0.0.1', 'WB_MANAGER_PORT': '7865' if helper else '7866',
                # Both generations must use the same admission ledger before
                # any relay switch.  The helper remains read/serve-only below;
                # this flag does not grant it background ownership.
                'WB_SHARED_ADMISSION': '1'})
    if helper or reconcile:
        env['WB_BACKGROUND_JOBS'] = '0'
    return env


def writer_command(*, helper=False, reconcile=False):
    if type(helper) is not bool or type(reconcile) is not bool or helper and reconcile:
        raise Rejected('fixed writer profile required')
    prefix = ['/usr/bin/setpriv', '--reuid=10001', '--regid=10001', '--groups=114',
            '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs',
            '/opt/workbuddy-manager/venv/bin/python', '-P', '-s', '-B', '-X',
            'pycache_prefix=/opt/zhihui-release/python-cache', '-m']
    if reconcile:
        return prefix + ['server.budget_reconcile']
    return prefix + ['server.serve', '--host', '127.0.0.1',
            '--port', '7865' if helper else '7866',
            '--timeout-graceful-shutdown', '75']


class Launcher:
    def __init__(self, guard: Guard, lock: RuntimeLock):
        if guard.lock is not lock or guard.paths != lock.paths:
            raise Rejected('launch and state guard must share one lease authority')
        self.guard, self.lock = guard, lock

    def _target(self, exact):
        paths = self.guard.paths
        if exact is not None:
            return Path(exact)
        pointer = paths.releases / 'current'
        protected(pointer.parent, paths, directory=True)
        info = pointer.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != paths.root_uid:
            raise Rejected('protected current release pointer required')
        target = pointer.resolve(strict=True)
        if target.parent != paths.releases:
            raise Rejected('release pointer escapes releases')
        return target

    @contextmanager
    def admitted(self, exact=None):
        with self.lock.acquire() as lease:
            release = self.guard.inspect_release(self._target(exact))
            self.guard.require_launch(release, self.guard.observe(lease), self.guard.load_floor())
            self.lock.validate(lease, exclusive=False)
            yield release, lease

    def execute(self, release: VerifiedRelease, lease: Lease, source, *, helper=False, reconcile=False):
        if self.guard.paths.synthetic:
            raise Rejected('synthetic tree cannot execute production writer')
        if getattr(self.guard.verifier, 'inspect_legacy', False):
            raise Rejected('legacy inspection authority cannot execute writers')
        self.lock.validate(lease, exclusive=False)
        if self.guard.inspect_release(release.root) != release:
            raise Rejected('admitted release changed before execution')
        self.guard.require_launch(release, self.guard.observe(lease), self.guard.load_floor())
        env = writer_environment(source, release, helper=helper, reconcile=reconcile)
        argv = writer_command(helper=helper, reconcile=reconcile)
        # Runtime verification (venv closure and source/static closure) belongs
        # to the supplied trusted verifier, before any executable is admitted.
        self.lock.validate(lease, exclusive=False)
        normalize_service_data()
        os.chdir(release.root)
        os.set_inheritable(lease.fd, True)
        os.execve(argv[0], argv, env)
