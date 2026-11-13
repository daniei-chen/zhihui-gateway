#!/usr/bin/env python3
"""Atomic operator-controlled route switch for the inference relay."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from .cohort_rollout import Refused, atomic_json
except ImportError:  # direct ``python deploy/relay_route.py`` execution
    from cohort_rollout import Refused, atomic_json


TARGETS = frozenset({"old", "new"})
RELAY_UID = 10001
RELAY_GID = 10001


def _trusted_parent(route: Path) -> None:
    if not route.is_absolute():
        raise Refused("route_must_be_absolute")
    parent = route.parent
    try:
        info = parent.lstat()
    except OSError as exc:
        raise Refused("route_parent_unavailable") from exc
    if (not info or not parent.is_dir() or parent.is_symlink()
            or os.name == "posix" and (info.st_uid not in (0, os.geteuid(), RELAY_UID)
                                        or info.st_mode & 0o022)):
        raise Refused("unsafe_route_directory")


def _prepare_route_file(route: Path) -> None:
    if os.name != "posix":
        return
    try:
        if os.geteuid() == 0:
            os.chown(route, 0, RELAY_GID)
            os.chmod(route, 0o640)
        else:
            os.chmod(route, 0o600)
        with route.open("rb") as stream:
            os.fsync(stream.fileno())
        directory_fd = os.open(route.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise Refused("route_permission_contract_failed") from exc


def read_route(path: str | Path) -> dict:
    route = Path(path)
    _trusted_parent(route)
    if route.is_symlink() or not route.is_file() or route.stat().st_size > 4096:
        raise Refused("route_missing_or_unsafe")
    try:
        value = json.loads(route.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused("route_invalid") from exc
    if (set(value) != {"epoch", "target"} or type(value["epoch"]) is not int
            or value["epoch"] < 1 or value["target"] not in TARGETS):
        raise Refused("route_invalid")
    return value


def switch(path: str | Path, target: str) -> dict:
    if target not in TARGETS:
        raise Refused("invalid_target")
    route = Path(path)
    _trusted_parent(route)
    if route.exists() or route.is_symlink():
        current = read_route(route)
        if current["target"] == target:
            _prepare_route_file(route)
            return current
        epoch = current["epoch"] + 1
    else:
        epoch = 1
    value = {"epoch": epoch, "target": target}
    atomic_json(route, value)
    _prepare_route_file(route)
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("--route-file", default="/opt/workbuddy-manager/data/relay-route.json")
    args = parser.parse_args(argv)
    print(json.dumps(switch(args.route_file, args.target), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
