import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(r'D:\AI\kimi\2\zhihui-gateway')


def h(rel):
    raw = subprocess.check_output(['git', '-C', str(ROOT), 'cat-file', 'blob', f':{rel}'])
    return hashlib.sha256(raw).hexdigest()


stage = __import__('sys').argv[1] if len(__import__('sys').argv) > 1 else 'protected'

if stage == 'protected':
    path = ROOT / 'docs/protected-customizations.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    for rel in list(data):
        value = h(rel)
        if value != data[rel]:
            print('protected', rel, '->', value[:12])
            data[rel] = value
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
                    encoding='utf-8')
    print('stage 1 done')
else:
    path = ROOT / 'server/release-provenance.json'
    prov = json.loads(path.read_text(encoding='utf-8'))
    changed = []
    for rel in list(prov['reviewed_patch_files']):
        try:
            value = h(rel)
        except subprocess.CalledProcessError:
            continue
        if value != prov['reviewed_patch_files'][rel]:
            prov['reviewed_patch_files'][rel] = value
            changed.append(rel)
    path.write_text(json.dumps(prov, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
                    encoding='utf-8')
    print('stage 2 synced', len(changed), changed)
