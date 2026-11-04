import argparse
import json
from pathlib import Path
import re


def heads(value):
    result = {}
    for project in value.get('projects', []):
        name = project.get('repository')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name):
            raise ValueError('invalid repository identity')
        commits = project.get('commits') or []
        head = project.get('observed_head') or (commits[0].get('sha') if commits else None)
        if head is not None and (not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head)):
            raise ValueError('invalid commit identity')
        if name in result:
            raise ValueError('duplicate repository identity')
        result[name] = head
    return result


def compare(before, after):
    old, new = heads(before), heads(after)
    changes = []
    for name in sorted(old.keys() | new.keys()):
        previous, current = old.get(name), new.get(name)
        state = 'not_observed' if not current else 'new' if name not in old else 'changed' if previous != current else 'unchanged'
        changes.append({'repository': name, 'previous': previous, 'current': current, 'state': state})
    return {'schema': 1, 'network_accessed': False, 'upstream_code_executed': False,
            'automatic_installation': False, 'changes': changes,
            'unknown_count': sum(row['state'] == 'not_observed' for row in changes)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline public-source delta review; never downloads or installs code')
    parser.add_argument('--baseline', type=Path, default=Path(__file__).with_name('upstream-registry.json'))
    parser.add_argument('--observed', required=True, type=Path)
    options = parser.parse_args(argv)
    try:
        result = compare(json.loads(options.baseline.read_text(encoding='utf-8')),
                         json.loads(options.observed.read_text(encoding='utf-8')))
    except (ValueError, OSError, TypeError, KeyError):
        print(json.dumps({'ok': False, 'error': 'invalid_offline_source_evidence', 'network_accessed': False}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result['unknown_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
