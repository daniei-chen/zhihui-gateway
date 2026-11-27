#!/usr/bin/env python3
"""Bind a static export to the exact source commit used by quality gates.

The manifest travels inside the immutable Actions artifact. Consumers verify
both its source identity and every file before running HTTP tests or packaging.
This is build provenance, not a production deployment attestation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat

MANIFEST = '.quality-source.json'
SCHEMA = 'zhihui.quality-frontend.v1'
MAX_FILES = 20_000
MAX_BYTES = 512 * 1024 * 1024


def source_sha(value: str) -> str:
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('source SHA must be a full lowercase commit ID')
    return value


def snapshot(root: Path) -> dict[str, dict]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError('static export root is missing or is a symlink')
    files: dict[str, dict] = {}
    total = 0
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f'non-regular static artifact: {relative}')
        size = path.stat().st_size
        total += size
        if len(files) >= MAX_FILES or total > MAX_BYTES:
            raise ValueError('static export exceeds artifact resource limits')
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b''):
                digest.update(chunk)
        files[relative] = {'sha256': digest.hexdigest(), 'bytes': size}
    for required in ('index.html', 'login/index.html'):
        if not files.get(required, {}).get('bytes'):
            raise ValueError(f'required static page missing or empty: {required}')
    if not any(name.startswith('_next/static/') for name in files):
        raise ValueError('static JavaScript assets are missing')
    return files


def create(root: Path, sha: str) -> dict:
    value = {'schema': SCHEMA, 'source_commit': source_sha(sha), 'files': snapshot(root)}
    (root / MANIFEST).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return value


def verify(root: Path, sha: str) -> dict:
    path = root / MANIFEST
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('missing or invalid static artifact manifest')
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema') != SCHEMA or value.get('source_commit') != source_sha(sha):
        raise ValueError('static artifact does not belong to the checked-out source commit')
    if value.get('files') != snapshot(root):
        raise ValueError('static artifact bytes differ from the quality-checked export')
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('create', 'verify'))
    parser.add_argument('--root', type=Path, default=Path('web/out'))
    parser.add_argument('--source-sha', required=True)
    args = parser.parse_args()
    try:
        value = (create if args.operation == 'create' else verify)(args.root, args.source_sha)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f'{exc}\n')
    print(f"verified static export: {value['source_commit']} ({len(value['files'])} files)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
