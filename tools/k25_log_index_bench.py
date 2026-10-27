"""K25 · request_logs 真实查询形状的索引前后对比 + 写入代价测量（本地）。

用途（对应审计包 K25）：
  1. 在**本地临时库**上用 manager 的真实 schema（`server.db.SCHEMA`）造合成数据；
  2. 用 `router/logs.py`、`routers/stats.py` 里**逐字复制**的 SQL 形状跑
     `EXPLAIN QUERY PLAN`，给出加索引前 / 后的计划对比；
  3. 测量代表性查询的 P50 耗时；
  4. 测量写入代价（同一批 INSERT 在有/无索引时的耗时）与建索引窗口。

约束：
  * 只写系统临时目录，**不连生产、不读 manager/data、不碰任何真实库**；
  * 不修改业务源码；候选索引在这里只是实验参数，最终 DDL 落在
    `server/migrations.py` 的 v2 步骤里；
  * 输出 markdown 表格，便于直接贴进 `design/manager/K25-*.md` 作为证据。

用法::

    python -X utf8 tools\\k25_log_index_bench.py
    python -X utf8 tools\\k25_log_index_bench.py --rows 200000 --repeat 7 --json out.json
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# manager/ 是 `server` 包的父目录（测试里用 parents[2]，这里等价）
sys.path.insert(0, str(REPO / 'manager'))

# 候选索引：键 = 变体名，值 = DDL 列表。
# 说明见 design/manager/K25-日志查询与索引.md「候选」一节。
#
# 结论先行（实测见输出）：只有「等值过滤列 + id」这种**能把 ORDER BY id DESC
# 吃进索引顺序**的组合才对列表页有益。把只覆盖 WHERE 的索引加在列表查询上，
# 会让 SQLite 先用索引捞出行集再 USE TEMP B-TREE FOR ORDER BY —— 反而比原来的
# 「反向 rowid 扫描 + LIMIT 提前退出」慢几个数量级（实测 0.23ms → 700ms）。
VARIANTS: dict[str, list[tuple[str, str]]] = {
    'base': [],
    # safe：当前查询文本下**无回归**的两个索引（v2 实际采用的集合）
    'safe': [
        ('idx_logs_key_id_id', 'CREATE INDEX IF NOT EXISTS idx_logs_key_id_id '
                               'ON request_logs(key_id, id)'),
        # 账号回填（db.attach_request_accounts）只查 account IS NULL 的行
        ('idx_logs_unfilled_ts', 'CREATE INDEX IF NOT EXISTS idx_logs_unfilled_ts '
                                 'ON request_logs(ts) WHERE account IS NULL'),
    ],
    # rewrite：需要同时改写 routers/logs.py 的 WHERE 文本（不在本任务写入边界内，
    # 只作为候选证据）。索引是表达式索引，只有改写后的谓词能命中。
    'rewrite': [
        ('idx_logs_key_id_id', 'CREATE INDEX IF NOT EXISTS idx_logs_key_id_id '
                               'ON request_logs(key_id, id)'),
        ('idx_logs_unfilled_ts', 'CREATE INDEX IF NOT EXISTS idx_logs_unfilled_ts '
                                 'ON request_logs(ts) WHERE account IS NULL'),
        ('idx_logs_realm_cn_id', 'CREATE INDEX IF NOT EXISTS idx_logs_realm_cn_id '
                                 "ON request_logs(COALESCE(realm,'cn'), id)"),
        ('idx_logs_status_class_id', 'CREATE INDEX IF NOT EXISTS idx_logs_status_class_id '
                                     'ON request_logs(status/100, id)'),
    ],
    # harmful：「看起来合理」但实测会让列表页退化的索引 —— 保留在脚本里是为了
    # 让「为什么没加它们」这个结论可复现（不是漏测）。
    'harmful': [
        ('idx_logs_key_id_id', 'CREATE INDEX IF NOT EXISTS idx_logs_key_id_id '
                               'ON request_logs(key_id, id)'),
        ('idx_logs_realm_id', 'CREATE INDEX IF NOT EXISTS idx_logs_realm_id '
                              'ON request_logs(realm, id)'),
        ('idx_logs_status_ts', 'CREATE INDEX IF NOT EXISTS idx_logs_status_ts '
                               'ON request_logs(status, ts)'),
    ],
}

# 「改写后」的等价谓词（与 logs.py 当前文本语义逐行等价；只用于证明候选索引有效）
REWRITE_CLAUSES: dict[str, tuple[str, tuple]] = {
    'realm_cn_coalesce': ("WHERE COALESCE(l.realm,'cn') = ?", ('cn',)),
    'realm_global_coalesce': ("WHERE COALESCE(l.realm,'cn') = ?", ('global',)),
    'realm_days_coalesce': ("WHERE COALESCE(l.realm,'cn') = ? AND l.ts >= ?", ('cn', 0)),
    'status_ok_expr': ('WHERE l.status/100 = 2', ()),
    'status_days_expr': ('WHERE l.ts >= ? AND l.status/100 = 2', (0,)),
}

# ── 真实 SQL 形状（逐字来自 manager/server/routers/logs.py 与 stats.py） ──
# 只把参数换成占位符；WHERE 子句的拼装顺序与实现一致。
LOG_CLAUSES: dict[str, tuple[str, tuple]] = {
    'no_filter': ('', ()),
    'realm_cn': ('WHERE (l.realm = ? OR l.realm IS NULL)', ('cn',)),
    'realm_global': ('WHERE l.realm = ?', ('global',)),
    'days': ('WHERE l.ts >= ?', (0,)),
    'key_id': ('WHERE l.key_id = ?', (7,)),
    'model_like': ('WHERE (l.model LIKE ? OR l.mapped_model LIKE ?)', ('%deepseek%', '%deepseek%')),
    'ip_like': ('WHERE l.ip LIKE ?', ('%10.0.0.1%',)),
    'status_ok': ('WHERE l.status >= 200 AND l.status < 300', ()),
    'status_error': ('WHERE (l.status < 200 OR l.status >= 300)', ()),
    'realm_days': ('WHERE (l.realm = ? OR l.realm IS NULL) AND l.ts >= ?', ('cn', 0)),
    'key_days': ('WHERE l.ts >= ? AND l.key_id = ?', (0, 7)),
    'status_days': ('WHERE l.ts >= ? AND l.status >= 200 AND l.status < 300', (0,)),
}

COUNT_SQL = 'SELECT COUNT(*) AS c FROM request_logs l {clause}'
LIST_SQL = ('SELECT l.*, k.name AS key_name FROM request_logs l '
            'LEFT JOIN api_keys k ON k.id = l.key_id {clause} '
            'ORDER BY l.id DESC LIMIT ? OFFSET ?')

STATS_SQL: dict[str, str] = {
    # stats.py::throughput
    'throughput': (
        'SELECT model AS name, COUNT(*) AS samples, SUM(completion_tokens) AS ct, '
        'SUM(CASE WHEN latency_ms > COALESCE(first_token_ms, 0) '
        '         THEN latency_ms - COALESCE(first_token_ms, 0) ELSE 0 END) AS gen_ms '
        'FROM request_logs WHERE ts >= ? AND status >= 200 AND status < 300 '
        'AND completion_tokens > 0 AND latency_ms > COALESCE(first_token_ms, 0) '
        'GROUP BY model'),
    # stats.py::_failure_counts
    'failure_counts': (
        'SELECT SUM(CASE WHEN status >= 400 AND status < 500 THEN 1 ELSE 0 END) AS c4, '
        'SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS c5 '
        'FROM request_logs WHERE ts >= ? AND key_id IS NOT NULL '
        "AND COALESCE(realm, 'cn') = ?"),
    # stats.py::_usage_health
    'usage_health': (
        'SELECT COUNT(*) AS c FROM request_logs '
        'WHERE ts >= ? AND ts < ? AND key_id IS NOT NULL '
        'AND (COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0) > 0 '
        '     OR COALESCE(credit,0) > 0) '
        "AND COALESCE(realm, ?) = ?"),
    # db.py::attach_request_accounts（每采到一条上游账号日志就查一次）
    'account_backfill': (
        'SELECT id FROM request_logs WHERE account IS NULL AND ts BETWEEN ? AND ? '
        'AND (mapped_model = ? OR model = ?) ORDER BY ABS(ts - ?) LIMIT 1'),
}

STATS_ARGS: dict[str, tuple] = {
    'throughput': (0,),
    'failure_counts': (0, 'cn'),
    'usage_health': (0, 10 ** 10, 'cn', 'cn'),
    'account_backfill': (0, 10 ** 10, 'deepseek-v3', 'deepseek-v3', 0),
}

# 深分页 OFFSET 采样点
DEEP_OFFSETS = (0, 2000, 50000)

MODELS = ['deepseek-v3', 'deepseek-r1', 'global:deepseek-v3', 'gpt-4o-mini',
          'qwen-max', 'global:qwen-max', 'glm-4', 'kimi-k2']


def _seed(conn: sqlite3.Connection, rows: int, seed: int = 20261023) -> None:
    random.seed(seed)
    now = int(time.time())
    batch: list[tuple] = []
    for i in range(1, rows + 1):
        model = random.choice(MODELS)
        global_ = model.startswith('global:')
        ts = now - random.randint(0, 180 * 86400)
        status = random.choices([200, 200, 200, 200, 429, 500, 400, 499],
                                weights=[70, 10, 8, 4, 3, 2, 2, 1])[0]
        pt = random.randint(50, 8000)
        ct = 0 if status >= 400 else random.randint(0, 4000)
        latency = random.randint(200, 60000)
        first = random.choice([None, random.randint(100, 3000)])
        batch.append((
            ts, random.choice([None, random.randint(1, 200)]),
            f'10.0.0.{random.randint(1, 200)}', model, model, status, pt, ct,
            latency, first, 'bench/1.0', None if status < 400 else 'bench error',
            1 if random.random() < 0.6 else 0,
            None if random.random() < 0.3 else round(random.random() * 0.4, 6),
            'global' if global_ else 'cn',
            None if random.random() < 0.4 else f'account-{random.randint(1, 40)}',
            random.randint(0, 900) if first else None,
            random.randint(0, 4000) if first else None, None,
            f'bench-{i:08d}', '/v1/chat/completions', 'POST', 'openai',
            200, 'completed', None, 'bench', 'upstream', 1,
            random.randint(10, 900) if first else None,
            'qoder' if random.random() < 0.2 else 'workbuddy',
        ))
        if len(batch) >= 2000:
            _insert(conn, batch)
            batch.clear()
    if batch:
        _insert(conn, batch)
    conn.commit()


INSERT_COLUMNS = (
    'ts,key_id,ip,model,mapped_model,status,prompt_tokens,completion_tokens,'
    'latency_ms,first_token_ms,ua,error,stream,credit,realm,account,'
    'cache_hit_tokens,cache_miss_tokens,cache_write_tokens,request_id,endpoint,'
    'method,protocol,http_status,terminal_state,error_stage,release_id,'
    'usage_source,usage_complete,first_byte_ms,platform'
)


def _insert(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    marks = ','.join('?' for _ in range(len(batch[0])))
    conn.executemany(f'INSERT INTO request_logs({INSERT_COLUMNS}) VALUES({marks})', batch)


def _new_db(tmp: Path, rows: int) -> sqlite3.Connection:
    """用 manager 的真实 SCHEMA 建库（只借 DDL，不碰业务数据）。"""
    from server import db as server_db  # noqa: WPS433 —— 只为拿到权威 DDL

    path = tmp / 'bench.db'
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.executescript(server_db.SCHEMA)
    # SCHEMA 只含建表；升级补列（request_logs.account / platform 等）由 _migrate 做，
    # 这里必须走同一条真实路径，否则测的不是生产形状。
    server_db._migrate(conn)
    conn.commit()
    conn.executemany('INSERT INTO api_keys(name,key_hash,prefix,created_at) VALUES(?,?,?,?)',
                     [(f'k{i}', f'h{i}', f'p{i}', 0) for i in range(1, 201)])
    _seed(conn, rows)
    return conn


def _plan(conn: sqlite3.Connection, sql: str, args: tuple) -> str:
    rows = conn.execute('EXPLAIN QUERY PLAN ' + sql, args).fetchall()
    return ' <- '.join(str(r['detail']) for r in rows)


def _timeit(conn: sqlite3.Connection, sql: str, args: tuple, repeat: int) -> float:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        conn.execute(sql, args).fetchall()
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples), 3)


def _write_cost(conn: sqlite3.Connection, rows: int) -> dict[str, float]:
    """写入代价：同一批 INSERT 在有 / 无候选索引时的耗时（毫秒/千行）。"""
    # 先清空实验表，保证两次写入规模一致；索引是否存在于当前库决定测量口径。
    conn.execute('DELETE FROM request_logs')
    conn.commit()
    start = time.perf_counter()
    batch: list[tuple] = []
    rnd = random.Random(7)
    for i in range(rows):
        batch.append((int(time.time()), rnd.randint(1, 200), '10.0.0.1', 'deepseek-v3',
                      'deepseek-v3', 200, 100, 50, 900, 120, 'ua', None, 1, 0.001,
                      'cn', None, None, None, None, f'wc-{i:08d}', '/v1/chat/completions',
                      'POST', 'openai', 200, 'completed', None, 'bench', 'upstream', 1,
                      120, 'workbuddy'))
        if len(batch) >= 500:
            _insert(conn, batch)
            batch.clear()
    if batch:
        _insert(conn, batch)
    conn.commit()
    elapsed = time.perf_counter() - start
    return {'rows': rows, 'ms': round(elapsed * 1000, 1),
            'ms_per_1k': round(elapsed * 1000 / rows * 1000, 2)}


def run(rows: int, repeat: int) -> dict:
    report: dict = {'rows': rows, 'repeat': repeat, 'variants': {}, 'index_build_ms': {},
                    'write_cost': {}}
    tmpdir = tempfile.mkdtemp(prefix='k25-bench-')
    conn = None

    def _reseed() -> None:
        conn.execute('DELETE FROM request_logs')
        conn.commit()
        _seed(conn, rows)

    try:
        tmp = Path(tmpdir)
        conn = _new_db(tmp, rows)

        # 写入代价基线（无候选索引）
        report['write_cost']['base'] = _write_cost(conn, min(rows, 50000))
        _reseed()

        for name, ddls in VARIANTS.items():
            # 建索引窗口：在已有数据上建（首次出现时才是真实成本，后续变体是超集）
            for idx_name, ddl in ddls:
                start = time.perf_counter()
                conn.execute(ddl)
                conn.commit()
                report['index_build_ms'].setdefault(
                    idx_name, round((time.perf_counter() - start) * 1000, 1))
            entry: dict = {'indexes': [d[0] for d in ddls], 'plans': {}, 'timings': {}}
            for label, (clause, args) in LOG_CLAUSES.items():
                sql_args = tuple(args) + (20, 0)
                entry['plans'][f'count:{label}'] = _plan(conn, COUNT_SQL.format(clause=clause), args)
                entry['plans'][f'list:{label}'] = _plan(conn, LIST_SQL.format(clause=clause), sql_args)
                if label in ('no_filter', 'realm_cn', 'key_id', 'days', 'model_like',
                             'realm_days', 'status_days'):
                    entry['timings'][f'count:{label}'] = _timeit(
                        conn, COUNT_SQL.format(clause=clause), args, repeat)
                    entry['timings'][f'list:{label}'] = _timeit(
                        conn, LIST_SQL.format(clause=clause), sql_args, repeat)
            for label, sql in STATS_SQL.items():
                entry['plans'][f'stats:{label}'] = _plan(conn, sql, STATS_ARGS[label])
                entry['timings'][f'stats:{label}'] = _timeit(conn, sql, STATS_ARGS[label], repeat)
            # 深分页：OFFSET 越大越暴露「反向扫描 + 过滤」的 O(offset) 代价
            for label in ('no_filter', 'key_id', 'realm_cn'):
                clause, args = LOG_CLAUSES[label]
                for offset in DEEP_OFFSETS:
                    entry['timings'][f'ann:{label}@{offset}'] = _timeit(
                        conn, LIST_SQL.format(clause=clause), tuple(args) + (20, offset), repeat)
            if name == 'rewrite':
                for label, (clause, args) in REWRITE_CLAUSES.items():
                    entry['plans'][f'count:{label}'] = _plan(conn, COUNT_SQL.format(clause=clause), args)
                    entry['plans'][f'list:{label}'] = _plan(
                        conn, LIST_SQL.format(clause=clause), tuple(args) + (20, 0))
                    entry['timings'][f'count:{label}'] = _timeit(
                        conn, COUNT_SQL.format(clause=clause), args, repeat)
                    entry['timings'][f'list:{label}'] = _timeit(
                        conn, LIST_SQL.format(clause=clause), tuple(args) + (20, 0), repeat)
                    entry['timings'][f'ann:{label}@{DEEP_OFFSETS[-1]}'] = _timeit(
                        conn, LIST_SQL.format(clause=clause),
                        tuple(args) + (20, DEEP_OFFSETS[-1]), repeat)
            report['variants'][name] = entry
            if name != 'base':
                # 该变体的索引集合已就位：测写入手部代价，然后恢复数据供下一轮
                report['write_cost'][name] = _write_cost(conn, min(rows, 50000))
                _reseed()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        # Windows 上 SQLite 句柄释放有延迟，清理失败不影响测量结果
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    return report


def _md(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# K25 测量结果（rows={report['rows']}，repeat={report['repeat']}）\n")
    variants = report['variants']
    labels = list(LOG_CLAUSES)
    lines.append('## 1. 日志列表：EXPLAIN QUERY PLAN（count / list，`<br>` 分隔）\n')
    lines.append('| 查询形状 | ' + ' | '.join(variants) + ' |')
    lines.append('|---|' + '---|' * len(variants))
    for label in labels:
        cells = []
        for name in variants:
            cells.append(f"`{variants[name]['plans'][f'count:{label}']}`<br>`{variants[name]['plans'][f'list:{label}']}`")
        lines.append(f'| {label} | ' + ' | '.join(cells) + ' |')
    if 'rewrite' in variants:
        lines.append('\n## 1b. 改写后的等价谓词（候选，未应用到源码）\n')
        lines.append('| 谓词 | 计划 count | 计划 list | count ms | list ms |')
        lines.append('|---|---|---|---|---|')
        for label in REWRITE_CLAUSES:
            v = variants['rewrite']
            lines.append(
                f"| {label} | `{v['plans'][f'count:{label}']}` | `{v['plans'][f'list:{label}']}` "
                f"| {v['timings'].get(f'count:{label}', '—')} "
                f"| {v['timings'].get(f'list:{label}', '—')} |")
    lines.append('\n## 2. 统计查询计划\n')
    lines.append('| 查询 | ' + ' | '.join(variants) + ' |')
    lines.append('|---|' + '---|' * len(variants))
    for label in STATS_SQL:
        lines.append(f'| {label} | ' + ' | '.join(
            f"`{variants[n]['plans'][f'stats:{label}']}`" for n in variants) + ' |')
    lines.append('\n## 3. 代表性查询 P50 耗时（ms）\n')
    rows_labels = [k for k in variants['base']['timings']]
    lines.append('| 查询 | ' + ' | '.join(variants) + ' |')
    lines.append('|---|' + '---|' * len(variants))
    for label in rows_labels:
        vals = []
        for n in variants:
            v = variants[n]['timings'].get(label)
            vals.append('—' if v is None else f'{v}')
        lines.append(f'| {label} | ' + ' | '.join(vals) + ' |')
    lines.append('\n## 4. 写入与建索引代价\n')
    lines.append('写入代价按「该变体索引集合全部存在时」测量（每千行毫秒，越小越好）：\n')
    lines.append('| 索引集合 | ms/千行 | 行数 |')
    lines.append('|---|---|---|')
    for name, cost in report['write_cost'].items():
        lines.append(f"| {name} | {cost['ms_per_1k']} | {cost['rows']} |")
    lines.append('')
    lines.append('| 索引 | 建索引耗时（ms，已有数据） |')
    lines.append('|---|---|')
    for k, v in report['index_build_ms'].items():
        lines.append(f'| {k} | {v} |')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='K25 request_logs 索引前后对比（本地临时库）')
    ap.add_argument('--rows', type=int, default=120000)
    ap.add_argument('--repeat', type=int, default=5)
    ap.add_argument('--json', type=str, default='')
    args = ap.parse_args(argv)
    report = run(args.rows, args.repeat)
    print(_md(report))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\nJSON 已写入 {args.json}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
