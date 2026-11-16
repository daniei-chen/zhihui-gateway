"""Switch one release pointer, and restore it if readiness fails.

The caller owns service stop/start and validates signed content before entry.
This module never restores databases, account tokens or private configuration.
"""
import os
from pathlib import Path
import secrets


def point(pointer, target):
    pointer = Path(pointer)
    stage = pointer.with_name('.%s.%s.next' % (pointer.name, secrets.token_hex(16)))
    if stage.exists() or stage.is_symlink():
        raise RuntimeError('unfinished pointer transaction retained')
    created = False
    try:
        stage.symlink_to(target, target_is_directory=True)
        created = True
        os.replace(stage, pointer)
        fd = os.open(pointer.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if created:
            try:
                stage.unlink()
            except OSError:
                pass


def _healthy(check):
    try:
        return check() is True
    except BaseException:
        return False


def activate(pointer, target, stop, start, healthy):
    pointer = Path(pointer)
    if not pointer.is_symlink():
        raise RuntimeError('current release pointer missing')
    previous = os.readlink(pointer)
    stop()

    try:
        point(pointer, target)
        start()
        if not _healthy(healthy):
            raise RuntimeError('candidate readiness failed')
    except BaseException as candidate_error:
        rollback_errors = []
        try:
            stop()
        except BaseException as exc:
            rollback_errors.append(exc)

        try:
            point(pointer, previous)
        except BaseException as exc:
            rollback_errors.append(exc)

        try:
            start()
        except BaseException as exc:
            rollback_errors.append(exc)

        if not _healthy(healthy):
            rollback_errors.append(RuntimeError('rollback readiness failed; operator action required'))
        if rollback_errors:
            detail = '; '.join('%s: %s' % (type(exc).__name__, exc) for exc in rollback_errors)
            raise RuntimeError(
                'release activation failed and rollback did not fully verify: ' + detail
            ) from candidate_error
        raise

    return previous
