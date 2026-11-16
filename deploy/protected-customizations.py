#!/opt/workbuddy-manager/venv/bin/python
"""校验 `docs/protected-customizations.json` 与当前源码一致，并可自动修正。

## 为什么需要这个工具

CI 里那条 `Preserve customized algorithm contract` 要求若干「定制算法」文件的
哈希与登记值一致，改了就必须同步登记。但**手工同步已经漏过两次**
（2026-11-16 同一天内：先漏 `deploy/sign-release-ed25519.py`，再漏
`server/routers/gateway.py` 与 `server/budget.py`），每次都要等 CI 红了才发现。

这个脚本把「算哈希」和「写登记」合成一步，本地跑一次就能对齐。

## 用法

    # 只检查（CI 用这个语义，退出码非 0 表示不一致）
    deploy/protected-customizations.py --check

    # 就地更新登记表（改了受保护文件后跑这个）
    deploy/protected-customizations.py --update

哈希按 **LF 规范化后**的字节计算，因此与检出时的行尾无关 —— 这是 2026-11-16
那次「同一份内容在 Windows 与 Linux 上哈希不同」的教训。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'docs' / 'protected-customizations.json'


def digest(path: Path) -> str:
    """LF 规范化后的 sha256：行尾不影响结论。"""
    raw = path.read_bytes()
    return hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()


def load() -> dict:
    return json.loads(REGISTRY.read_text(encoding='utf-8'))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true', help='只检查，不一致时退出码 1')
    group.add_argument('--update', action='store_true', help='就地更新登记表')
    args = parser.parse_args(argv)

    registry = load()
    problems = []
    fresh = {}
    for relative in registry:
        path = ROOT / relative
        if not path.is_file():
            problems.append(f'{relative}: 文件不存在')
            continue
        actual = digest(path)
        fresh[relative] = actual
        if actual != registry[relative]:
            problems.append(f'{relative}: 登记 {registry[relative][:16]}… 实际 {actual[:16]}…')

    if args.update:
        REGISTRY.write_text(
            json.dumps(fresh, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'已更新 {REGISTRY}（{len(fresh)} 项）')
        if problems:
            print('原不一致项：')
            for line in problems:
                print('  ' + line)
        return 0

    if problems:
        print('受保护文件的哈希与登记不一致：', file=sys.stderr)
        for line in problems:
            print('  ' + line, file=sys.stderr)
        print('\n修复：deploy/protected-customizations.py --update', file=sys.stderr)
        return 1
    print(f'受保护文件一致（{len(fresh)} 项）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
