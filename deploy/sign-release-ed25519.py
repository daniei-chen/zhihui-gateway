#!/usr/bin/env python3
"""Safe Ed25519 manifest signer used by the managed release process.

Validation is completed before any output is replaced.  A failed signing or
verification operation leaves the previous detached signature untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile


SAFE_MODES = {0o400, 0o440, 0o444, 0o600, 0o640, 0o644, 0o700, 0o750, 0o755}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _fail(message: str, code: int = 4):
    raise SystemExit(code)


def _root(manifest: Path) -> Path:
    if manifest.name != "managed-release.json" or manifest.parent.name != "deploy":
        _fail("manifest must be deploy/managed-release.json")
    return manifest.parent.parent.resolve()


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("unsafe release member")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or path.as_posix() != value
            or value in ("deploy/managed-release.json", "deploy/managed-release.sig")):
        _fail("unsafe release member")
    return value


def _member(root: Path, relative: str) -> Path:
    relative = _relative(relative)
    path = root / Path(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            _fail("release member missing")
        if stat.S_ISLNK(info.st_mode):
            _fail("release member symlink")
    if not path.is_file() or path.is_symlink():
        _fail("release member is not a regular file")
    return path


def _mode(path: Path) -> int:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode not in SAFE_MODES:
        _fail("release member has unsafe mode")
    return mode


def _load(manifest: Path) -> dict:
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fail("invalid manifest")
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        _fail("invalid manifest")
    return value


def _validate_existing(root: Path, value: dict) -> None:
    for relative, item in value["files"].items():
        if not isinstance(item, dict) or set(item) - {"candidate_sha256", "mode", "sha256"}:
            _fail("invalid manifest member")
        path = _member(root, relative)
        mode = _mode(path)
        expected_mode = item.get("mode")
        if (type(expected_mode) is not int or expected_mode not in SAFE_MODES
                or mode != expected_mode):
            _fail("manifest member drift")


def _build_value(manifest: Path, additions: list[str]) -> dict:
    root = _root(manifest)
    value = _load(manifest)
    _validate_existing(root, value)
    files = dict(value["files"])
    # Rebuild the digest for every existing member.  A stale digest is a
    # repairable manifest drift; a mode/path drift remains a hard refusal.
    for relative, item in list(files.items()):
        path = _member(root, relative)
        files[relative] = {"candidate_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "mode": item["mode"]}
    for relative in additions:
        path = _member(root, relative)
        mode = _mode(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[relative] = {"candidate_sha256": digest, "mode": mode}
    value["files"] = dict(sorted(files.items()))
    value["signature_algorithm"] = "ed25519"
    value["signature_required"] = True
    return value


def resolve_key(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("WB_SIGN_KEY", "")
    if not value:
        raise SystemExit(2)
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise SystemExit(2)
    return path


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def sign_file(key: Path, manifest: Path, signature: Path, *, trust: Path | None = None) -> None:
    """Sign to a temporary file and replace the detached signature last."""

    signature.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="zhihui-sign-") as directory:
        output = Path(directory) / "signature"
        result = subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key),
             "-in", str(manifest), "-out", str(output)],
            capture_output=True,
        )
        if result.returncode or not output.is_file():
            raise SystemExit(3)
        signed = output.read_bytes()
        if trust is not None:
            verify_temp = Path(directory) / "verify"
            verify_temp.write_bytes(signed)
            result = subprocess.run(
                ["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey",
                 str(trust), "-in", str(manifest), "-sigfile", str(verify_temp)],
                capture_output=True,
            )
            if result.returncode:
                raise SystemExit(3)
        _write_atomic(signature, signed, 0o644)


def verify_file(trust: Path, manifest: Path, signature: Path) -> None:
    result = subprocess.run(
        ["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(trust),
         "-in", str(manifest), "-sigfile", str(signature)],
        capture_output=True,
    )
    if result.returncode:
        raise SystemExit(3)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--add", action="append", default=[])
    parser.add_argument("--sign-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--key")
    parser.add_argument("--trust")
    args = parser.parse_args(argv)
    manifest = Path(args.manifest)
    root = _root(manifest)
    if args.sign_only and args.add:
        _fail("--sign-only cannot be combined with --add")
    value = None if args.sign_only else _build_value(manifest, args.add)
    if args.dry_run:
        return 0
    key = resolve_key(args.key)
    signature = manifest.with_suffix(".sig")
    if args.sign_only:
        sign_file(key, manifest, signature, trust=Path(args.trust) if args.trust else None)
        return 0
    temporary_manifest = manifest.with_name(f".{manifest.name}.{os.getpid()}.tmp")
    temporary_signature = manifest.with_name(f".{signature.name}.{os.getpid()}.tmp")
    try:
        raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _write_atomic(temporary_manifest, raw, stat.S_IMODE(manifest.stat().st_mode))
        sign_file(key, temporary_manifest, temporary_signature, trust=Path(args.trust) if args.trust else None)
        os.replace(temporary_manifest, manifest)
        os.replace(temporary_signature, signature)
    except BaseException:
        try:
            temporary_manifest.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            temporary_signature.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
