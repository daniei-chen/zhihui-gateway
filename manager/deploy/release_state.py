"""Switch one release pointer, and restore it if readiness fails.

The caller owns service stop/start and validates signed content before entry.
This module never restores databases, account tokens or private configuration.
"""
import os
from pathlib import Path

def point(pointer, target):
    pointer=Path(pointer)
    staged=pointer.with_name(pointer.name+'.next')
    if staged.exists() or staged.is_symlink():
        raise RuntimeError('unfinished pointer transaction retained')
    staged.symlink_to(target, target_is_directory=True)
    owned = staged.lstat()
    try:
        os.replace(staged,pointer)
    except BaseException:
        # Only remove the exact staging inode created by this call. Unknown
        # pre-existing transactions are never discarded or adopted.
        try:
            actual = staged.lstat()
            if (actual.st_dev, actual.st_ino) == (owned.st_dev, owned.st_ino):
                staged.unlink()
        except FileNotFoundError:
            pass
        raise
    fd=os.open(pointer.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def activate(pointer,target,stop,start,healthy):
    pointer=Path(pointer)
    if not pointer.is_symlink():raise RuntimeError('current release pointer missing')
    previous=os.readlink(pointer)
    stop()
    try:
        point(pointer,target)
        start()
        if not healthy():raise RuntimeError('candidate readiness failed')
    except BaseException:
        stop()
        rollback_durability = None
        if not pointer.is_symlink() or os.readlink(pointer) != previous:
            try:
                point(pointer,previous)
            except BaseException as exc:
                if not pointer.is_symlink() or os.readlink(pointer)!=previous:
                    raise RuntimeError('rollback pointer failed; operator action required') from exc
                rollback_durability=exc
        start()
        if not healthy():raise RuntimeError('rollback readiness failed; operator action required')
        if rollback_durability:
            raise RuntimeError('old service restarted; rollback pointer durability unconfirmed') from rollback_durability
        raise
    return previous
