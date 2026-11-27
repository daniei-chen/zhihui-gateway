#!/usr/bin/env python3
"""Promote recorded image digests only after an explicit stable Release publication.

Default mode is a read-only plan. --apply moves registry aliases, never deploys
containers. Both current aliases must exist so a caught failure can restore them.
Cross-repository tags cannot be atomic; the durable report records rollback or
repair requirements, and a rerun reconciles both aliases to the same release.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import release_policy as policy  # noqa: E402


def command(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=180).stdout


def api(repository: str, resource: str, *, pages: bool = False):
    args = ['gh', 'api', f'repos/{repository}/{resource}']
    if pages:
        args.extend(['--paginate', '--slurp'])
    return json.loads(command(args))


def image_digest(reference: str, run: Callable = command) -> str:
    value = json.loads(run(['docker', 'buildx', 'imagetools', 'inspect', reference,
                           '--format', '{{json .Manifest}}']))
    digest = str(value.get('digest', ''))
    if not policy.DIGEST.fullmatch(digest):
        raise ValueError(f'registry did not return a valid digest for {reference}')
    return digest


def write_report(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def apply_plan(plan: dict, *, apply: bool, report: Path, run: Callable = command,
               recheck: Callable = lambda: None) -> dict:
    """Validate all inputs/targets before writing; restore even ambiguous writes."""
    rows = []
    for kind in ('manager', 'upstream'):
        image = plan['images'][kind]
        name, target = image['name'], image['digest']
        if image_digest(f'{name}@{target}', run) != target:
            raise ValueError(f'{kind} immutable image digest mismatch')
        # Fail closed on missing aliases: inventing an old target makes rollback
        # impossible. A new registry must seed both aliases deliberately first.
        previous = image_digest(f'{name}:latest', run)
        rows.append({'name': name, 'target': target, 'previous': previous})
    value = {**plan, 'aliases': rows, 'state': 'planned', 'apply': apply}
    write_report(report, value)
    if not apply:
        return value
    touched = []
    try:
        for row in rows:
            recheck()
            if image_digest(f"{row['name']}:latest", run) != row['previous']:
                raise ValueError('stable alias changed after preflight; refusing to overwrite it')
            if row['target'] == row['previous']:
                continue
            # Record ownership before the registry request: a nonzero response
            # can arrive after the server has already accepted the new tag.
            touched.append(row)
            value['state'] = 'updating'
            value['attempted'] = [item['name'] for item in touched]
            write_report(report, value)
            run(['docker', 'buildx', 'imagetools', 'create', '--prefer-index=false',
                 '--tag', f"{row['name']}:latest", f"{row['name']}@{row['target']}"])
            if image_digest(f"{row['name']}:latest", run) != row['target']:
                raise ValueError('stable alias verification failed')
        recheck()
        value['state'] = 'promoted'
        write_report(report, value)
        return value
    except BaseException as exc:
        failures = []
        for row in reversed(touched):
            try:
                observed = image_digest(f"{row['name']}:latest", run)
                if observed == row['previous']:
                    continue
                if observed != row['target']:
                    raise ValueError('alias changed outside this promotion; manual repair required')
                run(['docker', 'buildx', 'imagetools', 'create', '--prefer-index=false',
                     '--tag', f"{row['name']}:latest", f"{row['name']}@{row['previous']}"])
                if image_digest(f"{row['name']}:latest", run) != row['previous']:
                    raise ValueError('rollback verification failed')
            except Exception as rollback_error:
                failures.append({'image': row['name'], 'error': str(rollback_error)})
        value.update(state='repair_required' if failures else 'rolled_back',
                     error=str(exc), rollback_errors=failures)
        write_report(report, value)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, default=Path('promotion-report.json'))
    args = parser.parse_args()

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('promotion interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        source = policy.resolve(args.tag, Path('.'))['source_sha']
        with tempfile.TemporaryDirectory(prefix='zhihui-promotion-') as temporary:
            command(['gh', 'release', 'download', args.tag, '--repo', args.repository,
                     '--pattern', 'release-images.json', '--dir', temporary])
            path = Path(temporary) / 'release-images.json'
            if path.stat().st_size > 64 * 1024:
                raise ValueError('release image evidence exceeds size limit')
            manifest = json.loads(path.read_text(encoding='utf-8'))
        policy.validate_manifest(manifest, args.repository, args.tag, source)
        release = api(args.repository, f'releases/tags/{args.tag}')
        latest = api(args.repository, 'releases/latest')
        releases = [item for page in api(args.repository, 'releases?per_page=100', pages=True) for item in page]
        run = api(args.repository, f"actions/runs/{manifest['run_id']}")
        job_pages = api(args.repository, f"actions/runs/{manifest['run_id']}/attempts/{manifest['run_attempt']}/jobs?per_page=100", pages=True)
        jobs = [job for page in job_pages for job in page['jobs']]
        plan = policy.validate_promotion(repository=args.repository, tag=args.tag, source=source,
                                         manifest=manifest, release=release, latest=latest,
                                         releases=releases, run=run, jobs=jobs)

        def recheck():
            current = api(args.repository, 'releases/latest')
            if current.get('id') != release['id'] or current.get('tag_name') != args.tag:
                raise ValueError('latest Release changed during promotion')

        result = apply_plan(plan, apply=args.apply, report=args.report, recheck=recheck)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        parser.exit(1, f'stable promotion stopped: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
