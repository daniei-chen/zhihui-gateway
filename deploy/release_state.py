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
    os.replace(staged,pointer)
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
        # 回滚路径**必须尽最大努力把服务重新拉起来**，即使指针恢复本身也失败。
        #
        # 原先这里写的是 `point(...); start()`：`point` 抛异常时 `start()` 根本
        # 不执行，服务就停在「已停止」状态 —— 指针可能还指向坏版本，而且没人再
        # 启动它。这不是「回滚失败」，是**回滚把服务留在停机态**。
        #
        # 现在无论 `point` 成败都调 `start()`；两者都失败时一并报告，交给运维。
        pointer_error=None
        try:
            point(pointer,previous)
        except BaseException as exc:          # noqa: BLE001 - 要连异常一起报出去
            pointer_error=exc
        start()
        if not healthy():raise RuntimeError('rollback readiness failed; operator action required')
        if pointer_error is not None:
            # 保留**原始异常类型**，只是补一句说明：调用方按 `OSError` 等具体类型
            # 分支处理时不该因为这里换了类型而走错路径。
            note = ('release pointer could not be restored; service was restarted on the '
                    'previously linked release but the pointer may still name the failed one')
            try:
                pointer_error.add_note(note)
            except AttributeError:            # Python < 3.11
                pass
            raise pointer_error
        raise
    return previous
