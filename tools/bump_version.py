#!/usr/bin/env python3
"""一键把产品版本推到所有发布面，并重新对齐 provenance 哈希。

## 为什么需要

版本号散落在 6 个文件里，`test_release_consistency` / `test_changelog` 要求它们
完全一致，漏一处门禁就红：

    VERSION
    server/product-release.json
    server/release-provenance.json      （version + release_id）
    server/writer-capabilities.json     （只有 release_id —— version 是协议版本）
    web/lib/product.ts                  （PRODUCT_VERSION）
    CHANGELOG.md  ＋ server/CHANGELOG.md（两份必须逐字节相同）

而且 `server/release-provenance.json` 的 `reviewed_patch_files` 里钉着 145 个
源文件哈希 —— 改了其中任何一个文件却不更新这里，`test_release_provenance_integrity`
就会失败。手工维护这套东西的结果是已知的：它曾经整体停在 v1.3.3，后来又漂了
10 个文件，而没人发现。

## 用法

    python tools/bump_version.py 1.5.46        # 先改 VERSION 再同步
    python tools/bump_version.py               # 只按当前 VERSION 重新对齐

哈希口径与 `server/tests/test_release_provenance_integrity.py` 一致：取
`sha256(LF 归一化后的 utf-8 字节)`，这样无论仓库按 CRLF 还是 LF 检出都能对上。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / 'server/release-provenance.json'

# Windows 的默认控制台代码页是 GBK，`print` 里出现 GBK 编不了的字（比如 ⚠）
# 会直接抛 UnicodeEncodeError，把一次本该成功的版本同步变成失败。统一改成
# UTF-8 + replace：宁可显示成问号，也不要因为一行提示把发布流程打断。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8', newline='\n')


def normalized_hash(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(
        text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')).hexdigest()


def sync_surfaces(version: str) -> int:
    product = json.loads((ROOT / 'server/product-release.json').read_text(encoding='utf-8'))
    product['version'] = version
    write_json(ROOT / 'server/product-release.json', product)

    writer = json.loads((ROOT / 'server/writer-capabilities.json').read_text(encoding='utf-8'))
    writer['release_id'] = f'v{version}'
    # `version` 是 **writer 协议版本**（整数 1），不是产品版本 —— 别跟着改，
    # 否则服务会以「unsupported writer descriptor」被拒启动。
    write_json(ROOT / 'server/writer-capabilities.json', writer)

    frontend = ROOT / 'web/lib/product.ts'
    text = frontend.read_text(encoding='utf-8')
    replaced, count = re.subn(r"PRODUCT_VERSION(\s*=\s*)'[^']*'",
                              rf"PRODUCT_VERSION\1'{version}'", text)
    if count != 1:
        print(f'web/lib/product.ts 里找不到唯一的 PRODUCT_VERSION（找到 {count} 处）')
        return 1
    frontend.write_text(replaced, encoding='utf-8', newline='\n')

    # 两份更新日志必须逐字节相同。
    (ROOT / 'server/CHANGELOG.md').write_bytes((ROOT / 'CHANGELOG.md').read_bytes())
    print(f'  版本面已同步为 {version}')
    return 0


def _git(*args: str) -> str:
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                          check=True, timeout=20).stdout.decode().strip()


def sync_provenance(version: str) -> int:
    data = json.loads(PROVENANCE.read_text(encoding='utf-8'))
    previous_version = str(data.get('version') or '')
    changed: list[str] = []
    missing: list[str] = []
    files = data.get('reviewed_patch_files') or {}
    for relative, expected in sorted(files.items()):
        path = ROOT / relative
        if not path.is_file():
            missing.append(relative)
            continue
        actual = normalized_hash(path)
        if actual != expected:
            files[relative] = actual
            changed.append(relative)
    data['reviewed_patch_files'] = files
    data['version'] = version
    data['release_id'] = f'v{version}'

    # Source edits are not build/deployment evidence. A new version must not
    # inherit the old release's commit, build digests or production status.
    # GitHub packaging binds the eventual checkout SHA after this commit exists.
    if previous_version != version or changed:
        data.update({
            'candidate_only': True,
            'production_deployed': False,
            'signed': False,
            'source_kind': 'source-candidate',
            'source_commit': None,
            'frontend_rebuilt': False,
        })
        for key in ('source_input_sha256', 'frontend_input_sha256'):
            data.pop(key, None)
        if previous_version != version and re.fullmatch(r'\d+\.\d+\.\d+', previous_version):
            data['base_release'] = 'v' + previous_version
        try:
            data['base_commit'] = _git('rev-parse', 'HEAD')
        except (OSError, subprocess.SubprocessError):
            data.pop('base_commit', None)
        print('  版本或源码发生变化，标为源码候选；实际来源提交由构建流程绑定')

    write_json(PROVENANCE, data)

    print(f'  provenance 版本对齐；哈希更新 {len(changed)} 个')
    for name in changed:
        print(f'    {name}')
    if missing:
        print(f'  ⚠ 清单里有 {len(missing)} 个文件已不存在，需要手工处理：')
        for name in missing:
            print(f'    {name}')
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('version', nargs='?', help='新版本（省略则用当前 VERSION）')
    args = parser.parse_args(argv)

    if args.version:
        if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
            print(f'不是语义化版本：{args.version!r}')
            return 1
        (ROOT / 'VERSION').write_text(args.version + '\n', encoding='ascii', newline='\n')

    version = (ROOT / 'VERSION').read_text(encoding='ascii').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        print(f'VERSION 内容不是语义化版本：{version!r}')
        return 1

    if sync_surfaces(version) != 0:
        return 1
    return sync_provenance(version)


if __name__ == '__main__':
    sys.exit(main())
