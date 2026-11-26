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
    import subprocess
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

    # `source_commit` 必须指向**真正被打包的那个提交**。
    #
    # 这一条曾经是纯手工维护的，结果是公开的 `/api/release` 长期报着一个更早的
    # 提交：v1.5.44 实际由 `72358a0` 构建，`source_commit` 里写的却是 v1.5.43 的
    # `76f5b0b`。发布判据（运维手册第 1 节）就是「`source_commit` 必须等于你打的
    # tag 的完整哈希」—— 一旦它漂了，这条判据要么误报失败，要么被当成噪声忽略，
    # 两种都比没有更糟。
    #
    # 工作区不干净时**不能**写 source_commit：那时 HEAD 描述不了将要打包的内容。
    # 但哈希重算是另一回事 —— 改完文档顺手对齐哈希是常态，不能因此整条命令失败。
    dirty = _git('status', '--porcelain', '--untracked-files=no')
    head = _git('rev-parse', 'HEAD')
    previous = data.get('source_commit')
    # **只在改版本号时**写 source_commit / base_*。
    #
    # 原因是结构性的事实：`source_commit` 说的是「将要被打成包的那个提交」，而它
    # 自己就在包里 —— 一旦锁定到打 tag 的提交，后续任何一次元数据提交都会让
    # 「HEAD ≠ 已发布提交」，跟着 HEAD 走就成了永远差一个提交的追逐。
    # 所以：升版本时锁定当时 HEAD（= 随后要打 tag 的提交），之后不再自作主张。
    if previous_version != version:
        if dirty:
            print('  ⚠ 工作区有未提交改动，本次不更新 source_commit（先提交再升版本）：')
            for line in dirty.splitlines()[:5]:
                print(f'    {line}')
        else:
            if previous != head:
                data['source_commit'] = head
                print(f'  source_commit: {str(previous)[:12]} → {head[:12]}')
            try:
                base_tag = _git('describe', '--tags', '--abbrev=0',
                                f'--exclude=v{version}', 'HEAD')
                data['base_release'] = base_tag
                data['base_commit'] = _git('rev-parse', f'{base_tag}^{{commit}}')
                print(f'  基线: {base_tag}')
            except Exception:  # noqa: BLE001 - 拿不到基线就保持原值
                print('  ⚠ 找不到上一个 tag，base_release / base_commit 保持原值')

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
