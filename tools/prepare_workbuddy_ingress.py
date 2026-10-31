"""Prepare an exact, credential-free WorkBuddy source candidate; never install it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


BUNDLE = Path(__file__).resolve().parents[1] / 'ops/workbuddy-ingress'


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def relative_path(name: str) -> Path:
    parts = PurePosixPath(name)
    if (not name or parts.is_absolute() or '\\' in name or ':' in name
            or str(parts) != name or any(part in ('.', '..') for part in parts.parts)):
        raise ValueError('unsafe manifest path')
    return Path(*parts.parts)


def regular(path: Path) -> bytes:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('linked or non-regular input')
    return path.read_bytes()


def safe_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('regular directory required')


def inventory(root: Path) -> dict[str, str]:
    for path in (root, *root.parents):
        safe_directory(path)
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path = Path(directory) / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('linked input directory')
        for name in files:
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            relative_path(rel)
            # The original export's receipt is provenance, not a compiler input.
            raw = regular(path)
            if rel != 'source-receipt.json':
                result[rel] = digest(raw)
    return result


def apply_patch(destination: Path, raw: bytes, git: str) -> None:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0')
    base = [git, '-c', 'core.hooksPath=', '-c', 'core.autocrlf=false',
            '-c', 'core.safecrlf=false', '-c', 'apply.whitespace=nowarn',
            '-C', str(destination), 'apply']
    # No rejects, fuzzy application, hooks, or shell interpolation.
    for args in (['--check', '-'], ['-']):
        subprocess.run(base + args, input=raw, capture_output=True,
                       timeout=30, check=True, env=env)


def prepare(source: Path, destination: Path, git: str = 'git') -> dict:
    manifest = json.loads(regular(BUNDLE / 'manifest.json'))
    raw_patch = regular(BUNDLE / 'ingress.patch')
    if manifest.get('schema') != 1 or digest(raw_patch) != manifest['patch_sha256']:
        raise ValueError('patch identity mismatch')
    for mapping in ('source_files', 'candidate_files'):
        values = manifest[mapping]
        if len({name.casefold() for name in values}) != len(values):
            raise ValueError('case-colliding input names')
        for name, sha in values.items():
            relative_path(name)
            if not re.fullmatch('[a-f0-9]{64}', sha):
                raise ValueError('invalid manifest digest')
    source = source.absolute()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('new destination required; no existing tree is overwritten')
    if source.resolve() in destination.resolve().parents:
        raise ValueError('candidate must be outside its source')
    for path in (destination.parent, *destination.parent.parents):
        safe_directory(path)
    before = inventory(source)
    if before != manifest['source_files']:
        raise ValueError('source closure differs; do not use the mutable live checkout')
    destination.mkdir(mode=0o700)
    for name, expected in sorted(before.items()):
        raw = regular(source / relative_path(name))
        if digest(raw) != expected:
            raise ValueError('source changed during preparation')
        target = destination / relative_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(raw)
    apply_patch(destination, raw_patch, git)
    after = inventory(destination)
    if after != manifest['candidate_files'] or inventory(source) != before:
        raise ValueError('candidate or source closure changed')
    return {'schema': 1, 'component': manifest['component'],
            'patch_sha256': manifest['patch_sha256'], 'files_verified': len(after),
            'tree_sha256': digest(json.dumps(after, sort_keys=True, separators=(',', ':')).encode()),
            'source_unchanged': True, 'production_modified': False,
            'build_tests_signature_and_guarded_publication_still_required': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--git', default='git')
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.destination, args.git), indent=2))


if __name__ == '__main__':
    main()
