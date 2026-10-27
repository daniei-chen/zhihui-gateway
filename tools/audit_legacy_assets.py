"""K52：遗留资产在**本仓库内**的引用盘点（只读静态扫描）。

## 能做什么 / 不能做什么

能做：在源码/配置/文档里查每个遗留资产是否还被引用，给出"零引用 = 候选清理"
的**本仓库内**证据。

不能做：生产机上的进程、systemd unit、nginx server 块、访问日志统计 ——
那些必须到目标机执行（脚本末尾列出具体命令）。

## 已知局限（读结论前必看）

关键词命中的是**文本**，不是"依赖"。例如 `sub2api` 会命中
`account-status.ts` 里的一句注释（"`sub2api.json` 这类是…"），而且 `8081`
这种纯数字会命中金额/字节数等同形值。所以：

* 本脚本的结论是**候选清单**，不是"可以删"的判据；
* 真正的处置必须结合：该文件是否在 import 链上、生产进程/配置是否引用、
  访问日志是否还有流量；
* 因此输出里"运行时源码引用"只给**文件+行**，不下"在用/不用"结论。

## 运行

    python -X utf8 tools/audit_legacy_assets.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'node_modules', '.git', 'evidence', '__pycache__', '.next', 'out',
             'handoff-20261024-1236', '.zcode'}
#: 自指排除：审计工具自己的模式定义、以及纯历史文档目录。
#: 不排除的话每个资产都会"命中自己"，结论全是噪声。
SELF_EXCLUDE = {'tools/audit_legacy_assets.py'}
DOC_ONLY_DIRS = ('handoff-',)

#: "执行路径"只认真正的运行时源码树。recovery/、ops/、baseline/、contracts/、
#: tools/ 下的文件即使有 .json/.py 后缀，也只是恢复素材或审计脚本，不会被加载。
RUNTIME_PREFIXES = ('manager/', 'qtg/', 'src/three-platform/', 'release/')

#: 每个遗留资产：标识、说明、扫描用的模式（正则）
ASSETS = [
    ('sub2api_8081', '旧首页 sub2api（8081）',
     [r'\bsub2api\b', r'8081']),
    ('shadow_8790', '影子实例 8790（qoder-proxy 容器）',
     [r'\b8790\b']),
    ('bff_8791', '旧 BFF 端口 8791', [r'\b8791\b']),
    ('bff_8792', '旧 BFF 端口 8792', [r'\b8792\b']),
    ('manager_backups', 'workbuddy-manager backup-* 目录',
     [r'backup-\d{8}', r'/opt/workbuddy-manager/backup']),
    ('gateway_ops_snapshots', 'gateway-ops snapshots（M0 前配置快照）',
     [r'gateway-ops', r'pre-m0-']),
    ('wb3_shadow', '旧 __wb3 影子路由', [r'__wb3']),
    ('trae_work', 'Trae 工作目录 /opt/trae-work', [r'/opt/trae-work']),
    ('qoder_hub', 'Qoder 数据目录 /opt/qoder-hub', [r'/opt/qoder-hub']),
]


def iter_files():
    for path in ROOT.rglob('*'):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in {'.py', '.ts', '.tsx', '.js', '.mjs', '.json',
                                       '.md', '.sh', '.ps1', '.yml', '.yaml',
                                       '.conf', '.txt', '.service', '.toml', ''}:
            continue
        if path.stat().st_size > 2_000_000:
            continue
        yield path


def scan() -> dict:
    patterns = {key: [re.compile(p) for p in pats] for key, _, pats in ASSETS}
    hits: dict[str, list[dict]] = {key: [] for key in patterns}
    for path in iter_files():
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in SELF_EXCLUDE:
            continue
        for key, regs in patterns.items():
            if not any(r.search(text) for r in regs):
                continue
            # 记录命中的前 3 行，便于人工判断"引用"还是"只是文档提到"
            lines = []
            for i, line in enumerate(text.splitlines(), 1):
                if any(r.search(line) for r in regs):
                    lines.append({'line': i, 'text': line.strip()[:160]})
                    if len(lines) >= 3:
                        break
            hits[key].append({'file': rel, 'samples': lines})
    return hits


def classify(entry_files: list[dict]) -> tuple[str, str]:
    """按"引用它的文件类型"给处置建议。

    分三类，避免把文档里的历史说明当成"还在用"：
      * code_references — .py/.ts/.tsx/.js/.sh/.yml/.conf：**可能仍在执行路径**
      * doc_references  — .md/.txt：历史说明，清理时一并更新即可
      * 零引用          — 本仓库内找不到，需到生产确认后再删
    """
    code = [f for f in entry_files
            if f['file'].startswith(RUNTIME_PREFIXES)
            and Path(f['file']).suffix.lower() not in {'.md', '.txt'}]
    docs = [f for f in entry_files if f not in code]
    if code:
        return 'code_references', f'{len(code)} 个运行时源码引用，{len(docs)} 个文档'
    if docs:
        return 'doc_only', f'仅 {len(docs)} 个文档提到（历史说明），无代码引用'
    return 'no_reference', '本仓库内零引用'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', type=Path)
    args = ap.parse_args(argv)

    hits = scan()
    rows = []
    for key, label, _ in ASSETS:
        files = hits[key]
        kind, detail = classify(files)
        rows.append({'asset': key, 'label': label, 'kind': kind,
                     'detail': detail, 'files': files[:12],
                     'file_count': len(files)})
        marker = {'code_references': 'CODE', 'doc_only': 'DOC ',
                  'no_reference': 'NONE'}[kind]
        print(f'{marker} {label}: {detail}')
        if kind == 'code_references':
            # 只打印真正落在运行时源码树里的引用 —— 那才是"清理会不会弄坏"
            # 的判据；文档/审计素材里的同名串没有这个意义。
            for f in files:
                if f['file'].startswith(RUNTIME_PREFIXES):
                    print(f'       [runtime] {f["file"]}:{f["samples"][0]["line"]}')
        elif kind == 'doc_only':
            for f in files[:2]:
                print(f'       {f["file"]}:{f["samples"][0]["line"]}')

    print()
    print('需在生产执行（本脚本不判定）:')
    for cmd in (
        'systemctl list-units --all | grep -Ei "sub2api|gateway|workbuddy"',
        'docker ps -a --format "{{.Names}} {{.Image}} {{.Status}}"',
        'ss -ltnp | grep -E ":(8081|8790|8791|8792)"',
        'nginx -T | grep -nE "server_name|location"    # 全量 location 清单',
        'awk \'{print $7}\' /var/log/nginx/access.log | cut -d/ -f1-2 | sort | uniq -c | sort -rn | head -30',
        'ls -d /opt/workbuddy-manager/backup-* | xargs -r du -sh',
        'find /opt -maxdepth 2 -name "*.bak" -o -name "*.pre*" | head -30',
    ):
        print(f'    $ {cmd}')

    if args.json:
        counts = Counter(r['kind'] for r in rows)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {'schema': 'zhihui-k52-legacy/v1', 'rows': rows,
             'summary': dict(counts),
             'runtime_checks_required': True},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
