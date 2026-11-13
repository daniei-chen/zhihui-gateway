#!/usr/bin/env python3
"""Create the signed runtime closure manifest for a staged release.

The GitHub artifact is assembled before host signing.  This tool binds the
actual ``server/`` and ``deploy/`` bytes present in that staged directory to a
detached Ed25519 signature; it never follows symlinks or includes its own
manifest/signature.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


SAFE_MODES = {0o400, 0o440, 0o444, 0o600, 0o640, 0o644, 0o700, 0o750, 0o755}
EXCLUDED = {"deploy/managed-release.json", "deploy/managed-release.sig"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


def build(root: Path) -> dict:
    root = root.resolve(strict=True)
    if root.name.startswith(".") or not root.is_dir():
        raise ValueError("staged release directory required")
    files: dict[str, dict[str, int | str]] = {}
    for directory in (root / "server", root / "deploy"):
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("runtime closure directory missing or symlinked: " + str(directory))
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if relative in EXCLUDED or "__pycache__" in path.relative_to(directory).parts:
                continue
            information = path.lstat()
            if stat.S_ISDIR(information.st_mode):
                continue
            if path.is_symlink() or not stat.S_ISREG(information.st_mode):
                raise ValueError("runtime closure contains unsafe entry: " + relative)
            mode = stat.S_IMODE(information.st_mode)
            if mode not in SAFE_MODES:
                raise ValueError("runtime closure mode is not allow-listed: " + relative)
            files[relative] = {"candidate_sha256": digest(path), "mode": mode}
    if not files or "server/main.py" not in files or "deploy/source_preflight.py" not in files:
        raise ValueError("runtime closure is incomplete")
    return {
        "at": "host-staged",
        "database_rollback": False,
        "files": dict(sorted(files.items())),
        "release_id": root.name,
        "signature_algorithm": "ed25519",
        "signature_required": True,
        "version": root.name,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: create-managed-release.py RELEASE_DIR")
    root = Path(argv[0]).resolve(strict=True)
    target = root / "deploy/managed-release.json"
    temporary = target.with_name(".managed-release.json.tmp")
    value = build(root)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
    print(json.dumps({"release": value["release_id"], "files": len(value["files"]),
                      "manifest": str(target)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
