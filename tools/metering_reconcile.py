"""K27 · 计量归因对账 CLI（只读，本地副本）。

把 `server.metering.reconcile()` 与 `server.budget.summary()` 的结果打印成可复核的
报告：每个 `request_id` 的「日志 / usage_events / 预算预留 / forwarding_events /
请求终态」是否闭合，以及成功 / 取消 / 未知花费各自多少。

用法::

    python -X utf8 tools\\metering_reconcile.py --db <local.db>
    python -X utf8 tools\\metering_reconcile.py --db <local.db> --limit 5000 --json out.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'manager'))

from server import metering  # noqa: E402

#: 拒绝直接操作生产路径（只允许本地副本）
PRODUCTION_PREFIXES = ('/opt/workbuddy-manager', '/opt/qtg', '/var/lib/workbuddy')


def _guard(path: Path) -> None:
    text = str(path.resolve()).replace('\\', '/')
    for prefix in PRODUCTION_PREFIXES:
        if text.startswith(prefix):
            raise SystemExit(f'拒绝直接操作生产路径：{path}（本工具只跑本地副本）')


def budget_summary(conn: sqlite3.Connection) -> dict:
    """预算预留按状态汇总：成功 / 未决 / 未知分开，未知不释放。"""
    rows = conn.execute(
        'SELECT state, COUNT(*), COALESCE(SUM(charged_tokens),0), '
        'COALESCE(SUM(charged_credit),0) FROM budget_reservations GROUP BY state'
    ).fetchall() if _has(conn, 'budget_reservations') else []
    out = {'by_state': {}, 'unresolved_total': 0}
    for state, count, tokens, credit in rows:
        out['by_state'][str(state)] = {'count': int(count), 'tokens': tokens or 0,
                                       'credit': credit or 0}
        if str(state) in ('reserved', 'forwarded', 'unknown'):
            out['unresolved_total'] += int(count)
    return out


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def run(db_path: Path, *, limit: int, since_ts: int | None) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        report = metering.reconcile(conn, since_ts=since_ts, limit=limit)
        report['budget'] = budget_summary(conn)
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='K27 计量归因对账（只读，本地副本）')
    ap.add_argument('--db', required=True)
    ap.add_argument('--limit', type=int, default=2000, help='扫描最近 N 条请求日志')
    ap.add_argument('--since', type=int, default=None, help='只扫描 ts >= 该 epoch 的日志')
    ap.add_argument('--json', default='')
    args = ap.parse_args(argv)
    path = Path(args.db)
    _guard(path)
    if not path.is_file():
        print(f'数据库不存在：{path}', file=sys.stderr)
        return 2
    report = run(path, limit=args.limit, since_ts=args.since)
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    # 退出码：有需要人工处理的差异时返回 1（便于 CI/巡检直接判断）
    actionable = sum(
        bucket['count'] for name, bucket in report['findings'].items()
        if name in ('missing_usage_event', 'token_mismatch', 'platform_mismatch',
                    'budget_unknown', 'termial_mismatch', 'terminal_mismatch',
                    'budget_settled_without_event'))
    return 1 if actionable else 0


if __name__ == '__main__':
    raise SystemExit(main())
