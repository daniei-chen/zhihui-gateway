#!/usr/bin/env python3
"""Validate immutable release identity and the evidence for stable promotion."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
SCHEMA = 'zhihui.release-images.v1'


def version_tuple(tag: str) -> tuple[int, int, int]:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError('release tag must have the form vMAJOR.MINOR.PATCH')
    return tuple(int(part) for part in match.groups())


def require_sha(value: str) -> str:
    if not SHA.fullmatch(value):
        raise ValueError('release source must be a full lowercase commit SHA')
    return value


def resolve(tag: str, root: Path) -> dict[str, str]:
    version_tuple(tag)
    sha = subprocess.run(['git', 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}'],
                         cwd=root, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    require_sha(sha)
    version = subprocess.run(['git', 'show', f'{sha}:VERSION'], cwd=root, check=True,
                             capture_output=True, text=True, timeout=30).stdout.strip()
    if version != tag[1:]:
        raise ValueError('tag and source VERSION do not match')
    return {'tag': tag, 'version': version, 'source_sha': sha}


def validate_manifest(value: dict, repository: str, tag: str, source: str) -> dict:
    version_tuple(tag)
    require_sha(source)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('invalid repository identity')
    if (value.get('schema') != SCHEMA or value.get('repository') != repository
            or value.get('tag') != tag or value.get('source_commit') != source):
        raise ValueError('image evidence does not match repository, tag and source commit')
    require_sha(str(value.get('workflow_source_sha', '')))
    for name in ('run_id', 'run_attempt'):
        if type(value.get(name)) is not int or value[name] < 1:
            raise ValueError(f'invalid release workflow {name}')
    images = value.get('images')
    if not isinstance(images, dict) or set(images) != {'manager', 'upstream'}:
        raise ValueError('both manager and upstream image evidence are required')
    owner = repository.split('/')[0].lower()
    for kind, package in (('manager', 'workbuddy-manager'), ('upstream', 'workbuddy2api')):
        image = images[kind]
        if (not isinstance(image, dict) or image.get('name') != f'ghcr.io/{owner}/{package}'
                or not DIGEST.fullmatch(str(image.get('digest', '')))):
            raise ValueError(f'invalid {kind} image identity or digest')
    return value


def validate_promotion(*, repository: str, tag: str, source: str, manifest: dict,
                       release: dict, latest: dict, releases: list[dict], run: dict,
                       jobs: list[dict]) -> dict:
    """Fail closed before any registry mutation; callers supply read-only API facts."""
    validate_manifest(manifest, repository, tag, source)
    if (release.get('tag_name') != tag or release.get('draft') is not False
            or release.get('prerelease') is not False or not release.get('published_at')):
        raise ValueError('stable promotion requires a published non-prerelease Release')
    if latest.get('tag_name') != tag or latest.get('id') != release.get('id'):
        raise ValueError('only the current latest published Release may advance stable images')
    target = version_tuple(tag)
    for other in releases:
        other_tag = str(other.get('tag_name', ''))
        if other.get('draft') is False and other.get('prerelease') is False and TAG.fullmatch(other_tag):
            if version_tuple(other_tag) > target:
                raise ValueError('a newer stable Release exists; refusing channel downgrade')
    if (run.get('id') != manifest['run_id'] or run.get('run_attempt') != manifest['run_attempt']
            or run.get('path') != '.github/workflows/release.yml'
            or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_sha') != manifest['workflow_source_sha']
            or run.get('repository', {}).get('full_name') != repository):
        raise ValueError('the recorded release workflow did not complete successfully for this source')
    for required in ('frontend', 'manager', 'gateway'):
        matches = [job for job in jobs if job.get('name') == f'quality / {required}']
        if len(matches) != 1 or matches[0].get('conclusion') != 'success' or matches[0].get('status') != 'completed':
            raise ValueError(f'missing successful same-run quality job: {required}')
    return {'tag': tag, 'source_commit': source, 'images': manifest['images'],
            'release_id': release['id'], 'run_id': manifest['run_id']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    resolve_parser = sub.add_parser('resolve')
    resolve_parser.add_argument('--tag', required=True)
    resolve_parser.add_argument('--root', type=Path, default=Path('.'))
    manifest_parser = sub.add_parser('manifest')
    manifest_parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'resolve':
            value = resolve(args.tag, args.root)
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
                    handle.writelines(f'{key}={item}\n' for key, item in value.items())
        else:
            value = {
                'schema': SCHEMA, 'repository': os.environ['GITHUB_REPOSITORY'],
                'tag': os.environ['RELEASE_TAG'], 'source_commit': os.environ['SOURCE_COMMIT'],
                'workflow_source_sha': os.environ['GITHUB_SHA'],
                'run_id': int(os.environ['GITHUB_RUN_ID']), 'run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
                'images': {
                    'manager': {'name': os.environ['MANAGER_IMAGE'], 'digest': os.environ['MANAGER_DIGEST']},
                    'upstream': {'name': os.environ['UPSTREAM_IMAGE'], 'digest': os.environ['UPSTREAM_DIGEST']},
                },
            }
            validate_manifest(value, value['repository'], value['tag'], value['source_commit'])
            args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(value, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'{exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
