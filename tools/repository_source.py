"""Acquire an immutable source commit from the sole product origin; never install it.

Uses Git's object verification, a detached exact-commit checkout and a fresh
destination. A source checkout is not a signed production release. Production
still requires build, independent tests, Ed25519 sealing and the trusted publisher.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORY = 'daniei-chen/zhihui-gateway'
ORIGIN = 'https://github.com/' + REPOSITORY + '.git'


def git(directory: Path, *arguments: str) -> str:
    # Ignore ambient URL rewrites, credential helpers, templates and Git overrides.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1')
    result = subprocess.run(['git', '-c', 'core.hooksPath=', '-c', 'credential.helper=',
                             '-c', 'http.followRedirects=false', '-c', 'protocol.allow=never',
                             '-c', 'protocol.https.allow=always', '-c', 'core.autocrlf=false',
                             '-C', str(directory), *arguments],
                            capture_output=True, text=True, timeout=180, check=True, env=env)
    return result.stdout.strip()


def acquire(destination: Path, commit: str, tag: str | None = None) -> dict:
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('a full immutable commit SHA is required')
    if tag is not None and not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('invalid product release tag')
    if destination.exists() or destination.is_symlink():
        raise ValueError('a new destination is required; existing source is never overwritten')
    destination.mkdir(parents=False)
    git(destination, 'init', '--quiet', '--template=')
    git(destination, 'remote', 'add', 'origin', ORIGIN)
    git(destination, '-c', 'credential.helper=', '-c', 'core.hooksPath=',
        '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true',
        'fetch', '--no-tags', '--depth=1', 'origin', tag or commit)
    actual = git(destination, 'rev-parse', 'FETCH_HEAD^{commit}')
    if actual != commit:
        raise ValueError('tag/remote commit differs from the approved commit')
    git(destination, '-c', 'core.hooksPath=', 'checkout', '--detach', '--quiet', commit)
    if git(destination, 'remote', 'get-url', 'origin') != ORIGIN:
        raise ValueError('product origin changed')
    if git(destination, 'status', '--porcelain'):
        raise ValueError('acquired checkout is not clean')
    version_path = destination / 'VERSION'
    if version_path.is_symlink() or not version_path.is_file():
        raise ValueError('source VERSION must be a regular file')
    version = version_path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('invalid product version')
    if tag and tag != 'v' + version:
        raise ValueError('tag and source product version differ')
    return {'repository': REPOSITORY, 'origin': ORIGIN, 'source_commit': actual,
            'version': version, 'tag': tag, 'production_modified': False,
            'source_only_not_a_trusted_installation': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tag')
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(acquire(args.destination.absolute(), args.commit, args.tag), indent=2))


if __name__ == '__main__':
    main()
