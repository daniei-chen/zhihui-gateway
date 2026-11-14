"""按改动挑出该跑的测试，避免每次都等全量。

## 为什么需要它

全量有 371 个测试文件，跑一遍要十几分钟以上。改一行代码等十几分钟，实际上等于
没有验证手段 —— 人会开始「猜哪几个相关」，猜错了就漏。

这个脚本把「猜」变成机械判断：

    python server/tests/select_tests.py            # 看改动对应哪些测试
    python server/tests/select_tests.py --run      # 直接跑

## 怎么判断相关

三条规则，从严到宽：

1. **文件名命中**：改动 `ledger.py` 时，`test_ledger*.py`、`test_*ledger*.py` 都跑；
2. **内容引用**：测试文件里出现该模块名（`import ... ledger` / `ledger.`）也跑；
3. **核心常跑**：发布一致性、变更日志规范、能力声明这类守卫，任何改动都跑 ——
   它们便宜，且能立刻发现版本号 / 清单不一致。

## 为什么不并发

这套测试**共用工作区里的 `data/` 目录**（`config.DB_PATH` 在 import 时确定，
用例里改不了）。两个 pytest 进程同时读写同一个库，会互相污染，表现成随机的假失败。
所以这里只做「挑得准」，不做「跑得快」。

## 用什么作为改动基准

默认 `HEAD`：未提交的改动 + 上次提交。也可以显式给 `--base`，例如
`--base v1.2.57` 用于对比一个版本以来的全部变化。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

#: 任何改动都要跑的守卫测试（便宜且能立刻抓到不一致）。
ALWAYS = (
    'test_release_consistency.py',
    'test_release_provenance_integrity.py',
    'test_changelog.py',
    'test_docs_packaging.py',
    'test_writer_capabilities.py',
)

#: 改动这些目录才值得去看；其余（文档、前端）不选测试。
SOURCE_PREFIXES = ('server/', 'deploy/')


def changed_files(base: str) -> list[str]:
    """相对 `base` 改动过的文件；工作区未提交的也算进来。"""
    out: list[str] = []
    for args in (['diff', '--name-only', base],
                 ['diff', '--name-only', '--cached'],
                 ['diff', '--name-only'],
                 ['ls-files', '--others', '--exclude-standard']):
        try:
            done = subprocess.run(['git', '-C', str(REPO), *args],
                                  capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        out.extend(line.strip() for line in done.stdout.splitlines() if line.strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for path in out:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def modules_of(paths: list[str]) -> tuple[set[str], list[Path]]:
    """把改动路径拆成「关心的源模块」与「直接改到的测试文件」。

    测试文件不算模块：改了 `server/tests/test_x.py`，该跑的是它自己，而不是
    把所有名字里含 `test_x` 的文件都跑一遍。早先没区分这一点，于是选中了
    一堆无关文件，还把选择脚本自己也当成了模块。
    """
    names: set[str] = set()
    direct: list[Path] = []
    for path in paths:
        if not path.endswith('.py'):
            continue
        if path.startswith('server/tests/'):
            # 只有真的会被 pytest 收集的才算（`test_*.py`）；选择脚本自身、
            # 辅助模块改了不需要"跑自己"。
            name = Path(path).name
            candidate = HERE / name
            if name.startswith('test_') and candidate.is_file():
                direct.append(candidate)
            continue
        if not path.startswith(SOURCE_PREFIXES):
            continue
        stem = Path(path).stem
        if stem.startswith('_') or stem in ('__init__', 'conftest'):
            continue
        names.add(stem)
    return names, direct


def select(modules: set[str], direct: list[Path] | None = None) -> list[Path]:
    """挑出相关的测试文件（含固定的守卫与直接改到的）。"""
    chosen: list[Path] = list(direct or [])
    for path in sorted(HERE.glob('test_*.py')):
        name = path.name
        if name in ALWAYS:
            chosen.append(path)
            continue
        # 注意：`ALWAYS` 里列了但仓库里没有的名字会被 glob 自然跳过，
        # 所以清单可以写"理想守卫集"，不必与当前文件严格一一对应。
        if not modules:
            continue
        stem = path.stem
        # 规则一：文件名里带模块名（去掉 test_ 前缀后做包含判断）
        bare = stem[5:] if stem.startswith('test_') else stem
        if any(mod in bare or bare in mod for mod in modules):
            chosen.append(path)
            continue
        # 规则二：文件内容引用了该模块
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            continue
        if any(mod in text for mod in modules):
            chosen.append(path)
    # 去重并保持稳定顺序
    seen: set[Path] = set()
    result: list[Path] = []
    for path in chosen:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='按改动挑测试文件')
    parser.add_argument('--base', default='HEAD', help='改动基准（默认 HEAD）')
    parser.add_argument('--run', action='store_true', help='挑完直接跑')
    parser.add_argument('--quiet', action='store_true', help='只输出文件路径')
    parser.add_argument('--shared-data', action='store_true',
                        help='共用工作区 data 目录（更快，但结果可能互相干扰）')
    args = parser.parse_args(argv)

    paths = changed_files(args.base)
    modules, direct = modules_of(paths)
    picked = select(modules, direct)

    if not args.quiet:
        print(f'改动文件 {len(paths)} 个，涉及模块 {len(modules)} 个')
        if modules:
            print('  模块：' + ', '.join(sorted(modules)))
        print(f'选中测试 {len(picked)} 个：')

    for path in picked:
        print(path.relative_to(REPO).as_posix())

    if not args.run:
        return 0
    if not picked:
        print('没有需要跑的测试（改动不涉及 server/ 或 deploy/）', file=sys.stderr)
        return 0

    cmd = [sys.executable, '-m', 'pytest', *[str(p) for p in picked],
           '-q', '-p', 'no:cacheprovider', '--tb=short']
    if not args.quiet:
        print('\n执行：' + ' '.join(cmd[:6]) + f' … 共 {len(picked)} 个文件\n')

    # 用独立的数据目录跑。
    #
    # 这套测试共用工作区的 `data/`：`config.DB_PATH` 在 import 时就定了，用例里
    # 改不了。于是前一个文件留下的预算预留、死信、限流状态会渗到后一个文件里，
    # 表现成「同一批测试这次过下次挂」。
    #
    # 2026-11-15 实测：同一批 58 个文件，共用目录跑出 25 个失败；换成独立目录后
    # 降到 9 个，剩下的都是与本次改动无关的既有失败。
    env = dict(os.environ)
    if not args.shared_data:
        env['WB_DATA_DIR'] = tempfile.mkdtemp(prefix='wb-test-')
        if not args.quiet:
            print(f'独立数据目录：{env["WB_DATA_DIR"]}\n')
    return subprocess.call(cmd, cwd=str(REPO), env=env)


if __name__ == '__main__':
    raise SystemExit(main())
