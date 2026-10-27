#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""C-001：计费口径对账与修复（api_keys.used_credit 与明细不一致）。

## 口径定义（本次确立）
  * 明细层   `request_logs`          每笔请求一行，**全量权威**
  * 账本层   `usage_events`          逐请求事件账本，只覆盖账本上线后（可审计/可回滚）
  * 展示层   `usage_daily`           明细的按日聚合（实测与明细差 3.77 credits，正常）
  * 执法层   `api_keys.used_credit*` 配额判断依据，语义应为「该密钥累计已用（含历史）」

## 问题
  执法层与「明细聚合」不一致：key1 少 6,725.00、key2 少 429.24、key4 少 77.92，
  合计少 7,232.16；另有 2,930.25 属于已删除密钥（`api_keys` 无对应行，不处理）。

## 用法
  --dry-run   只报告差异（默认）
  --apply     把 used_credit_units / used_credit 重算为明细聚合（先自动备份）
"""
import argparse
import json
import os
import sqlite3
import time

DB = '/opt/workbuddy-manager/data/manager.db'
SCALE = 1_000_000


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DB)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--backup-dir', default='/opt/backups')
    a = ap.parse_args()

    conn = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row

    print('=== 口径对账（credit）===')
    print('%-4s %-14s %-14s %-14s %-8s' % ('key', '当前(执法)', '应有(明细)', '差额', '状态'))
    rows = q(conn, '''
        SELECT k.id, k.name, k.used_credit_units,
               COALESCE((SELECT SUM(r.credit) FROM request_logs r
                         WHERE r.key_id = k.id AND r.credit IS NOT NULL), 0) AS detail_credit
        FROM api_keys k ORDER BY k.id''')
    plan = []
    total_gap = 0.0
    for r in rows:
        cur = (r['used_credit_units'] or 0) / SCALE
        want = float(r['detail_credit'] or 0)
        gap = want - cur
        total_gap += gap
        status = 'OK' if abs(gap) < 0.005 else 'NEEDS FIX'
        print('%-4s %-14.4f %-14.4f %-+14.4f %-8s' % (r['id'], cur, want, gap, status))
        if status != 'OK':
            plan.append((r['id'], r['name'], cur, want, gap))
    print('%-4s %-14s %-14.4f %-+14.4f' % ('', '合计差额', total_gap, total_gap))

    # 已删除密钥占用的量（无 api_keys 行，仅报告）
    orphan = q(conn, '''
        SELECT COALESCE(SUM(credit),0) AS c, COUNT(*) AS n FROM request_logs
        WHERE credit IS NOT NULL AND (key_id IS NULL OR key_id NOT IN (SELECT id FROM api_keys))''')[0]
    print('\n已删除/无主密钥占用的明细 credit: %.4f（%d 笔，不属于任何存活密钥，不处理）'
          % (orphan['c'], orphan['n']))

    # 三面汇总
    print('\n=== 三面汇总 ===')
    for label, sql in (
        ('明细 request_logs ', 'SELECT COALESCE(SUM(credit),0) FROM request_logs WHERE credit IS NOT NULL'),
        ('账本 usage_events ', 'SELECT COALESCE(SUM(credit_units),0)/1000000.0 FROM usage_events'),
        ('展示 usage_daily  ', 'SELECT COALESCE(SUM(COALESCE(credit_units, CAST(credit*1000000 AS INTEGER))),0)/1000000.0 FROM usage_daily'),
        ('执法 api_keys     ', 'SELECT COALESCE(SUM(used_credit_units),0)/1000000.0 FROM api_keys'),
    ):
        v = q(conn, sql)[0][0]
        print('  %s : %.4f' % (label, v))
    conn.close()

    if not plan:
        print('\n无需修复：执法层已与明细一致')
        return 0

    print('\n=== 待修复 %d 个密钥 ===' % len(plan))
    for kid, name, cur, want, gap in plan:
        print('  key%-3d %-12s %.4f -> %.4f  (%+.4f)' % (kid, name, cur, want, gap))

    if not a.apply:
        print('\n（dry-run，未写入。加 --apply 执行）')
        return 0

    # 备份 + 写入
    ts = time.strftime('%Y%m%d-%H%M%S')
    bak = os.path.join(a.backup_dir, 'api_keys-before-c001-%s.json' % ts)
    os.makedirs(a.backup_dir, exist_ok=True)
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    snap = [dict(r) for r in conn.execute('SELECT * FROM api_keys ORDER BY id')]
    with open(bak, 'w', encoding='utf-8') as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1)
    os.chmod(bak, 0o600)
    print('\n已备份 api_keys -> %s（%d 行）' % (bak, len(snap)))

    applied = 0
    for kid, name, cur, want, gap in plan:
        units = int(round(want * SCALE))
        conn.execute('UPDATE api_keys SET used_credit_units=?, used_credit=? WHERE id=?',
                     (units, want, kid))
        applied += 1
    conn.commit()

    # 复核
    rows = conn.execute('''
        SELECT k.id, k.used_credit_units,
               COALESCE((SELECT SUM(r.credit) FROM request_logs r
                         WHERE r.key_id = k.id AND r.credit IS NOT NULL), 0) AS d
        FROM api_keys k ORDER BY k.id''').fetchall()
    bad = [r['id'] for r in rows if abs((r['used_credit_units'] or 0) / SCALE - float(r['d'] or 0)) >= 0.005]
    conn.close()
    print('已更新 %d 行；复核不一致的 key: %s' % (applied, bad or '无'))
    return 0 if not bad else 1


if __name__ == '__main__':
    raise SystemExit(main())
