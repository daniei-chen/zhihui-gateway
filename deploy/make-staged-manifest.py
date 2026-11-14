#!/usr/bin/env python3
"""从 git 生成「期望哈希清单」，供 `verify-staged-files.sh` 对比已组装的目录。

## 为什么从 git 而不是从磁盘

发布组装的输入是**某个已推送的提交**，不是工作区。磁盘上可能还有未提交的改动，
拿它算哈希会把「本地试过但没发出去的东西」当成期望值。

## 用法

    make-staged-manifest.py --repo <仓库路径> --ref <提交或标签> \
        --out <清单路径> <相对路径> [<相对路径> ...]

输出格式 `sha256 路径`，与 `sha256sum` 一致，可直接喂给校验脚本。

文件在 ref 上不存在时**直接失败** —— 静默跳过会让清单少一项，而少了的那项恰好是
最需要校验的。
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


def blob_hash(repo: Path, ref: str, rel: str) -> str | None:
    result = subprocess.run(
        ['git', '-C', str(repo), 'cat-file', 'blob', f'{ref}:{rel}'],
        capture_output=True)
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('paths', nargs='+')
    options = parser.parse_args(argv)

    if not (options.repo / '.git').exists():
        print(f'FAIL: 不是 git 仓库: {options.repo}', file=sys.stderr)
        return 2

    lines: list[str] = []
    missing: list[str] = []
    for rel in options.paths:
        digest = blob_hash(options.repo, options.ref, rel)
        if digest is None:
            missing.append(rel)
            continue
        lines.append(f'{digest}  {rel}')

    if missing:
        print('FAIL: 这些路径在目标提交上不存在：', file=sys.stderr)
        for rel in missing:
            print(f'  {rel}', file=sys.stderr)
        return 1

    if not lines:
        print('FAIL: 没有生成任何条目', file=sys.stderr)
        return 2

    options.out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'MANIFEST-OK entries={len(lines)} out={options.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
