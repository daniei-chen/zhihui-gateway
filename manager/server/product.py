"""Product identity is release-owned; provider URLs are not product upstreams."""
from __future__ import annotations

import json
from pathlib import Path
import re


REPOSITORY = 'daniei-chen/zhihui-gateway'
REPOSITORY_URL = 'https://github.com/' + REPOSITORY
IDENTITY_FILE = Path(__file__).with_name('product-release.json')


def identity() -> dict:
    value = json.loads(IDENTITY_FILE.read_text(encoding='utf-8'))
    if (value.get('schema') != 1 or value.get('repository') != REPOSITORY
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', str(value.get('version', '')))):
        raise ValueError('invalid product identity')
    return value


def metadata(root: Path, *, managed: bool) -> dict:
    value = identity()
    commit = None
    provenance = root / 'server/release-provenance.json'
    if provenance.is_file():
        try:
            record = json.loads(provenance.read_text(encoding='utf-8'))
            if not isinstance(record, dict):
                return _metadata(value, None, managed)
            candidate = record.get('source_commit', '')
            if (record.get('repository') == REPOSITORY
                    and record.get('version') == value['version']
                    and re.fullmatch(r'[0-9a-f]{40}', candidate)):
                commit = candidate
        except (OSError, ValueError, TypeError):
            pass
    return _metadata(value, commit, managed)


def _metadata(value: dict, commit: str | None, managed: bool) -> dict:
    return {
        'name': value['name'], 'version': value['version'], 'repository': REPOSITORY,
        'repository_url': REPOSITORY_URL, 'branch': value['branch'],
        'releases_url': REPOSITORY_URL + '/releases', 'source_commit': commit,
        'manager_baseline': value['manager_baseline'],
        'release_channel': value['release_channel'], 'managed': managed,
        'web_update_enabled': not managed,
        'update_policy': 'trusted-publisher-required' if managed else 'legacy-updater',
        'provider_endpoints_changed': False,
    }
