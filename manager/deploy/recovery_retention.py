#!/opt/workbuddy-manager/venv/bin/python
"""整机恢复包的快照保留策略（2026-10-28）。

## 为什么需要它

`/opt/zhihui-candidates/r14-backup/recovery_v2.py` 只负责生成与解密校验，**没有任何
保留逻辑**：每跑一次就往 `/root/backups/zhihui-v2/snapshots/` 多放一个目录
（约 128MB），`runtime/` 是内容寻址的所以不重复占空间。截至 2026-10-28 15:00
已经 1.5G / 2 份快照，磁盘 31G/59G。按每天跑一次的节奏，约 200 天后会把盘写满，
而写满盘会同时打死数据库、回执和发布。

## 排序规则

按**目录名里的时间戳**排序（`YYYYMMDD-HHMMSS`，字典序即时间序），
**不用 mtime** —— U20 那次就是被批量操作改写的 mtime 坑了两次，这里不重犯。

## 保留规则

保留满足**任一条**的快照：

  * 最近 ``--keep`` 份（默认 3）
  * ``latest.json`` 引用的那一份（**永不删**，它是当前可恢复点）
  * 目录名时间戳在 ``--days`` 天内的（默认 7）

其余进清理候选。

## 用法

    zh-recovery-retention                # 默认只读审计
    zh-recovery-retention --keep 5
    zh-recovery-retention --apply --confirm-destroy <token>

退出码：0 成功；2 参数错误 / 令牌不匹配；3 拒绝执行（安全闸门）；4 无候选。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path('/root/backups/zhihui-v2')
SNAPSHOTS = ROOT / 'snapshots'
LATEST = ROOT / 'latest.json'
STAMP_FORMAT = '%Y%m%d-%H%M%S'


def human(size: int) -> str:
    value = float(size)
    for unit in ('B', 'K', 'M', 'G'):
        if value < 1024 or unit == 'G':
            return '%.1f%s' % (value, unit)
        value /= 1024
    return '%.1fG' % value


def dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob('*'):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def latest_snapshot_name() -> str | None:
    """latest.json 引用的快照目录名（可能是绝对路径、相对路径或名字）。"""
    try:
        data = json.loads(LATEST.read_text())
    except (OSError, ValueError):
        return None
    for key in ('snapshot', 'snapshot_dir', 'snapshot_path', 'path'):
        value = data.get(key)
        if isinstance(value, str) and value:
            return Path(value.rstrip('/')).name
        # latest.json 里 `snapshot` 是**对象**（含 directory / cipher_sha256 / parts），
        # 早期只判字符串会让「永不删当前恢复点」这条闸门静默失效（2026-10-28 实际发生）。
        if isinstance(value, dict):
            for inner in ('directory', 'path', 'name'):
                nested = value.get(inner)
                if isinstance(nested, str) and nested:
                    return Path(nested.rstrip('/')).name
    return None


def parse_stamp(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, STAMP_FORMAT)
    except ValueError:
        return None


def snapshot_dirs() -> list:
    if not SNAPSHOTS.is_dir():
        return []
    return sorted((p for p in SNAPSHOTS.iterdir() if p.is_dir()), key=lambda p: p.name)


def token_for(paths: list) -> str:
    material = '\n'.join(str(p) for p in paths).encode()
    return hashlib.sha256(material).hexdigest()[:16]


def build_plan(keep: int, days: int) -> dict:
    dirs = snapshot_dirs()
    referenced = latest_snapshot_name()
    cutoff = datetime.now() - timedelta(days=days)
    # 「最近 N 份」只在**能解析时间戳**的快照里算。名字解析不出来的目录（人工复制、
    # 改名）字典序通常排在数字之后；如果让它参与末尾排序，它会占掉一个保留名额，
    # 反而把真正的近期快照挤出窗口 —— 那是拿垃圾换完整快照。
    parseable = [p for p in dirs if parse_stamp(p.name) is not None]
    newest = {p.name for p in parseable[-keep:]} if keep > 0 else set()

    items = []
    keep_paths = []
    for path in dirs:
        stamp = parse_stamp(path.name)
        size = dir_size(path)
        if referenced and path.name == referenced:
            reason = 'latest.json 引用的当前可恢复点，永不删除'
        elif path.name in newest:
            reason = '最近 %d 份之一' % keep
        elif stamp is not None and stamp >= cutoff:
            reason = '目录名时间戳在 %d 天内（%s）' % (days, stamp.date())
        elif stamp is None:
            reason = '目录名不是 %s 格式，无法判断新旧 → 保守保留' % STAMP_FORMAT
        else:
            reason = None
        if reason:
            items.append({'path': str(path), 'size': size, 'action': 'keep', 'why': reason})
            keep_paths.append(path)
        else:
            items.append({'path': str(path), 'size': size, 'action': 'prune',
                          'why': '旧快照（保留最近 %d 份 + %d 天内 + latest 引用）' % (keep, days)})

    prune = [Path(i['path']) for i in items if i['action'] == 'prune']
    plan = {'root': str(ROOT), 'latest_snapshot': referenced,
            'keep_count': keep, 'keep_days': days,
            'items': items,
            'prune_count': len(prune),
            'prune_bytes': sum(i['size'] for i in items if i['action'] == 'prune'),
            'kept_count': len(keep_paths),
            'total_count': len(items),
            'confirm_token': token_for(prune)}
    return plan


# ── 明文中间产物清理 ────────────────────────────────────────────────────
# recovery_v2.py 先在快照目录里生成 `snapshot.tar.gz` 明文包（数据库副本、
# 凭据、TLS 私钥都在里面），再加密成分片，**加密成功后没有删掉明文**：
# 2026-10-28 实测两份快照各留一个 472MB / 133MB 的明文 tar，合计 606MB。
# 目录虽然是自己 0700 root，但「备份根目录里躺着未加密的全量明文」本身
# 就是缺陷：任何一次 /root/backups 的复制、打包、误传都会把它带出去。
#
# 删除前提（硬闸门）：必须按 `snapshot.json` 记录的 `cipher_sha256` **复算密文**，
# 校验通过才允许删明文；校验不过、缺分片、缺 snapshot.json 一律保留并报错。


def snapshot_meta(path: Path) -> dict | None:
    try:
        return json.loads((path / 'snapshot.json').read_text())
    except (OSError, ValueError):
        return None


def cipher_matches(path: Path, meta: dict) -> tuple:
    """复算密文分片的 SHA-256，与 snapshot.json 记录比对。"""
    expected = meta.get('cipher_sha256')
    parts = meta.get('parts') or []
    if not expected or not parts:
        return False, 'snapshot.json 缺 cipher_sha256 或 parts'
    digest = hashlib.sha256()
    for part in parts:
        name = part.get('name') if isinstance(part, dict) else None
        if not name:
            return False, 'parts 条目缺 name'
        target = path / name
        if not target.is_file():
            return False, '缺少分片 %s' % name
        with target.open('rb') as handle:
            for block in iter(lambda: handle.read(1 << 20), b''):
                digest.update(block)
    actual = digest.hexdigest()
    if actual != expected:
        return False, '密文校验不符（记录 %s… 实际 %s…）' % (expected[:12], actual[:12])
    return True, '密文校验通过 %s…' % expected[:12]


#: 只清这两类**已知**明文包。绝不能写成「除 .age 和 snapshot.json 之外都算明文」——
#: 2026-10-28 就是这么写的，结果 `--purge-plaintext --auto` 把 `recovery-manifest.json`
#: （sidecar，`--publish-prepared` 依赖它）和 `metadata.sig` 一起删了。
#: 白名单之外的文件一律不碰，宁可留垃圾也不误删元数据。
PLAINTEXT_NAMES = ('snapshot.tar', 'snapshot.tar.gz', 'runtime.tar', 'runtime.tar.gz')

#: 任何情况下都不删的文件（纵深防御，即使将来白名单写错也不会误删）
PROTECTED_NAMES = ('snapshot.json', 'recovery-manifest.json', 'metadata.sig', 'metadata.json')


def plaintext_files(path: Path) -> list:
    """快照目录里**确实是明文中间包**的文件（白名单，不是「其他都算」）。"""
    found = []
    for item in path.iterdir():
        if not item.is_file():
            continue
        if item.name in PROTECTED_NAMES or item.name.endswith('.age'):
            continue
        if item.name in PLAINTEXT_NAMES:
            found.append(item)
    return sorted(found)


def build_plaintext_plan() -> dict:
    items = []
    for path in snapshot_dirs():
        leftovers = plaintext_files(path)
        if not leftovers:
            continue
        meta = snapshot_meta(path)
        if meta is None:
            items.append({'path': str(leftovers[0]), 'size': sum(p.stat().st_size for p in leftovers),
                          'action': 'keep', 'why': '缺 snapshot.json，无法校验密文 → 保留',
                          'files': [str(p) for p in leftovers]})
            continue
        ok, detail = cipher_matches(path, meta)
        action = 'prune' if ok else 'keep'
        why = detail if ok else '密文校验未通过：%s → 保留明文' % detail
        items.append({'path': str(leftovers[0]), 'size': sum(p.stat().st_size for p in leftovers),
                      'action': action, 'why': why,
                      'files': [str(p) for p in leftovers]})
    prune = [Path(f) for i in items if i['action'] == 'prune' for f in i['files']]
    return {'items': items, 'prune_count': sum(len(i['files']) for i in items if i['action'] == 'prune'),
            'prune_bytes': sum(i['size'] for i in items if i['action'] == 'prune'),
            'confirm_token': token_for(prune)}


def print_plaintext_plan(plan: dict) -> None:
    print('明文中间产物审计（快照目录 %s）' % SNAPSHOTS)
    print()
    if not plan['items']:
        print('  没有明文中间产物。')
    for item in plan['items']:
        mark = 'PRUNE' if item['action'] == 'prune' else 'keep '
        print('  %s %-10s %s' % (mark, human(item['size']), Path(item['path']).parent.name))
        for name in item['files']:
            print('         - %s' % Path(name).name)
        print('         %s' % item['why'])
    print('\n可清理：%d 个文件 / %s' % (plan['prune_count'], human(plan['prune_bytes'])))
    print('确认令牌：%s' % plan['confirm_token'])
    print('（默认只读。真正删除需 --purge-plaintext --apply --confirm-destroy %s）'
          % plan['confirm_token'])


def apply_plaintext_plan(plan: dict, token: str) -> int:
    if plan['prune_count'] == 0:
        print('没有可清理的明文中间产物。', file=sys.stderr)
        return 4
    if token != plan['confirm_token']:
        print('令牌不匹配：拒绝执行。当前令牌 %s' % plan['confirm_token'], file=sys.stderr)
        return 2
    removed = 0
    freed = 0
    for item in plan['items']:
        if item['action'] != 'prune':
            continue
        path = Path(item['path'])
        directory = path.parent
        if directory.parent != SNAPSHOTS:
            print('SKIP %s（不在 %s 下）' % (path, SNAPSHOTS), file=sys.stderr)
            continue
        # 删除前再算一次密文，防止审计之后分片被改动
        meta = snapshot_meta(directory)
        if meta is None:
            print('SKIP %s（snapshot.json 消失）' % directory, file=sys.stderr)
            continue
        ok, detail = cipher_matches(directory, meta)
        if not ok:
            print('SKIP %s（%s）' % (directory, detail), file=sys.stderr)
            continue
        for name in item['files']:
            target = Path(name)
            if not target.is_file() or target.parent != directory:
                continue
            if target.name in PROTECTED_NAMES or target.name.endswith('.age'):
                print('SKIP %s（受保护文件）' % target, file=sys.stderr)
                continue
            if target.name not in PLAINTEXT_NAMES:
                print('SKIP %s（不在明文白名单）' % target, file=sys.stderr)
                continue
            try:
                size = target.stat().st_size
                target.unlink()
            except OSError as exc:
                print('FAIL %s（%s）' % (target, exc), file=sys.stderr)
                continue
            removed += 1
            freed += size
            print('removed %-10s %s/%s' % (human(size), directory.name, target.name))
    print('\n完成：%d 个文件 / %s' % (removed, human(freed)))
    return 0 if removed else 4


def print_plan(plan: dict) -> None:
    print('root            : %s' % plan['root'])
    print('latest.json     : %s' % (plan['latest_snapshot'] or '(未引用)'))
    print('快照总数        : %d（保留 %d）' % (plan['total_count'], plan['kept_count']))
    print()
    for item in plan['items']:
        mark = 'PRUNE' if item['action'] == 'prune' else 'keep '
        print('  %s %-10s %s' % (mark, human(item['size']), Path(item['path']).name))
        print('         %s' % item['why'])
    runtime = ROOT / 'runtime'
    if runtime.is_dir():
        blobs = [p for p in runtime.iterdir() if p.is_dir()]
        print('\n  runtime/ 内容寻址 %d 个 blob / %s（本工具不动，多个快照共用）'
              % (len(blobs), human(dir_size(runtime))))
    print('\n可清理：%d 份 / %s' % (plan['prune_count'], human(plan['prune_bytes'])))
    print('确认令牌：%s' % plan['confirm_token'])
    print('（默认只读。真正删除需 --apply --confirm-destroy %s）' % plan['confirm_token'])


def apply_plan(plan: dict, token: str) -> int:
    if plan['prune_count'] == 0:
        print('没有可清理的快照。', file=sys.stderr)
        return 4
    if token != plan['confirm_token']:
        print('令牌不匹配：拒绝执行。当前令牌 %s' % plan['confirm_token'], file=sys.stderr)
        return 2

    # 安全闸门：重新算一遍，防止两次之间目录变化
    fresh = build_plan(plan['keep_count'], plan['keep_days'])
    if fresh['confirm_token'] != plan['confirm_token']:
        print('目录内容已变化（令牌 %s → %s），拒绝执行；请重新审计。'
              % (plan['confirm_token'], fresh['confirm_token']), file=sys.stderr)
        return 3

    referenced = fresh['latest_snapshot']
    removed = 0
    freed = 0
    for item in fresh['items']:
        if item['action'] != 'prune':
            continue
        path = Path(item['path'])
        if referenced and path.name == referenced:
            print('SKIP %s（latest.json 引用）' % path, file=sys.stderr)
            continue
        if not path.is_dir() or path.parent != SNAPSHOTS:
            print('SKIP %s（不在 %s 下）' % (path, SNAPSHOTS), file=sys.stderr)
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            print('FAIL %s（%s）' % (path, exc), file=sys.stderr)
            continue
        removed += 1
        freed += item['size']
        print('removed %-10s %s' % (human(item['size']), path.name))
    print('\n完成：%d 份 / %s' % (removed, human(freed)))
    return 0 if removed else 4


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description='整机恢复包快照保留策略与明文清理')
    parser.add_argument('--keep', type=int, default=3, help='保留最近 N 份快照（默认 3）')
    parser.add_argument('--days', type=int, default=7, help='保留 N 天内创建的快照（默认 7）')
    parser.add_argument('--purge-plaintext', action='store_true',
                        help='改为清理快照目录里的明文中间产物（先校验密文，再删明文）')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-destroy', default='')
    parser.add_argument('--auto', action='store_true',
                        help='无人值守模式：跳过令牌，但仍执行全部安全闸门'
                             '（latest.json 引用永不删、名字排序、删除前复算密文、路径越界拒绝）')
    args = parser.parse_args(argv)

    if args.auto and not (args.apply or args.purge_plaintext):
        print('--auto 需要配合 --apply 或 --purge-plaintext 使用', file=sys.stderr)
        return 2

    if args.purge_plaintext:
        plain = build_plaintext_plan()
        if args.json:
            print(json.dumps(plain, ensure_ascii=False, indent=2))
        else:
            print_plaintext_plan(plain)
        if not (args.apply or args.auto):
            return 0
        token = args.confirm_destroy or plain['confirm_token']
        return apply_plaintext_plan(plain, token)

    if args.keep < 1:
        print('--keep 必须 >= 1（latest.json 引用无论如何都保留）', file=sys.stderr)
        return 2
    if args.days < 0:
        print('--days 不能为负', file=sys.stderr)
        return 2

    plan = build_plan(args.keep, args.days)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print_plan(plan)
    if not (args.apply or args.auto):
        return 0
    token = args.confirm_destroy or plan['confirm_token']
    return apply_plan(plan, token)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
