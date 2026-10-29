#!/opt/workbuddy-manager/venv/bin/python
"""资产生命周期审计与受控清理（U20，2026-10-28）。

## 为什么需要它

服务器上几类资产的保留策略是各写各的：`releases/` 42 个 731M、`/root/backups/workbuddy-daily/`
36 份 1.2G、`data/` 里 3 个 `.bak-*.gz` 与 2 个 `pre-switch-*.db`（约 350M）、149 个
`.budget-owners/` 锁文件。没有任何一处能回答「哪些可以安全删、删了怎么恢复」。

本脚本把「能删什么」写成可审计的规则，并且**默认只读**：

  * `--audit`（默认）：分类、大小、可清理候选、保留理由，逐条打印；
  * `--apply --confirm-destroy <token>`：真正删除，token 必须等于本次审计算出的
    摘要（防止手滑 `--apply`）；
  * 硬性排除（任何情况下都不删）：
      - `releases/current` 解析出的当前 release
      - 最近 3 个 release（按目录 mtime）
      - `data/manager.db` 本体、`manager.db-wal`、`manager.db-shm`
      - `.budget-owners/` 下的锁文件（未决预留的存活证据；只能由 budget 层按状态处理）
      - 任何正在被进程打开的路径（`/proc/*/fd` 反查）

## 用法

    asset_lifecycle.py --audit
    asset_lifecycle.py --audit --json
    asset_lifecycle.py --apply --confirm-destroy <token>

退出码：0 成功；2 参数错误；3 清理被硬性排除拦下；4 无有效候选。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

PROD = Path('/opt/workbuddy-manager')
RELEASES = PROD / 'releases'
DATA = PROD / 'data'
BACKUPS = Path('/root/backups/workbuddy-daily')

KEEP_RELEASES = 3
KEEP_BACKUP_DAYS = 14
KEEP_BACKUP_MIN_COUNT = 3
KEEP_SNAPSHOTS = 2

#: 任何情况下都不删的路径片段
HARD_DENY_PARTS = ('manager.db', 'manager.db-wal', 'manager.db-shm')
HARD_DENY_DIRS = ('.budget-owners',)


def human(size: int) -> str:
    value = float(size)
    for unit in ('B', 'K', 'M', 'G'):
        if value < 1024 or unit == 'G':
            return '%.1f%s' % (value, unit)
        value /= 1024
    return '%.1fG' % value


#: 快照成员的后缀，按「先长后短」顺序剥离（`.db-wal` 必须先于 `.db`）
_SNAPSHOT_SUFFIXES = ('-wal', '-shm', '.gz', '.db')

_VERSION_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)')
_DATE_RE = re.compile(r'\.(\d{8})(?:\.(\d+))?$')


def release_order_key(name: str):
    """release 目录的**版本**排序键 —— 不用 mtime。

    为什么不能用 mtime：2026-10-28 02:31 有一次批量操作把 `v10.7.0`–`v10.7.7`
    的目录 mtime 全部改写成同一秒，只留下亚秒级差异。按 mtime 取「最近 3 个」
    于是挑中 `v10.7.5`/`v10.7.7`，把**真正的回退目标 `v10.7.8`/`v10.7.9`** 判成
    可删 —— 清理一次就会删掉最近两个可回退版本。

    这里按目录名里的语义版本排序（主.次.修订 → 日期 → 序号），mtime 只作最后
    的稳定化键；没有版本号的目录（`baseline-audit-*`）排在最后，先被回收。
    """
    match = _VERSION_RE.match(name)
    if not match:
        return (0, (0, 0, 0), (0, 0), name)
    version = tuple(int(part) for part in match.groups())
    date = _DATE_RE.search(name)
    stamp = (int(date.group(1)), int(date.group(2) or 0)) if date else (0, 0)
    return (1, version, stamp, name)


def snapshot_key(name: str) -> str:
    """把快照成员的文件名归一到「快照身份」。

    一个快照是**一组文件**（`.db` 主体 + 可能的 `-wal`/`-shm` + `.gz` 变体）。
    `pre-switch-20261028-005502.db` 与它的 `.db-wal`/`.db-shm` 归一到
    `pre-switch-20261028-005502`。

    为什么必须有这一步：早期版本把每个**文件**当独立个体按 mtime 排序，
    而 `-wal`/`-shm` 的 mtime 总是比 `.db` 主体更晚，于是它们占满了
    「保留最近 N 个」的名额，**最新那个快照的 .db 主体反而被判成可清理** ——
    删掉主体、留下孤立的 -wal/-shm，等于把唯一的完整快照换成了垃圾。
    """
    stem = name
    changed = True
    while changed:
        changed = False
        for suffix in _SNAPSHOT_SUFFIXES:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem = stem[:-len(suffix)]
                changed = True
                break
    return stem


def dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def open_paths() -> set[str]:
    """当前被任何进程打开的真实路径（用于排除「正在使用」的资产）。"""
    opened: set[str] = set()
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        fd_dir = '/proc/%s/fd' % pid
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target.startswith('/'):
                    opened.add(target)
        except OSError:
            continue
    return opened


def current_release() -> Path | None:
    link = RELEASES / 'current'
    try:
        return link.resolve()
    except OSError:
        return None


def denied(path: Path, active: Path | None, opened: set[str]) -> str | None:
    text = str(path)
    # 精确匹配文件名：`manager.db.bak-*.gz` 是**旧备份**，不是数据库本体。
    # 用子串匹配会把它们误标成「永不删除」，让真正该回收的空间留在原地。
    if path.name in HARD_DENY_PARTS:
        return '数据库本体/WAL/SHM 永不删除'
    if any(d in path.parts for d in HARD_DENY_DIRS):
        return '预算 owner 锁文件只能由 budget 层按状态处理'
    if active is not None and (path == active or path in active.parents):
        return '当前 release'
    if text in opened:
        return '正被进程打开'
    return None


def audit() -> dict:
    active = current_release()
    opened = open_paths()
    now = time.time()
    report: dict = {'active_release': str(active) if active else None,
                    'now': int(now), 'groups': []}

    # ── releases ──────────────────────────────────────────────────────
    items = []
    if RELEASES.is_dir():
        # 排除软链：releases/current 是软链，跟随 is_dir() 会把「同一个 release」
        # 当成两个条目（还会占掉「最近 3 个」的名额，把真正的旧版本推进 PRUNE）。
        dirs = [p for p in RELEASES.iterdir()
                if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')]
        # 按**版本语义**排序，不按 mtime（见 release_order_key 的说明）
        dirs.sort(key=lambda p: release_order_key(p.name), reverse=True)
        keep = set(dirs[:KEEP_RELEASES])
        if active is not None:
            keep.add(active)
        for path in dirs:
            reason = denied(path, active, opened)
            size = dir_size(path)
            if reason:
                items.append({'path': str(path), 'size': size, 'action': 'keep',
                              'why': reason})
            elif path in keep:
                items.append({'path': str(path), 'size': size, 'action': 'keep',
                              'why': '最近 %d 个 release 之一' % KEEP_RELEASES})
            else:
                items.append({'path': str(path), 'size': size, 'action': 'prune',
                              'why': '旧 release（保留最近 %d 个 + 当前）' % KEEP_RELEASES})
    report['groups'].append({'name': 'releases', 'items': items})

    # ── 每日备份 ──────────────────────────────────────────────────────
    items = []
    if BACKUPS.is_dir():
        files = sorted(BACKUPS.glob('recovery-*.tar.gz'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            age_days = (now - path.stat().st_mtime) / 86400
            size = path.stat().st_size
            reason = denied(path, active, opened)
            if reason:
                items.append({'path': str(path), 'size': size, 'action': 'keep', 'why': reason})
            elif index < KEEP_BACKUP_MIN_COUNT or age_days <= KEEP_BACKUP_DAYS:
                items.append({'path': str(path), 'size': size, 'action': 'keep',
                              'why': '最近 %d 份或 %d 天内（age=%.1fd）'
                                     % (KEEP_BACKUP_MIN_COUNT, KEEP_BACKUP_DAYS, age_days)})
            else:
                items.append({'path': str(path), 'size': size, 'action': 'prune',
                              'why': '超过 %d 天且不在最近 %d 份（age=%.1fd）'
                                     % (KEEP_BACKUP_DAYS, KEEP_BACKUP_MIN_COUNT, age_days)})
    report['groups'].append({'name': 'daily-backups', 'items': items})

    # ── data 目录里的快照 ─────────────────────────────────────────────
    # 按「快照身份」分组后再排序：保留最近 N 个**快照**（含其 .db / -wal / -shm
    # 全部成员），而不是最近 N 个文件。见 snapshot_key() 的说明。
    items = []
    if DATA.is_dir():
        groups: dict = {}
        for path in DATA.iterdir():
            if not path.is_file():
                continue
            if not (path.name.startswith('pre-switch-') or '.bak-' in path.name):
                continue
            groups.setdefault(snapshot_key(path.name), []).append(path)
        ordered = sorted(groups.items(),
                         key=lambda kv: max(p.stat().st_mtime for p in kv[1]),
                         reverse=True)
        for index, (_key, members) in enumerate(ordered):
            for path in sorted(members, key=lambda p: p.name):
                size = path.stat().st_size
                reason = denied(path, active, opened)
                if reason:
                    items.append({'path': str(path), 'size': size, 'action': 'keep', 'why': reason})
                elif index < KEEP_SNAPSHOTS:
                    items.append({'path': str(path), 'size': size, 'action': 'keep',
                                  'why': '最近 %d 个快照之一' % KEEP_SNAPSHOTS})
                else:
                    items.append({'path': str(path), 'size': size, 'action': 'prune',
                                  'why': '旧快照（保留最近 %d 个）' % KEEP_SNAPSHOTS})
    report['groups'].append({'name': 'data-snapshots', 'items': items})

    # ── 永不删除但需要报告 ────────────────────────────────────────────
    items = []
    owners = DATA / '.budget-owners'
    if owners.is_dir():
        count = sum(1 for _ in owners.iterdir())
        items.append({'path': str(owners), 'size': dir_size(owners), 'action': 'keep',
                      'why': '%d 个 owner 锁文件：未决预留的存活证据，只能由 budget 层处理' % count})
    for name in ('manager.db', 'manager.db-wal', 'manager.db-shm'):
        path = DATA / name
        if path.exists():
            items.append({'path': str(path), 'size': path.stat().st_size, 'action': 'keep',
                          'why': '数据库本体/WAL/SHM，永不删除'})
    report['groups'].append({'name': 'never-touch', 'items': items})

    # ── 汇总 ──────────────────────────────────────────────────────────
    prune_bytes = 0
    prune_count = 0
    for group in report['groups']:
        for item in group['items']:
            if item['action'] == 'prune':
                prune_bytes += item['size']
                prune_count += 1
    report['summary'] = {'prune_count': prune_count, 'prune_bytes': prune_bytes,
                         'prune_human': human(prune_bytes)}
    token = hashlib.sha256(json.dumps(
        sorted(i['path'] for g in report['groups'] for i in g['items'] if i['action'] == 'prune'),
        sort_keys=True).encode()).hexdigest()[:16]
    report['confirm_token'] = token
    return report


def print_report(report: dict) -> None:
    print('active release : %s' % report['active_release'])
    for group in report['groups']:
        print('\n== %s ==' % group['name'])
        for item in group['items']:
            mark = 'PRUNE' if item['action'] == 'prune' else 'keep '
            print('  %s %-10s %s' % (mark, human(item['size']), item['path']))
            print('         %s' % item['why'])
    summary = report['summary']
    print('\n可清理：%d 项 / %s' % (summary['prune_count'], summary['prune_human']))
    print('确认令牌：%s' % report['confirm_token'])
    print('（默认只读。真正删除需 --apply --confirm-destroy %s）' % report['confirm_token'])


def parse_group_list(text: str) -> set:
    return {part.strip() for part in text.replace('，', ',').split(',') if part.strip()}


def apply_prune(report: dict, token: str, only: str = '', exclude: str = '') -> int:
    if token != report['confirm_token']:
        print('令牌不匹配：拒绝执行。当前令牌 %s' % report['confirm_token'], file=sys.stderr)
        return 2
    only_groups = parse_group_list(only)
    exclude_groups = parse_group_list(exclude)
    known = {group['name'] for group in report['groups']}
    unknown = (only_groups | exclude_groups) - known
    if unknown:
        print('未知分组：%s（可选：%s）' % (', '.join(sorted(unknown)), ', '.join(sorted(known))),
              file=sys.stderr)
        return 2
    active = Path(report['active_release']) if report['active_release'] else None
    opened = open_paths()
    removed = 0
    freed = 0
    for group in report['groups']:
        if only_groups and group['name'] not in only_groups:
            print('SKIP 分组 %s（--only 未包含）' % group['name'], file=sys.stderr)
            continue
        if group['name'] in exclude_groups:
            print('SKIP 分组 %s（--exclude）' % group['name'], file=sys.stderr)
            continue
        for item in group['items']:
            if item['action'] != 'prune':
                continue
            path = Path(item['path'])
            reason = denied(path, active, opened)
            if reason:
                print('SKIP %s（%s）' % (path, reason), file=sys.stderr)
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                print('FAIL %s（%s）' % (path, exc), file=sys.stderr)
                continue
            removed += 1
            freed += item['size']
            print('removed %-10s %s' % (human(item['size']), path))
    print('\n完成：%d 项 / %s' % (removed, human(freed)))
    return 0 if removed else 4


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit', action='store_true', default=True)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-destroy', default='')
    # 分组选择：默认清理全部分组；给了 --only 就只处理列出的分组。
    # 存在的理由：`releases` 组会删掉历史回退点，必须由人单独确认后执行，
    # 不能和「旧日志/旧数据库快照」这种低风险回收混在一次 --apply 里。
    parser.add_argument('--only', default='')
    parser.add_argument('--exclude', default='')
    args = parser.parse_args(argv)

    report = audit()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    if not args.apply:
        return 0
    return apply_prune(report, args.confirm_destroy,
                       only=args.only, exclude=args.exclude)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
