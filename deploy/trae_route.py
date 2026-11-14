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
ROUTE_GID = 10001


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


def _trusted_parent(target: Path) -> None:
    """Require an operator-controlled, non-writable route directory."""
    if os.name != 'posix':
        return
    if not target.is_absolute():
        raise ValueError('route file must be absolute')
    parent = target.parent
    try:
        info = parent.lstat()
    except OSError as exc:
        raise ValueError('route parent is unavailable') from exc
    if (not parent.is_dir() or parent.is_symlink()
            or info.st_uid not in (0, os.geteuid(), ROUTE_GID)
            or info.st_mode & 0o022):
        raise ValueError('unsafe route parent')


def _prepare_route_file(target: Path) -> None:
    """Make root-authored handoff readable only by the fixed runtime UID."""
    if os.name != 'posix':
        return
    try:
        if os.geteuid() == 0:
            os.chown(target, 0, ROUTE_GID)
            os.chmod(target, 0o640)
        else:
            # Local development/tests may run without root; retain a private
            # handoff rather than pretending to provide the host group contract.
            os.chmod(target, 0o600)
        with target.open('rb') as stream:
            os.fsync(stream.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ValueError('route permission contract failed') from exc


def write_route(path: str | os.PathLike[str], endpoint: str) -> str:
    target = Path(path)
    _trusted_parent(target)
    if target.is_symlink():
        raise ValueError('route file must not be a symlink')
    if not target.parent.is_dir():
        raise ValueError('route parent must already exist')
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
        _prepare_route_file(target)
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
