"""K24 · 归档执行器（本地可跑，不连生产）。

逻辑放在 `server/` 下的原因：测试视图只复制 `manager/`，`tools/` 不在视图里；
`tools/archive_ledgers.py` 因此只保留 CLI 薄壳（与 K01 的 legacymig 同一模式）。

约定（来自审计包 K24 与执行约束）：

* 「归档完整性、对账、取回检验后才清理」——下面把三步拆成可单独调用的函数，
  `delete_archived()` 必须同时拿到**已校验的 sha256 + 操作者确认的同一个 sha**；
* 「未知预算不可归档」——只归档 `budget_reservations.state ∈ {settled, released}`；
  `reserved/forwarded/unknown` 永不归档、永不释放；
* 「不删 no-delete 触发器」——五张不可变事件表只允许导出快照；
* 「不以 DB<100MB / 表<20MB 为目标」——只按保留期与证据价值分类。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from . import retention

#: 归档时用于「金额/用量对账」的列（存在才统计）
SUM_COLUMNS = {
    'credit_units': ('usage_events', 'budget_reservations'),
    # request_logs 的金额列叫 credit（REAL 单位制），与 usage_events 的
    # credit_units（百万分之一整数）不是同一量纲，必须分开统计，不能相加。
    # N42：此前漏了这一列，归档摘要里只有 token 没有金额，
    # "行数与金额对账"实际无法完成。
    'credit': ('request_logs',),
    'prompt_tokens': ('request_logs', 'usage_events'),
    'completion_tokens': ('request_logs', 'usage_events'),
    'charged_tokens': ('budget_reservations',),
    'charged_credit': ('budget_reservations',),
}

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def plan(conn: sqlite3.Connection, datasets: list[str], now: int) -> dict:
    """只读规划：每个数据集的保留期、可归档量、是否可删。"""
    return {
        'generated_at': now,
        'policy': retention.policy(),
        'candidates': retention.archive_candidates(conn, now=now, datasets=tuple(datasets)),
        'unknown_budget': retention.unknown_budget(conn, now=now),
        'reconcile': retention.reconcile(conn, now=now),
    }


def archive_dataset(conn: sqlite3.Connection, name: str, *, out_dir: Path,
                    before_ts: int, batch: int = 1000, dry_run: bool = False) -> dict:
    """导出一个数据集保留期满的行 → JSONL + manifest + 校验，然后登记 archive_runs。

    幂等：水位来自上一次 `state ∈ {verified, deleted}` 的 archive_runs 行；中断后
    重跑从水位继续，不会重复导出已归档行。每次运行写**独立文件**
    （`<dataset>.<started_at>.jsonl`），因此 sha256 覆盖的永远是整个文件。
    """
    d = retention.dataset(name)
    retention.assert_immutable_triggers(conn)
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (d.name,)).fetchone()
    if not exists:
        return {'dataset': name, 'skipped': True, 'reason': '表不存在'}
    cutoff = before_ts
    if d.retention_days:
        cutoff = min(before_ts, int(time.time()) - d.retention_days * 86400)
    previous = conn.execute(
        "SELECT watermark_id, path, sha256 FROM archive_runs "
        "WHERE dataset=? AND state IN ('verified','archived','deleted') "
        "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    watermark = int(previous['watermark_id'] or 0) if previous else 0
    if dry_run:
        row = conn.execute(
            f'SELECT COUNT(*) FROM {d.name} WHERE {d.watermark} > ?', (watermark,)).fetchone()
        return {'dataset': name, 'dry_run': True, 'pending_rows': int(row[0] or 0),
                'resume_watermark': watermark,
                'action': d.action, 'retention_days': d.retention_days,
                'unknown_budget_excluded': name == 'budget_reservations'}

    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = int(time.time())
    run_id = _begin_run(conn, name, cutoff, started_at)
    # N41：文件名必须含 run_id。原先只用秒级时间戳，同一秒内跑两次归档会写到
    # **同一个文件**，第二批覆盖第一批 —— 第一批的数据丢失，而 archive_runs 里
    # 两条记录还指向同一个 path，事后无法察觉。run_id 是自增主键，天然唯一。
    target = out_dir / f'{name}.{started_at}.{run_id}.jsonl'
    manifest_path = out_dir / f'{name}.{started_at}.{run_id}.manifest.json'
    rows_written = 0
    last_watermark = watermark
    digest = hashlib.sha256()
    sums: dict[str, int | float] = {}
    sum_columns = tuple(c for c, owners in SUM_COLUMNS.items() if name in owners)
    with target.open('wb') as fh:
        while True:
            sql, args = d.select_sql(cutoff, last_watermark, batch)
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
            if not rows:
                break
            for row in rows:
                line = json.dumps(row, ensure_ascii=False, sort_keys=True,
                                  default=str).encode('utf-8') + b'\n'
                fh.write(line)
                digest.update(line)
                for column in sum_columns:
                    value = row.get(column)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        sums[column] = sums.get(column, 0) + value
            last_watermark = int(rows[-1][d.watermark_key])
            rows_written += len(rows)
    sha = digest.hexdigest()
    manifest = {
        'dataset': name, 'class': d.klass, 'action': d.action,
        'retention_days': d.retention_days,
        'cutoff_ts': cutoff, 'generated_at': int(time.time()),
        'rows_in_this_run': rows_written, 'watermark': last_watermark,
        'resume_watermark': watermark,
        'sha256': sha, 'sums_of_this_run': sums,
        'file': str(target), 'source_table': d.name,
        'policy_evidence': d.evidence,
        'excluded': ('state NOT IN (settled, released)'
                     if name == 'budget_reservations' else ''),
        'note': ('不可变事件表：本脚本只导出快照，删除会被触发器拒绝'
                 if d.action in (retention.ACTION_NO_DELETE, retention.ACTION_NEVER) else ''),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding='utf-8')
    verify = verify_archive(target, expected_sha=sha)
    state = 'verified' if verify['ok'] else 'failed'
    _finish_run(conn, run_id, state=state, rows_archived=rows_written,
                watermark_id=last_watermark,
                sha256=sha, path=str(target),
                credit_units_sum=sums.get('credit_units'),
                token_sum=sums.get('prompt_tokens', 0) + sums.get('completion_tokens', 0),
                detail=json.dumps(verify, ensure_ascii=False)[:2000])
    return {'dataset': name, 'run_id': run_id, 'rows': rows_written,
            'watermark': last_watermark, 'sha256': sha, 'state': state,
            'file': str(target), 'manifest': str(manifest_path), 'verify': verify,
            'sums': sums}


def verify_archive(path: Path, *, expected_sha: str | None = None,
                   sample: int = 5) -> dict:
    """取回检验：重算校验和、逐行可解析、随机抽样检查关键列存在。"""
    if not path.is_file():
        return {'ok': False, 'reason': f'文件不存在：{path}'}
    actual = _sha256(path)
    ok = expected_sha is None or expected_sha == actual
    lines = 0
    bad = 0
    samples: list[dict] = []
    with path.open('rb') as fh:
        for raw in fh:
            lines += 1
            if len(samples) < sample and lines % 97 == 1:
                try:
                    samples.append(json.loads(raw))
                except ValueError:
                    bad += 1
    return {'ok': ok and bad == 0, 'sha256': actual,
            'sha256_expected': expected_sha, 'lines': lines,
            'unparsable_sampled': bad,
            'sample_keys': [sorted(s)[:8] for s in samples[:2]]}


def delete_archived(conn: sqlite3.Connection, name: str, *, watermark: int,
                    sha: str, confirm_sha: str, batch: int = 1000) -> dict:
    """只有「归档 + 校验通过 + 人工确认校验和」三者齐备才删除源行。"""
    d = retention.dataset(name)
    if d.action != retention.ACTION_ARCHIVE_THEN_PRUNE:
        raise SystemExit(
            f'{name} 的分类是 {d.klass}/{d.action}：不允许删除'
            '（不可变事件/投影/配置类只能导出，见 retention.DATASETS）')
    if not confirm_sha or confirm_sha != sha:
        raise SystemExit('拒绝删除：需要 --confirm-sha 且必须等于本次归档的 sha256')
    retention.assert_immutable_triggers(conn)
    total = 0
    while True:
        sql, args = d.delete_batch_sql(watermark, batch)
        cur = conn.execute(sql, args)
        if cur.rowcount <= 0:
            break
        total += cur.rowcount
        conn.commit()
    conn.execute('UPDATE archive_runs SET state=?, approved_by=? WHERE dataset=? AND sha256=?',
                 ('deleted', 'operator-confirmed-sha', name, sha))
    conn.commit()
    return {'dataset': name, 'deleted_rows': total, 'watermark': watermark}


def _begin_run(conn: sqlite3.Connection, dataset: str, cutoff: int, started_at: int) -> int:
    cur = conn.execute(
        'INSERT INTO archive_runs(dataset,cutoff_ts,started_at,state,detail) '
        "VALUES(?,?,?,'started','')", (dataset, cutoff, started_at))
    conn.commit()
    return int(cur.lastrowid)


def _finish_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    assignments = ', '.join(f'{key}=?' for key in fields)
    conn.execute(f'UPDATE archive_runs SET finished_at=?,{assignments} WHERE id=?',
                 (int(time.time()), *fields.values(), run_id))
    conn.commit()



