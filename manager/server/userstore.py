"""Serialize users.json read-modify-write across local threads and workers."""
from __future__ import annotations

import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_mutex = threading.RLock()
_local = threading.local()


@contextmanager
def locked(target: Path):
    target = Path(target).absolute()
    with _mutex:
        if getattr(_local, 'target', None) == target:
            yield
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name('.' + target.name + '.lock')
        if lock_path.is_symlink():
            raise RuntimeError('unsafe user-store lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+b') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError('unsafe user-store lock')
            if info.st_size == 0:
                handle.write(b'1'); handle.flush()
            deadline = time.monotonic() + 5
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('用户存储正在修改，请稍后重试') from None
                    time.sleep(0.02)
            _local.target = target
            try:
                yield
            finally:
                _local.target = None
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
