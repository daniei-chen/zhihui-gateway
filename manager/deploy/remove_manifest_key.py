#!/opt/workbuddy-manager/venv/bin/python
"""从 managed-release.json 移除一个非发布产物条目（U11 清单卫生）。

与 deploy/sign-release-ed25519.py 的写回格式保持一致（indent=2, sort_keys=True），
避免下一次 --rebuild 产生无意义的格式 diff。

用法：remove_manifest_key.py <manifest.json> <relative-key>
"""
import json
import pathlib
import sys


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    path = pathlib.Path(argv[0])
    key = argv[1]
    data = json.loads(path.read_text(encoding='utf-8'))
    files = data.get('files') or {}
    if key not in files:
        print('not present: %s (files=%d)' % (key, len(files)))
        return 0
    del files[key]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')
    print('removed: %s (files=%d)' % (key, len(files)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
