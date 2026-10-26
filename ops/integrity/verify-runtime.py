#!/usr/bin/env python3
"""K15 完整性清单工具（候选，未部署）。

目的：证明"线上运行的字节 = 签名清单里的字节"，并把**运行覆盖层**
（local-overrides、部署后追加的补丁）与依赖、配置纳入同一份清单。

用法：
  python verify-runtime.py --release /opt/workbuddy-manager/releases/current \
      --out /tmp/runtime-manifest.json [--baseline /opt/workbuddy-manager/releases/current/deploy/managed-release.json]

输出：逐文件 sha256 + 大小 + 相对路径；对基线清单做差集（新增/缺失/变更）。
只读，不修改任何文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", "data", "node_modules", ".venv", "venv"}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root: Path, *, include_overrides: bool = True):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        if not include_overrides and "local-overrides" in rel_dir.parts:
            continue
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            yield p, p.relative_to(root).as_posix(), st


def build_manifest(root: Path) -> dict:
    files = {}
    overrides = []
    for p, rel, st in walk(root):
        try:
            digest = sha256_of(p)
        except OSError as exc:  # 权限/损坏：记为不可读而不是静默跳过
            files[rel] = {"error": type(exc).__name__, "size": st.st_size}
            continue
        files[rel] = {"sha256": digest, "size": st.st_size}
        if "local-overrides" in Path(rel).parts:
            overrides.append(rel)
    return {
        "root": str(root),
        "files": files,
        "count": len(files),
        "untrusted_overlay": overrides,
        "overlay_count": len(overrides),
    }


def load_baseline(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    # managed-release.json 的格式：{ "files": {"rel": {"sha256": ...}} } 或 {"rel": "hash"}
    files = data.get("files") if isinstance(data, dict) else None
    if isinstance(files, dict):
        out = {}
        for k, v in files.items():
            out[k.replace("\\", "/")] = v.get("sha256") if isinstance(v, dict) else str(v)
        return out
    if isinstance(files, list):
        out = {}
        for item in files:
            if isinstance(item, dict) and "path" in item:
                out[str(item["path"]).replace("\\", "/")] = item.get("sha256")
        return out
    raise SystemExit("基线清单格式无法识别")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline")
    ap.add_argument("--fail-on-overlay", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.release)
    if not root.is_dir():
        print(f"release 目录不存在: {root}", file=sys.stderr)
        return 2

    manifest = build_manifest(root)
    if args.baseline:
        base_path = Path(args.baseline)
        if base_path.is_file():
            base = load_baseline(base_path)
            current = {k: v.get("sha256") for k, v in manifest["files"].items() if "sha256" in v}
            missing = sorted(set(base) - set(current))
            extra = sorted(set(current) - set(base))
            changed = sorted(
                k for k in (set(base) & set(current))
                if base[k] and current[k] and base[k] != current[k]
            )
            manifest["baseline"] = {
                "path": str(base_path),
                "missing": missing[:200],
                "extra": extra[:200],
                "changed": changed[:200],
                "counts": {"missing": len(missing), "extra": len(extra), "changed": len(changed)},
            }
        else:
            manifest["baseline"] = {"path": str(base_path), "error": "not found"}

    out = Path(args.out)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"files={manifest['count']} overlay={manifest['overlay_count']} -> {out}")
    if manifest["overlay_count"]:
        print("警告：运行覆盖层未在签名清单内（local-overrides 可执行代码）", file=sys.stderr)
        if args.fail_on_overlay:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
