"""Managed single-worker launcher; pre-stop drain is separate from SIGTERM.

Run: python -m server.serve --host 127.0.0.1 --port 7864
Use SIGUSR1 to begin pre-stop drain and SIGUSR2 to abort it on POSIX. A release
controller must observe in-flight=0 before SIGTERM; otherwise abort the release.
"""
from __future__ import annotations

import argparse
import math
import os
import signal

import uvicorn

from . import drain


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
    cfg = uvicorn.Config('server.main:app', host=args.host, port=args.port,
                         workers=1, timeout_graceful_shutdown=args.timeout_graceful_shutdown)
    DrainServer(cfg).run()


if __name__ == '__main__':
    main()
