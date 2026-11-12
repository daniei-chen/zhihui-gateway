#!/usr/bin/env python3
"""Read-only audit for release and database snapshot retention.

The former cleanup command is intentionally retired.  ``audit`` produces a
reviewable keep/prune plan, but ``apply_prune`` always refuses and never
deletes production data.  Destructive retention needs a separately reviewed
operator procedure with an offline backup and an explicit maintenance window.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
from pathlib import Path
from typing import Iterable


PROD = Path("/opt/workbuddy-manager")
RELEASES = PROD / "releases"
DATA = PROD / "data"
BACKUPS = Path("/opt/workbuddy-manager/backups")
KEEP_SNAPSHOTS = 2
KEEP_RELEASES = 2

_VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:[+].*)?$")
_SNAPSHOT_SUFFIXES = (".db-wal", ".db-shm", ".db", ".gz")


def snapshot_key(name: str) -> str:
    """Map all SQLite snapshot members to one identity."""

    value = str(name)
    changed = True
    while changed:
        changed = False
        for suffix in _SNAPSHOT_SUFFIXES:
            if value.endswith(suffix) and len(value) > len(suffix):
                value = value[: -len(suffix)]
                changed = True
                break
    return value


def release_order_key(name: str) -> tuple:
    """Sort semantic product versions numerically; unknown names last."""

    match = _VERSION.match(str(name))
    if not match:
        return (0, 0, 0, 0, "")
    return (1, int(match.group(1)), int(match.group(2)), int(match.group(3)), str(name))


def denied(path: Path, _stat=None, _seen: set | None = None) -> str | None:
    """Return a reason when a path is outside the retired cleanup scope."""

    path = Path(path)
    name = path.name
    if name in {".budget-owners", "current", "current.next"}:
        return "protected_runtime_state"
    if name.endswith((".lock", ".refresh.lock", ".part", ".tmp")):
        return "active_or_transactional_state"
    if path.is_symlink():
        return "symlink"
    return None


def _item(path: Path, action: str, reason: str) -> dict:
    try:
        size = path.stat().st_size if path.is_file() else 0
        mtime = path.stat().st_mtime
    except OSError:
        size, mtime = 0, 0
    return {"path": str(path), "action": action, "reason": reason,
            "size": size, "mtime": mtime}


def _snapshot_items() -> list[dict]:
    if not DATA.is_dir():
        return []
    groups: dict[str, list[Path]] = {}
    for path in DATA.iterdir():
        if path.name == ".budget-owners":
            continue
        if path.is_file() and not path.is_symlink():
            groups.setdefault(snapshot_key(path.name), []).append(path)
    ordered = sorted(groups.items(),
                     key=lambda pair: max((p.stat().st_mtime for p in pair[1]), default=0),
                     reverse=True)
    keep = {key for key, _ in ordered[:KEEP_SNAPSHOTS]}
    result = []
    for key, paths in ordered:
        action = "keep" if key in keep else "prune"
        reason = "snapshot group retained" if action == "keep" else "snapshot group outside retention window"
        result.extend(_item(path, action, reason) for path in sorted(paths, key=lambda p: p.name))
    return result


def _release_items() -> list[dict]:
    if not RELEASES.is_dir():
        return []
    candidates = [p for p in RELEASES.iterdir() if p.is_dir() and not p.is_symlink()]
    ordered = sorted(candidates, key=lambda p: release_order_key(p.name), reverse=True)
    keep = {p.name for p in ordered[:KEEP_RELEASES]}
    result = []
    for path in ordered:
        action = "keep" if path.name in keep else "prune"
        reason = "release retention window" if action == "keep" else "older semantic release"
        result.append(_item(path, action, reason))
    return result


def audit() -> dict:
    """Return a deterministic, read-only retention plan."""

    never_touch = []
    owner_dir = DATA / ".budget-owners"
    if owner_dir.exists():
        never_touch.append(_item(owner_dir, "keep", "budget ownership state"))
    return {
        "schema": 1,
        "confirm_token": secrets.token_urlsafe(18),
        "active_release": str((RELEASES / "current").resolve()) if (RELEASES / "current").exists() else None,
        "groups": [
            {"name": "data-snapshots", "items": _snapshot_items()},
            {"name": "releases", "items": _release_items()},
            {"name": "never-touch", "items": never_touch},
        ],
        "destructive_apply": "retired",
    }


def apply_prune(report: dict, token: str, *, only: str | None = None,
                exclude: Iterable[str] | None = None) -> int:
    """Refuse all deletion; retained for compatibility with old callers."""

    return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        print("asset cleanup is retired; no files were changed")
        return 3
    if args.audit or not args.apply:
        print(json.dumps(audit(), ensure_ascii=False, indent=2))
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
