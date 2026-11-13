#!/usr/bin/env python3
"""Atomically publish a validated local Trae upstream endpoint.

The gateway reads this file as a small, non-secret handoff document.  The
writer intentionally accepts only HTTP loopback endpoints so an operator typo
or a tampered route cannot turn the gateway into an arbitrary network proxy.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_ROUTE_FILE = '/opt/workbuddy-manager/engine/var/trae_gateway_base'


def validate_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid loopback HTTP endpoint') from exc
    if (parsed.scheme != 'http' or hostname not in ('127.0.0.1', '::1')
            or port is None or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('endpoint must be http://127.0.0.1:<port> or http://[::1]:<port>')
    host = '[::1]' if hostname == '::1' else hostname
    return f'http://{host}:{port}'


def write_route(path: str | os.PathLike[str], endpoint: str) -> str:
    target = Path(path)
    if target.is_symlink():
        raise ValueError('route file must not be a symlink')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('route temporary file already exists')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o644)
        try:
            data = memoryview((endpoint + '\n').encode('ascii'))
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError('short route write')
                data = data[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
        if os.name == 'posix':
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return endpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('endpoint')
    parser.add_argument('--route-file', default=DEFAULT_ROUTE_FILE)
    args = parser.parse_args(argv)
    print(write_route(args.route_file, validate_endpoint(args.endpoint)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
