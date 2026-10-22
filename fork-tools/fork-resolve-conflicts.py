#!/usr/bin/env python3
"""fork-resolve-conflicts.py —— 只自动解决已知模式的合并冲突（保守模式）。

输入：合并工作树路径（argv[1]）+ 冲突文件列表（stdin）
行为：已知冲突按 fork-manifest.json 记录的解法改写；任一未知 → 不写盘、退出 1；解决后 git add 但不提交。
"""
import re
import subprocess
import sys
import pathlib

# 已知解法。每条都是对 2026-10-22 v2.4.0→v2.8.3 实升时真实冲突块的精确匹配。
KNOWN = {
    'server/main.py': [
        (r'<<<<<<< HEAD\n(from \.routers import budgets)\n=======\n(from \.services import [^\n]+)\n>>>>>>> [^\n]+\n',
         lambda m: m.group(2) + "\n" + m.group(1) + "\n"),
    ],
    'server/routers/gateway.py': [
        (r'<<<<<<< HEAD\n(from \.\. import observability)\nfrom \.\.services import ([^\n]+)\n=======\nfrom \.\.services import ([^\n]+)\n>>>>>>> [^\n]+\n',
         lambda m: m.group(1) + "\nfrom ..services import " + ', '.join(
             sorted(set(x.strip() for x in (m.group(2) + ',' + m.group(3)).split(',') if x.strip()))) + "\n"),
        (r'<<<<<<< HEAD\n(from \.\. import observability)\n=======\n(from \.\.services import [^\n]+)\n>>>>>>> [^\n]+\n',
         lambda m: m.group(1) + "\n" + m.group(2) + "\n"),
    ],
}

# 上游已废弃、本地必删的整段（冲突块整体删除）
DELETE_BLOCK = {
    'server/routers/system.py': [
        r"<<<<<<< HEAD\n@router\.post\('/upstream-ref'\).*?>>>>>>> [^\n]+\n",
    ],
    'server/services/updater.py': [
        r"<<<<<<< HEAD\n# 本地定制：原上游 Sliverkiss/workbuddy2api.*?>>>>>>> [^\n]+\n",
    ],
}


def _which_key(path: pathlib.Path) -> str:
    text = str(path)
    for key in list(KNOWN) + list(DELETE_BLOCK):
        if text.endswith(key):
            return key
    return ''


def resolve(path: pathlib.Path) -> bool:
    text = path.read_text(encoding='utf-8')
    if '<<<<<<<' not in text:
        return True
    key = _which_key(path)
    if not key:
        return False
    orig = text
    for pat, fn in KNOWN.get(key, []):
        text = re.sub(pat, fn, text, flags=re.S)
    for pat in DELETE_BLOCK.get(key, []):
        text = re.sub(pat, '', text, flags=re.S)
    if '<<<<<<<' in text:
        return False
    text = _normalize_imports(text)
    if text != orig:
        path.write_text(text, encoding='utf-8')
        print("  已解决 " + key)
    return True



def _normalize_imports(text: str) -> str:
    """合并重复的同模块 import 行（保留首个出现位置，其余删除）。

    自动解决冲突时可能出现两行 `from .services import ...`（原有一行 + 上游新增一行），
    合并成一行并去重排序，避免 pyflakes 噪声和重复定义。
    """
    lines = text.split('\n')
    seen = {}
    out = []
    for line in lines:
        m = re.match(r'^(from (\.+[\w.]*) import )(.+)$', line)
        if not m:
            out.append(line)
            continue
        mod = m.group(2)
        names = [x.strip() for x in m.group(3).split(',') if x.strip()]
        if mod in seen:
            seen[mod][1].extend(names)
            continue
        entry = [m.group(1).rstrip() + ' ', names]
        seen[mod] = entry
        out.append(entry)
    if not seen:
        return text
    # 重建
    result = []
    for item in out:
        if isinstance(item, list):
            prefix, names = item
            result.append(prefix + ', '.join(sorted(set(names))))
        else:
            result.append(item)
    return '\n'.join(result)

def main() -> int:
    root = pathlib.Path(sys.argv[1])
    files = [l.strip() for l in sys.stdin if l.strip()]
    if not files:
        return 0
    unresolved = []
    for f in files:
        p = root / f
        if not p.exists() or not resolve(p):
            unresolved.append(f)
            continue
        subprocess.run(['git', '-C', str(root), 'add', f], check=False)
    if unresolved:
        print("  以下文件存在未知冲突，需人工处理：")
        for f in unresolved:
            print("    " + f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
