from __future__ import annotations

import asyncio
import contextvars
import errno
import os
import stat
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


_held = contextvars.ContextVar('credential_leases', default={})


def _owner():
    try:
        return asyncio.current_task() or threading.get_ident()
    except RuntimeError:
        return threading.get_ident()


def _open(path: Path):
    if path.is_symlink():
        raise OSError(errno.EINVAL, 'credential source must not be a symlink')
    source = path.stat() if path.exists() else None
    if source is not None and not stat.S_ISREG(source.st_mode):
        raise OSError(errno.EINVAL, 'credential source must be regular')
    lock_path = path.with_name(path.name + '.refresh.lock')
    if lock_path.is_symlink():
        raise OSError(errno.EINVAL, 'credential lock must not be a symlink')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, 'unsafe credential lock')
        if os.name != 'nt':
            if info.st_mode & 0o077:
                raise OSError(errno.EPERM, 'unsafe credential lock permissions')
            if source is not None and os.geteuid() == 0:
                os.fchown(descriptor, source.st_uid, source.st_gid)
            elif source is not None and info.st_uid != source.st_uid:
                raise OSError(errno.EPERM, 'credential lock ownership mismatch')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _try_lock(descriptor):
    try:
        if os.name == 'nt':
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise


def _unlock(descriptor):
    if os.name == 'nt':
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@asynccontextmanager
async def acquire(path: Path, *, timeout: float = 35):
    key = str(path.absolute())
    if _held.get().get(key) == _owner():
        yield
        return
    descriptor = _open(path)
    locked = False
    token = None
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(descriptor):
            if time.monotonic() >= deadline:
                raise TimeoutError('credential refresh is busy; retry later')
            await asyncio.sleep(min(0.025,max(0,deadline-time.monotonic())))
            if time.monotonic()>=deadline:
                raise TimeoutError('credential refresh is busy; retry later')
        locked = True
        if time.monotonic()>deadline:
            raise TimeoutError('credential refresh acquisition exceeded its deadline')
        token = _held.set({**_held.get(), key: _owner()})
        yield
    finally:
        if token is not None:
            _held.reset(token)
        try:
            if locked:
                _unlock(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def mutation(path: Path, *, timeout: float = 0):
    key = str(path.absolute())
    if _held.get().get(key) == _owner():
        yield
        return
    descriptor = _open(path)
    locked = False
    token = None
    try:
        deadline=time.monotonic()+max(0,timeout)
        while not _try_lock(descriptor):
            if time.monotonic()>=deadline:
                raise OSError(errno.EBUSY, 'credential refresh is busy; retry later')
            # Only explicitly opted-in blocking workers use a wait budget.
            time.sleep(min(0.01,max(0,deadline-time.monotonic())))
        locked = True
        token = _held.set({**_held.get(), key: _owner()})
        yield
    finally:
        if token is not None:
            _held.reset(token)
        try:
            if locked:
                _unlock(descriptor)
        finally:
            os.close(descriptor)
