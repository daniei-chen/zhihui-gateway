"""Fixed ordered publication/backup locks for the migration controller."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from launch_guard import protected
from state_guard import Rejected

try:
    import fcntl
except ImportError:
    fcntl = None


@dataclass(frozen=True)
class Ownership:
    pid: int
    descriptors: tuple
    _issuer: object = field(repr=False, compare=False)


class Coordination:
    def __init__(self, paths):
        self.paths = paths
        self.files = (paths.root / 'run/zh-release.lock',
                      paths.root / 'root/backups/zhihui-v2/backup.lock')
        self._issuer, self._live = object(), None

    @contextmanager
    def acquire(self):
        if fcntl is None or self._live is not None:
            raise Rejected('Linux non-reentrant publication ownership required')
        opened = []
        try:
            for index, path in enumerate(self.files):
                protected(path.parent, self.paths, directory=True)
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
                # Backup lock is pre-existing: never create a second authority
                # because its configured inode/path was missing.
                if index == 0:
                    flags |= os.O_CREAT
                fd = os.open(path, flags, 0o600)
                opened.append((path, fd))
                self._safe(path, fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Rejected('publication or backup operation is already active') from None
            owner = Ownership(os.getpid(), tuple((str(path), fd, self._safe(path, fd))
                                                 for path, fd in opened), self._issuer)
            self._live = owner
            self.validate(owner)
            yield owner
        finally:
            self._live = None
            for _, fd in reversed(opened):
                os.close(fd)

    def _safe(self, path, fd):
        protected(path, self.paths)
        info, linked = os.fstat(fd), path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != self.paths.root_uid
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
            raise Rejected('unsafe publication or backup lock inode')
        return info.st_dev, info.st_ino

    def validate(self, owner):
        if (type(owner) is not Ownership or owner is not self._live
                or owner._issuer is not self._issuer or owner.pid != os.getpid()):
            raise Rejected('publication and backup ownership is not live')
        for path, fd, inode in owner.descriptors:
            if self._safe(Path(path), fd) != inode:
                raise Rejected('coordination inode changed')
            rows = [line.split() for line in Path('/proc/self/fdinfo/' + str(fd)).read_text().splitlines()
                    if line.startswith('lock:')]
            if (len(rows) != 1 or len(rows[0]) != 9 or rows[0][2:6] !=
                    ['FLOCK', 'ADVISORY', 'WRITE', str(owner.pid)] or rows[0][-2:] != ['0', 'EOF']):
                raise Rejected('coordination kernel lease lost')
