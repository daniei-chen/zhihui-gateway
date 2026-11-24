"""Managed single-worker launcher; pre-stop drain is separate from SIGTERM.

Run: python -m server.serve --host 127.0.0.1 --port 7864
Use SIGUSR1 to begin pre-stop drain and SIGUSR2 to abort it on POSIX. A release
controller must observe in-flight=0 before SIGTERM; otherwise abort the release.

## systemd socket activation（可选，装了就不再有重启 502）

服务启动时如果检测到 systemd 传下来的监听 socket（`LISTEN_FDS`/`LISTEN_PID`，
fd 固定从 3 开始），就直接用它，不再自己 bind。

为什么值得装：现在每次 `systemctl restart` 都有几秒窗口，内核里没有进程在监听
7866，新连接被直接拒绝，nginx 记 502 并回给客户端 —— 2026-11-24 的两次升级实测
产生了 7 条 `/v1/chat/completions` 502。用 socket activation 后**监听 socket 一直
由 systemd 持有**，重启期间连接留在内核 backlog 里排队（默认 4096），新进程起来
后立刻被 accept，客户端只是慢一点、不会失败。

没有这套变量时行为完全不变（自己 bind host/port），所以装与不装都安全。
"""
from __future__ import annotations

import argparse
import math
import os
import signal
import socket

import uvicorn

from . import drain

#: systemd 传递的监听 socket 从 fd 3 开始（0/1/2 是标准输入输出错误）。
SD_LISTEN_FDS_START = 3


def inherited_listener() -> socket.socket | None:
    """取出 systemd 传来的监听 socket；没有则返回 None。

    三个细节都是踩过坑的：
    * `LISTEN_PID` 必须等于当前进程号。否则说明变量是从父进程继承来的（shell、
      systemd-run、容器入口脚本都可能），此时 fd 3 不是监听 socket，硬用会崩。
    * 这些变量读取后立刻清掉，避免再传给子进程（比如 docker 客户端、登录脚本），
      让"我拿到了激活 socket"这个契约只在本进程内成立。
    * fd 数量不是恰好 1 个就直接报错退出，不去猜该用哪个 —— 猜错就是把服务挂到
      了错误的地址上。
    """
    pid = os.environ.get('LISTEN_PID', '').strip()
    fds = os.environ.get('LISTEN_FDS', '').strip()
    os.environ.pop('LISTEN_PID', None)
    os.environ.pop('LISTEN_FDS', None)
    os.environ.pop('LISTEN_FDNAMES', None)
    if not pid or not fds:
        return None
    if pid != str(os.getpid()):
        return None
    if fds != '1':
        raise SystemExit(
            f'socket activation: expected exactly one listener, got {fds!r}')
    # socket.fromfd 会 dup 一份，原 fd 仍归 systemd 管，本进程退出时不影响它。
    listener = socket.fromfd(SD_LISTEN_FDS_START, socket.AF_INET, socket.SOCK_STREAM)
    listener.set_inheritable(False)
    return listener


class DrainServer(uvicorn.Server):
    def handle_exit(self, sig, frame):
        drain.begin(shutdown=True)
        super().handle_exit(sig, frame)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--timeout-graceful-shutdown', type=float,
                        default=float(os.environ.get('WB_GRACEFUL_SHUTDOWN_S', '650')))
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_graceful_shutdown) or args.timeout_graceful_shutdown <= 0:
        parser.error('graceful shutdown must have a positive bounded duration')
    from . import config
    from .configcontract import validate_environment
    validate_environment()
    if args.host is None:
        args.host=config.HOST if os.environ.get('WB_MANAGER_HOST','').strip() else '127.0.0.1'
    if args.port is None:
        args.port=config.PORT
    if not 0 <= args.port <= 65535:parser.error('port must be between 0 and 65535')
    if args.timeout_graceful_shutdown < config.DRAIN_TIMEOUT_SEC:
        parser.error('graceful shutdown must be at least WB_DRAIN_TIMEOUT_SEC')
    config.LAUNCH_ARGUMENTS={'host':args.host,'port':args.port,
                            'graceful_shutdown_s':args.timeout_graceful_shutdown}
    if hasattr(signal, 'SIGUSR1'):
        signal.signal(signal.SIGUSR1, lambda *_: drain.begin())
        signal.signal(signal.SIGUSR2, lambda *_: drain.resume())
    listener = inherited_listener()
    if listener is not None:
        # host/port 不参与：监听 socket 已经 bind 好了，uvicorn 直接把 fd 收下。
        cfg = uvicorn.Config('server.main:app', workers=1,
                             timeout_graceful_shutdown=args.timeout_graceful_shutdown)
        DrainServer(cfg).run(sockets=[listener])
    else:
        cfg = uvicorn.Config('server.main:app', host=args.host, port=args.port,
                             workers=1, timeout_graceful_shutdown=args.timeout_graceful_shutdown)
        DrainServer(cfg).run()


if __name__ == '__main__':
    main()
