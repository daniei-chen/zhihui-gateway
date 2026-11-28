"""一个数据目录只允许一个服务进程（实例租约）。

为什么需要：`filelock` 的互斥表是**进程内**的 `RLock`（`server/services/filelock.py:23`），
只有 `writing=True` 才落到内核锁上；限额、准入、密钥槽位同样按进程各记一份。于是
同一份 `data/` 上跑起第二个实例时：

  · 账号文件的「读—改—写」重新变成两个写者交错 —— 正是 issue #210（proxy 被刷新
    冲掉）与 #187（weight 被清回）的形状，只是换成了跨进程；
  · 限额按进程放大 N 倍（N 个实例各算一份预算）。

`server/serve.py` 的契约一直是单进程（`uvicorn.Config(..., workers=1)`），但这个
契约只写在代码里 —— 没有任何东西拦住第二次启动。本模块把它变成可执行的约束：
拿不到租约就拒绝启动，并把**谁占着**（pid/主机/启动时刻）写进日志。

实现与 `server/userstore.py` 的 `locked()` 同风格 —— 拒绝符号链接、`nlink == 1`
校验、POSIX `flock` / Windows `msvcrt.locking` 非阻塞独占。区别有两点：它是
**进程生命周期**的（不是临界区），而且**分成两个文件**：

    instance.lease    只用来加锁，从不读取
    instance.holder   纯文本，持有者在拿到锁之后写入

为什么拆开：Windows 的字节区间锁会连**读**一起挡住 —— 锁住第 0 字节后，别的进程
`read_text()` 直接拿到 `Permission denied`，被拒绝的一方只能报「未知进程」。把
元数据放进不加锁的文件，保留锁文件独占访问，两边互不干扰。
"""
from __future__ import annotations

import json
import os
import socket
import stat
import time
from pathlib import Path

LEASE_NAME = 'instance.lease'
HOLDER_NAME = 'instance.holder'


class LeaseBusy(RuntimeError):
    """另一个实例正持有租约。消息里带持有者信息，供启动日志直接打印。"""


def lease_path(directory: Path) -> Path:
    """锁文件放在数据目录里：跟着数据走，备份/迁移数据目录时一起搬。"""
    return Path(directory).absolute() / LEASE_NAME


def holder_path(directory: Path) -> Path:
    return Path(directory).absolute() / HOLDER_NAME


def _holder_text(directory: Path) -> str:
    path = holder_path(directory)
    try:
        info = json.loads(path.read_text(encoding='utf-8') or '{}')
    except (OSError, ValueError):
        return '未知进程（holder 文件不可读）'
    started = info.get('started')
    when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)) if started else '未知时刻'
    return 'pid=%s host=%s 启动于 %s' % (info.get('pid') or '?', info.get('host') or '?', when)


def _lock(handle) -> None:
    """非阻塞独占；拿不到由调用方解释成「有别的实例」。"""
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Lease:
    """进程级独占租约；`release()` 幂等，也可以当上下文管理器用。"""

    def __init__(self, directory: Path, handle) -> None:
        self.directory = directory
        self._handle = handle

    def __enter__(self) -> 'Lease':
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()


def acquire(directory: Path) -> Lease:
    """抢占数据目录的实例租约。已被活着的进程占用时抛 `LeaseBusy`。

    残留文件（上一个进程被杀、没走正常释放）不算占用：内核锁随文件描述符关闭
    自动释放，所以这里依然能拿到，并覆盖 holder 里的信息。
    """
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=True)
    path = lease_path(directory)
    if path.is_symlink():
        raise RuntimeError(f'实例租约不能是符号链接：{path}')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    handle = os.fdopen(descriptor, 'r+')
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f'实例租约文件不安全（nlink={info.nlink}）：{path}')
        try:
            _lock(handle)
        except OSError:
            raise LeaseBusy('另一个实例正在使用 %s（%s）'
                            % (directory, _holder_text(directory))) from None
        holder = holder_path(directory)
        holder.write_text(json.dumps({'pid': os.getpid(), 'host': socket.gethostname(),
                                      'started': time.time()}), encoding='utf-8')
        return Lease(directory, handle)
    except BaseException:
        handle.close()
        raise
