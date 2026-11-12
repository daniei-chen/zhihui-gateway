#!/usr/bin/env python3
"""Offline restore evidence drill.

The command decrypts into a temporary file, verifies the archive receipt, and
only then restores the non-sensitive recovery sidecar.  It never claims a
complete restore when SQLite verification was skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile


WORK = Path("/opt/workbuddy-manager/restore-drills")
KEY = Path("/opt/workbuddy-manager/keys/recovery.agekey")


def _step(name: str, ok, detail: str = "") -> dict:
    return {"step": name, "ok": ok, "detail": detail}


def _write_report(value: dict) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    target = WORK / "drill-report.json"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _safe_sidecar(snapshot: Path, data: bytes) -> None:
    target = snapshot / "recovery-manifest.json"
    if target.is_symlink():
        target.unlink()
    temporary = snapshot / f".recovery-manifest.{os.getpid()}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("sidecar temporary path exists")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _part_bytes(snapshot: Path, meta: dict) -> tuple[bytes, list[dict]]:
    parts = meta.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("cipher parts missing")
    seen = set()
    chunks = []
    evidence = []
    for item in parts:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("invalid cipher part")
        name = item["name"]
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or path.as_posix() != name
                or name in seen or not name.startswith("snapshot.part-") or not name.endswith(".age")):
            raise ValueError("unsafe cipher part")
        seen.add(name)
        target = snapshot / name
        if target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1:
            raise ValueError("cipher part missing or linked")
        raw = target.read_bytes()
        if "bytes" in item and (type(item["bytes"]) is not int or item["bytes"] != len(raw)):
            raise ValueError("cipher part size mismatch")
        if "sha256" in item and item["sha256"] != hashlib.sha256(raw).hexdigest():
            raise ValueError("cipher part digest mismatch")
        chunks.append(raw)
        evidence.append({"name": name, "bytes": len(raw),
                         "sha256": hashlib.sha256(raw).hexdigest()})
    return b"".join(chunks), evidence


def _decrypt(snapshot: Path, key: Path, ciphertext: bytes) -> bytes:
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
        source.write(ciphertext)
        source.seek(0)
        process = subprocess.Popen(["age", "--decrypt", "-i", str(key), "-o", "-"],
                                   stdin=source, stdout=output, stderr=subprocess.PIPE)
        result = process.communicate()
        if process.returncode:
            raise ValueError("age decrypt failed")
        output.seek(0)
        return output.read()


def _archive_sidecar(archive: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.getmember("recovery-manifest.json")
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("invalid recovery sidecar")
        handle = bundle.extractfile(member)
        if handle is None:
            raise ValueError("missing recovery sidecar")
        return handle.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("--restore-sidecar", action="store_true")
    parser.add_argument("--skip-sqlite", action="store_true")
    args = parser.parse_args(argv)
    snapshot = Path(args.snapshot).resolve()
    report = {"schema": 1, "result": "failed", "complete_restore_verified": False,
              "sqlite_skipped": bool(args.skip_sqlite), "read_only_source": True, "steps": []}
    try:
        meta_path = snapshot / "snapshot.json"
        if meta_path.is_symlink() or not meta_path.is_file():
            raise ValueError("snapshot receipt missing")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("snapshot receipt invalid")
        ciphertext, parts = _part_bytes(snapshot, meta)
        report["steps"].append(_step("cipher_sha256", hashlib.sha256(ciphertext).hexdigest() == meta.get("cipher_sha256")))
        if report["steps"][-1]["ok"] is not True:
            raise ValueError("cipher digest mismatch")
        report["steps"][-1]["evidence"] = parts
        archive = _decrypt(snapshot, KEY, ciphertext)
        recorded_archive = meta.get("archive_sha256")
        if not isinstance(recorded_archive, str):
            report["steps"].append(_step("archive_sha256", None, "archive receipt missing"))
            raise ValueError("archive receipt missing")
        archive_ok = hashlib.sha256(archive).hexdigest() == recorded_archive
        report["steps"].append(_step("archive_sha256", archive_ok))
        if not archive_ok:
            raise ValueError("archive digest mismatch")
        if args.restore_sidecar:
            _safe_sidecar(snapshot, _archive_sidecar(archive))
            report["read_only_source"] = False
            report["steps"].append(_step("restore_sidecar", True))
        if args.skip_sqlite:
            report["result"] = "sampled"
            report["complete_restore_verified"] = False
        else:
            # The full SQLite restore requires the operator's isolated DB
            # service and is intentionally never inferred from archive access.
            report["steps"].append(_step("sqlite_restore", None, "operator isolation required"))
            raise ValueError("sqlite restore not verified")
    except Exception as exc:  # noqa: BLE001
        report["error"] = type(exc).__name__
        report["detail"] = str(exc)[:160]
        report["result"] = "failed"
        report["complete_restore_verified"] = False
        _write_report(report)
        return 5
    _write_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
