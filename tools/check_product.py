"""Read-only coherence checks for product version, origin, icons and public docs."""
from pathlib import Path
import json
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def version_contract(version, marker, notes):
    numeric = r'0\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
    assert re.fullmatch(numeric, version), 'invalid product version'
    # Deployment metadata is identity, not a second product version. Do not
    # accept a different version, prerelease, empty or arbitrary suffix.
    assert re.fullmatch('v' + re.escape(version) + r'(?:\+[a-z0-9][a-z0-9.-]{0,95})?', marker), 'product marker differs'
    history = re.findall(r'^## \[([^\]]+)\]', notes, re.M)
    assert history and history[0] == version, 'current version must be first'
    assert len(history) == len(set(history)), 'duplicate product history'
    assert all(re.fullmatch(numeric, item) for item in history), 'non-product history'
    tuples = [tuple(map(int, item.split('.'))) for item in history]
    assert tuples == sorted(tuples, reverse=True), 'product history must descend'
    assert all(item >= (0, 0, 2) for item in tuples), 'pre-product history must stay separate'


def main():
    version = (ROOT / 'VERSION').read_text(encoding='ascii').strip()
    identity = json.loads((ROOT / 'manager/server/product-release.json').read_bytes())
    web = ROOT / 'src/three-platform/web'
    package = json.loads((web / 'package.json').read_bytes())
    lock = json.loads((web / 'package-lock.json').read_bytes())
    assert version == identity['version'] == package['version'] == lock['version'] == lock['packages']['']['version']
    changelogs = [(ROOT / name).read_bytes() for name in
                  ['CHANGELOG.md', 'manager/CHANGELOG.md', 'manager/server/CHANGELOG.md']]
    assert changelogs[0] == changelogs[1] == changelogs[2], 'product changelog copies differ'
    notes = changelogs[0].decode('utf-8')
    version_contract(version, (ROOT / 'manager/.version').read_text().strip(), notes)
    assert len(re.findall(r'^- .+', notes, re.M)) >= 5, 'product release has no useful notes'
    assert identity['repository'] == 'daniei-chen/zhihui-gateway'
    assert "PRODUCT_VERSION = '" + version + "'" in (web / 'lib/product.ts').read_text()
    for locale in ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']:
        copy = json.loads((web / 'lib/i18n/locales' / (locale + '.json')).read_bytes())
        assert set(copy['product']) == {
            'name', 'repository', 'version', 'origin', 'sourceCommit', 'unrecorded',
            'baseline', 'releases', 'providerBoundary', 'controlledTitle', 'controlledDetail',
        }, locale
    home_import = json.loads((ROOT / 'site/home-import.json').read_bytes())
    prefix = home_import['asset_prefix']
    assert re.fullmatch(r'/home-assets/r-[a-f0-9]{20}/', prefix)
    files = {row['path']: row for row in home_import['files']}
    icon = 'site/home/' + prefix.removeprefix('/home-assets/') + 'favicon.svg'
    assert icon.removeprefix('site/') in files
    import hashlib
    assert hashlib.sha256((ROOT / icon).read_bytes()).hexdigest() == files[icon.removeprefix('site/')]['sha256']
    assert prefix + 'favicon.svg' in (ROOT / 'site/home/index.html').read_text(encoding='utf-8')
    login = (ROOT / 'site/login/index.html').read_bytes()
    preserved = json.loads((ROOT / 'site/login/preservation.json').read_bytes())
    assert hashlib.sha256(login).hexdigest() == preserved['candidate_sha256']
    assert b'/favicon/zhihui-z-20261029/favicon.svg' in login
    assert '三平台' in login.decode('utf-8')
    assert preserved['candidate_sha256'] == preserved['source_sha256']
    assert preserved['permitted_text_changes'] == []
    home = (ROOT / 'site/home/index.html').read_text(encoding='utf-8')
    assert 'data-platform="codex"' not in home and '四种入口' not in home
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    targets = re.findall(r'(?:src|href)="([^"#]+)"', readme)
    targets += re.findall(r'\]\(([^)#]+)\)', readme)
    for target in targets:
        if not target.startswith(('https://', 'http://', '#')):
            resolved = (ROOT / target.split('#')[0]).resolve()
            assert resolved.is_relative_to(ROOT) and resolved.exists(), target
    assert not list((ROOT / 'manager/server').rglob('*.db'))
    print(json.dumps({'version': version, 'repository': identity['repository'], 'coherent': True}))


if __name__ == '__main__':
    main()
