"""K24 · 账本保留 / 归档 CLI（薄壳；逻辑在 `manager/server/archival.py`）。

为什么逻辑不在这里：测试视图只复制 `manager/`，`tools/` 不在视图里，放在 tools
就无法被测试覆盖。这里只做参数解析、生产路径守卫和输出。

典型用法（本地临时库/副本）::

    # 1) 只看分类与可归档量（不写文件、不改库）
    python -X utf8 tools\\archive_ledgers.py --db <local.db> --dry-run

    # 2) 归档保留期满的行到 ./archive-out
    python -X utf8 tools\\archive_ledgers.py --db <local.db> --dataset request_logs \\
        --out ./archive-out

    # 3) 取回检验某个归档文件（不碰源库）
    python -X utf8 tools\\archive_ledgers.py --db <local.db> --verify-only <file.jsonl>

    # 4) 校验通过后清理（必须同时给 --confirm-sha，且等于 manifest 里的 sha256）
    python -X utf8 tools\\archive_ledgers.py --db <local.db> --dataset request_logs \\
        --out ./archive-out --delete-after-verify --confirm-sha <sha256>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'manager'))

from server import archival, retention  # noqa: E402

#: 生产路径前缀：脚本拒绝直接操作（只能对本地副本跑）
PRODUCTION_PREFIXES = ('/opt/workbuddy-manager', '/opt/qtg', '/var/lib/workbuddy')


def _guard_path(path: Path) -> None:
    text = str(path.resolve()).replace('\\', '/')
    for prefix in PRODUCTION_PREFIXES:
        if text.startswith(prefix.rstrip('/')):
            raise SystemExit(
                f'拒绝直接操作生产路径：{path}\n'
                '本脚本只允许对本地副本运行（审计约束：不连生产）。')


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='K24 账本保留/归档（仅本地副本）')
    ap.add_argument('--db', required=True, help='本地 SQLite 副本路径（拒绝生产路径）')
    ap.add_argument('--dataset', action='append', default=None,
                    help='数据集名，可重复；缺省 = 全部可归档数据集')
    ap.add_argument('--out', default='./archive-out', help='归档输出目录')
    ap.add_argument('--before', default='', help='截止时间（YYYY-MM-DD 或 epoch 秒）')
    ap.add_argument('--batch', type=int, default=1000)
    ap.add_argument('--dry-run', action='store_true', help='只统计，不写文件/不改库')
    ap.add_argument('--verify-only', default='', help='只校验一个 .jsonl 归档文件')
    ap.add_argument('--delete-after-verify', action='store_true')
    ap.add_argument('--confirm-sha', default='')
    ap.add_argument('--json', default='', help='把结果 JSON 写到该路径')
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    _guard_path(db_path)
    if not db_path.is_file():
        print(f'数据库不存在：{db_path}', file=sys.stderr)
        return 2
    if args.verify_only:
        result = archival.verify_archive(Path(args.verify_only))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 1

    now = int(time.time())
    if args.before:
        before_ts = (int(args.before) if args.before.isdigit()
                     else int(time.mktime(time.strptime(args.before, '%Y-%m-%d'))))
    else:
        before_ts = now

    names = args.dataset or [d.name for d in retention.DATASETS
                             if d.action in (retention.ACTION_ARCHIVE_THEN_PRUNE,
                                             retention.ACTION_PRUNE,
                                             retention.ACTION_NO_DELETE,
                                             retention.ACTION_NEVER)]
    conn = connect(db_path)
    try:
        if args.dry_run:
            result = archival.plan(conn, names, now)
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            print(text)
            if args.json:
                Path(args.json).write_text(text, encoding='utf-8')
            return 0
        results = []
        for name in names:
            entry = archival.archive_dataset(conn, name, out_dir=Path(args.out),
                                             before_ts=before_ts, batch=args.batch)
            if args.delete_after_verify and entry.get('state') == 'verified':
                entry['delete'] = archival.delete_archived(
                    conn, name, watermark=int(entry['watermark']),
                    sha=str(entry['sha256']), confirm_sha=args.confirm_sha)
            results.append(entry)
        text = json.dumps(results, ensure_ascii=False, indent=2, default=str)
        print(text)
        if args.json:
            Path(args.json).write_text(text, encoding='utf-8')
        failed = [r for r in results if r.get('state') not in (None, 'verified')
                  and not r.get('skipped')]
        return 1 if failed else 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
