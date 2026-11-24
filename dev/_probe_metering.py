#!/usr/bin/env python3
"""在生产库上真跑一次 metering.reconcile，量成本并检查严格分类（只读）。

为什么要跑：接线测试是 mock 的，证明不了两件事 ——
1. 在 707 MB 的库上，15 分钟一次的扫描不会变成慢查询；
2. "必须为零"的分类在当前数据上确实是零（否则接上线就是每 15 分钟一条假警告）。

只读打开 SQLite（mode=ro），不经过服务器的写连接，不改任何数据。
"""
import os
import sqlite3
import sys
import time

os.environ.setdefault('WB_DATA_DIR', '/opt/workbuddy-manager/data')
sys.path.insert(0, '/opt/workbuddy-manager/releases/current')

from server import metering  # noqa: E402

#: 与 server.operational.METERING_STRICT_CATEGORIES 保持一致。
STRICT = {'missing_usage_event', 'token_mismatch', 'budget_settled_without_event',
          'terminal_mismatch', 'zero_token_completed'}

DB = os.path.join(os.environ['WB_DATA_DIR'], 'manager.db')
print('db =', DB, f'{os.path.getsize(DB) / 1024 / 1024:.0f} MB')

for limit in (500, 2000):
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    started = time.perf_counter()
    report = metering.reconcile(conn, limit=limit)
    elapsed = (time.perf_counter() - started) * 1000
    conn.close()
    findings = {name: item['count'] for name, item in report['findings'].items() if item['count']}
    strict = {k: v for k, v in findings.items() if k in STRICT}
    print(f'limit={limit:5d}  scanned={report["scanned_logs"]:5d}  {elapsed:7.1f} ms')
    print(f'           strict={strict or "{} (必须为零的分类全为 0 → 不会报警)"}')
    print(f'           all   ={findings}')
