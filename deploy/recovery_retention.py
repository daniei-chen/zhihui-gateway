#!/usr/bin/env python3
"""Conservative retention and plaintext purge for encrypted recovery snapshots."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
import secrets
import shutil


ROOT = Path("/root/backups/zhihui-v2")
SNAPSHOTS = ROOT / "snapshots"
LATEST = ROOT / "latest.json"
STAMP_FORMAT = "%Y%m%d-%H%M%S"
PLAINTEXT_NAMES = ["runtime.tar", "runtime.tar.gz", "snapshot.tar", "snapshot.tar.gz"]
PROTECTED_NAMES = {"recovery-manifest.json", "metadata.sig"}


def snapshot_dirs() -> list[Path]:
    if not SNAPSHOTS.is_dir() or SNAPSHOTS.is_symlink():
        return []
    return sorted((p for p in SNAPSHOTS.iterdir() if p.is_dir() and not p.is_symlink()),
                  key=lambda p: p.name)


def _parsed(name: str):
    try:
        return datetime.strptime(name, STAMP_FORMAT)
    except (TypeError, ValueError):
        return None


def _latest_info() -> tuple[str | None, bool]:
    if not LATEST.is_file() or LATEST.is_symlink():
        return None, not LATEST.exists()
    try:
        value = json.loads(LATEST.read_text(encoding="utf-8"))
        raw = value.get("snapshot") if isinstance(value, dict) else None
        if isinstance(raw, dict):
            raw = raw.get("directory")
        if not isinstance(raw, str) or not raw:
            return None, False
        path = Path(raw)
        name = path.name
        if path.is_absolute() and path.parent.resolve() != SNAPSHOTS.resolve():
            return None, False
        if name not in {p.name for p in snapshot_dirs()}:
            return None, False
        return name, True
    except (OSError, ValueError, TypeError):
        return None, False


def latest_snapshot_name() -> str | None:
    return _latest_info()[0]


def _fingerprint() -> list[tuple[str, int, int]]:
    result = []
    for path in snapshot_dirs():
        info = path.stat()
        result.append((path.name, info.st_ino, info.st_mtime_ns))
    return result


def build_plan(keep: int = 3, days: int = 7) -> dict:
    if type(keep) is not int or keep < 1 or type(days) is not int or days < 0:
        raise ValueError("invalid retention settings")
    dirs = snapshot_dirs()
    latest, latest_valid = _latest_info()
    now = datetime.now()
    recent = {p.name for p in dirs[-keep:]}
    cutoff = now - timedelta(days=days)
    items = []
    for path in dirs:
        parsed = _parsed(path.name)
        reasons = []
        if path.name in recent:
            reasons.append("最近保留份数")
        if parsed is None:
            reasons.append("无法判断新旧，保守保留")
        elif parsed >= cutoff:
            reasons.append("时间窗口内")
        if path.name == latest:
            reasons.append("latest.json 引用")
        action = "keep" if reasons else "prune"
        items.append({"path": str(path), "action": action,
                      "why": "；".join(reasons) if reasons else "超出保留规则",
                      "name": path.name})
    return {"schema": 1, "confirm_token": secrets.token_urlsafe(18),
            "items": items, "prune_count": sum(i["action"] == "prune" for i in items),
            "latest_snapshot": latest, "latest_valid": latest_valid,
            "fingerprint": _fingerprint(), "latest_bytes": LATEST.read_bytes() if LATEST.is_file() else None}


def _same_plan(plan: dict) -> bool:
    if plan.get("fingerprint") != _fingerprint():
        return False
    latest_bytes = LATEST.read_bytes() if LATEST.is_file() and not LATEST.is_symlink() else None
    return latest_bytes == plan.get("latest_bytes")


def _safe_plan_paths(plan: dict) -> list[Path] | None:
    result = []
    root = SNAPSHOTS.resolve()
    for item in plan.get("items", []):
        if item.get("action") != "prune":
            continue
        path = Path(item.get("path", ""))
        try:
            if path.parent.resolve() != root or path.is_symlink() or not path.is_dir():
                return None
        except OSError:
            return None
        result.append(path)
    return result


def apply_plan(plan: dict, token: str, *, auto: bool = False) -> int:
    if not auto and token != plan.get("confirm_token"):
        return 2
    if not plan.get("prune_count"):
        return 4
    if not _same_plan(plan) or not plan.get("latest_valid", True):
        return 3
    paths = _safe_plan_paths(plan)
    if paths is None:
        return 3
    for path in paths:
        shutil.rmtree(path)
    return 0


def snapshot_meta(snapshot: Path) -> dict | None:
    path = snapshot / "snapshot.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def cipher_matches(snapshot: Path, meta: dict | None, **_kwargs) -> tuple[bool, str]:
    if not isinstance(meta, dict) or not isinstance(meta.get("cipher_sha256"), str):
        return False, "无法校验密文：缺少摘要"
    parts = meta.get("parts")
    if not isinstance(parts, list) or not parts:
        return False, "无法校验密文：缺少分片"
    seen = set()
    chunks = []
    for item in parts:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return False, "分片记录无效"
        name = item["name"]
        pure = PurePosixPath(name)
        if (name in seen or pure.is_absolute() or ".." in pure.parts
                or pure.as_posix() != name or not name.startswith("snapshot.part-")
                or not name.endswith(".age")):
            return False, "分片路径不安全"
        seen.add(name)
        path = snapshot / name
        try:
            info = path.lstat()
            if path.is_symlink() or not path.is_file() or info.st_nlink != 1:
                return False, "分片缺失或被链接"
            raw = path.read_bytes()
        except OSError:
            return False, "缺少分片"
        if type(item.get("bytes", len(raw))) is not int or item.get("bytes", len(raw)) != len(raw):
            return False, "分片大小不符"
        if item.get("sha256", hashlib.sha256(raw).hexdigest()) != hashlib.sha256(raw).hexdigest():
            return False, "分片摘要不符"
        chunks.append(raw)
    digest = hashlib.sha256(b"".join(chunks)).hexdigest()
    if digest != meta["cipher_sha256"]:
        return False, "密文摘要不符"
    return True, "cipher verified"


def build_plaintext_plan() -> dict:
    items = []
    for snapshot in snapshot_dirs():
        meta = snapshot_meta(snapshot)
        ok, detail = cipher_matches(snapshot, meta)
        for name in PLAINTEXT_NAMES:
            path = snapshot / name
            if not path.exists() or path.is_symlink() or not path.is_file():
                continue
            action = "prune" if ok else "keep"
            items.append({"path": str(path), "files": [str(path)], "action": action,
                          "why": "密文已复算通过" if ok else "无法校验密文：" + detail,
                          "snapshot": str(snapshot), "cipher_fingerprint": _fingerprint_for(snapshot)})
    return {"schema": 1, "confirm_token": secrets.token_urlsafe(18), "items": items,
            "prune_count": sum(i["action"] == "prune" for i in items)}


def _fingerprint_for(path: Path):
    try:
        info = path.lstat()
        return (str(path), info.st_dev, info.st_ino, info.st_mtime_ns)
    except OSError:
        return None


def apply_plaintext_plan(plan: dict, token: str, *, auto: bool = False) -> int:
    if not auto and token != plan.get("confirm_token"):
        return 2
    if not plan.get("prune_count"):
        return 4
    for item in plan.get("items", []):
        if item.get("action") != "prune":
            continue
        snapshot = Path(item.get("snapshot", ""))
        expected = tuple(item.get("cipher_fingerprint") or ())
        if snapshot.is_symlink() or not snapshot.is_dir() or _fingerprint_for(snapshot) != expected:
            return 4
        meta = snapshot_meta(snapshot)
        for raw in item.get("files", []):
            path = Path(raw)
            if path.parent != snapshot or path.name not in PLAINTEXT_NAMES or path.is_symlink():
                return 4
            if not path.is_file() or path.is_symlink():
                return 4
            # Move the known file to a private staging name before rechecking
            # the ciphertext.  If the directory is replaced during that
            # check, deleting this handle still deletes only the original
            # plaintext file; it can never follow the replacement symlink.
            staged = ROOT / f".plaintext-purge-{os.getpid()}-{secrets.token_hex(8)}"
            os.replace(path, staged)
            ok = False
            try:
                ok = cipher_matches(snapshot, meta)[0]
                if ok:
                    staged.unlink(missing_ok=True)
                else:
                    # The directory may have been replaced after the initial
                    # verification.  The staged inode is already detached
                    # from that path, so removing it is safe and must not
                    # follow a replacement symlink.  A content change in the
                    # original directory remains a hard failure below.
                    if snapshot.is_symlink() or not snapshot.is_dir():
                        staged.unlink(missing_ok=True)
                        continue
                    if snapshot.is_dir() and not snapshot.is_symlink() and not path.exists():
                        os.replace(staged, path)
                    return 4
            except Exception:
                if staged.exists() and snapshot.is_dir() and not snapshot.is_symlink() and not path.exists():
                    os.replace(staged, path)
                return 4
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--token")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--purge-plaintext", action="store_true")
    args = parser.parse_args(argv)
    if args.keep < 1 or args.days < 0:
        return 2
    if args.auto and not args.apply and not args.purge_plaintext:
        return 2
    if args.purge_plaintext:
        plan = build_plaintext_plan()
        if not args.apply and not args.auto:
            print(json.dumps(plan, ensure_ascii=False))
            return 0
        if not args.auto and not args.token:
            return 2
        return apply_plaintext_plan(plan, args.token or "", auto=args.auto)
    plan = build_plan(args.keep, args.days)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if not args.auto and not args.token:
        return 2
    return apply_plan(plan, args.token or "", auto=args.auto)


if __name__ == "__main__":
    raise SystemExit(main())
