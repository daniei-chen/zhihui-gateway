"""检查 `reviewed_patch_files` 是否与当前源码对齐 —— **只读，不写**。

## 这个脚本不做什么

它**不重算、不写回**。写回的事由 `tools/bump_version.py` 负责：它的
`sync_provenance()` 已经遍历 `reviewed_patch_files` 的每一条、
比对 `normalized_hash()`、把变了的条目更新掉（见该函数第 100-110 行）。

一开始我写了个「重算并写回」的版本，等于把 `sync_provenance()` 抄了一遍。
那是重复实现 —— 两套哈希逻辑一旦分叉，修了一边忘了另一边比没有工具更糟。

## 它解决什么问题

`sync_provenance()` 只在跑 `bump_version.py` 时才执行，而它要一个版本号参数。
于是有两个场景没有工具可用：

1. **只改了文档、不打算升版本**（本仓库最常见的情况）。
   忘了对齐哈希的话，改动本身没问题，**下一个提交**才会在 CI 上炸
   —— 见 `docs/OPS-PITFALLS.md` 第十九条。

2. **提交前想确认**清单是最新的，而不想因此改文件。

这两种场景需要的都是「看一眼」，不是「改一遍」。

## 用法

```bash
python tools/check_reviewed_hashes.py      # 退出码 0 = 全部对得上
```

有差异时它会打印出**该怎么修**：

```
python tools/bump_version.py <当前版本号>   # 只对齐哈希，不动版本
```

传当前版本号时 `sync_provenance()` 走的是「版本没变」那条分支，
只重算哈希、不碰 `source_commit`（见该函数第 127-133 行的注释）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / 'server' / 'release-provenance.json'
KEY = 'reviewed_patch_files'


def normalized_hash(path: Path) -> str:
    """与 `tools/bump_version.py` 的 `normalized_hash` 保持完全一致。

    口径是 LF 归一化后的 SHA-256 —— 这个项目在 Windows 上开发、
    Linux 上部署，两种行尾都必须能对上。
    """
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return hashlib.sha256(raw).hexdigest()
    normalized = text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
    return hashlib.sha256(normalized).hexdigest()


def main() -> int:
    try:
        data = json.loads(PROVENANCE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        print(f'读不了 {PROVENANCE}: {exc}', file=sys.stderr)
        return 2

    files = data.get(KEY)
    if not isinstance(files, dict) or not files:
        print(f'{KEY} 缺失或为空', file=sys.stderr)
        return 2

    missing: list[str] = []
    stale: list[str] = []
    for relative, expected in sorted(files.items()):
        path = ROOT / relative
        if not path.is_file():
            missing.append(relative)
        elif normalized_hash(path) != expected:
            stale.append(relative)

    version = str(data.get('version') or '')
    for relative in missing:
        print(f'文件不存在  {relative}')
    for relative in stale:
        print(f'哈希过期    {relative}')

    if not missing and not stale:
        print(f'{len(files)} 个已审阅文件全部对得上（版本 {version}）')
        return 0

    print(f'\n共 {len(missing) + len(stale)} 处不一致。', end='')
    if stale:
        print(f'\n对齐哈希（不改版本、不动 source_commit）：\n'
              f'    python tools/bump_version.py {version}')
    if missing:
        print('\n文件不存在不是哈希问题 —— 清单可能列了已删除的文件，'
              '需要人工确认，不要直接删条目。')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
