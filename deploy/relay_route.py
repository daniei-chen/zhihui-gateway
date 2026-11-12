#!/usr/bin/env python3
"""Atomic operator-controlled route switch for the inference relay."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .cohort_rollout import Refused, atomic_json
except ImportError:  # direct ``python deploy/relay_route.py`` execution
    from cohort_rollout import Refused, atomic_json


TARGETS = frozenset({"old", "new"})


def read_route(path: str | Path) -> dict:
    route = Path(path)
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
    if route.exists() or route.is_symlink():
        current = read_route(route)
        if current["target"] == target:
            return current
        epoch = current["epoch"] + 1
    else:
        epoch = 1
    value = {"epoch": epoch, "target": target}
    atomic_json(route, value)
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
