"""K24 · 归档执行器（本地可跑，不连生产）。

逻辑放在 `server/` 下的原因：测试视图只复制 `manager/`，`tools/` 不在视图里；
当前没有单独的 `tools/archive_ledgers.py` 薄壳；直接使用
`python -m server.archival` 即可。默认只读预览，任何写入都要显式 `--apply`。

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
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from contextlib import closing

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


def _fsync_file(handle) -> None:
    """Make an archive/manifest durable before the run is marked verified."""
    handle.flush()
    # A failed file fsync means the archive is not proven durable.  Do not
    # mark the run verified and do not authorize deletion in that case.
    os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Directory durability is required on POSIX; unavailable on Windows."""
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
                    before_ts: int, batch: int = 1000, dry_run: bool = False,
                    max_rows: int = 10000, restart_scan: bool = False) -> dict:
    """导出一个数据集保留期满的行 → JSONL + manifest + 校验，然后登记 archive_runs。

    幂等：水位来自上一次 `state ∈ {verified, deleted}` 的 archive_runs 行；中断后
    重跑从水位继续，不会重复导出已归档行。每次运行写**独立文件**
    （`<dataset>.<started_at>.jsonl`），因此 sha256 覆盖的永远是整个文件。
    """
    d = retention.dataset(name)
    batch = max(1, min(1000, int(batch)))
    max_rows = max(1, min(10000, int(max_rows)))
    guard = retention.active_reservation_guard(conn, d)
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
    if restart_scan:
        watermark = 0
    if dry_run:
        # The dry-run count must use the same predicate as the real export;
        # otherwise it reports rows that the export intentionally excludes
        # (for example request_id IS NULL or an unresolved budget reservation).
        where = [f'{d.watermark} > ?']
        args: list[object] = [watermark]
        if d.time_column:
            where.append(f'{d.time_column} < ?')
            args.append(cutoff)
        if d.archive_filter:
            where.append(d.archive_filter)
        if guard:
            where.append(guard)
        row = conn.execute(
            f'SELECT COUNT(*) FROM {d.name} WHERE ' + ' AND '.join(where),
            tuple(args)).fetchone()
        return {'dataset': name, 'dry_run': True, 'pending_rows': int(row[0] or 0),
                'resume_watermark': watermark,
                'action': d.action, 'retention_days': d.retention_days,
                'unknown_budget_excluded': name == 'budget_reservations'}

    if out_dir.is_symlink():
        raise ValueError('archive directory must not be a symlink')
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt' and out_dir.stat().st_mode & 0o077:
        raise ValueError('archive directory must be private')
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
    with target.open('xb') as fh:
        os.chmod(target, 0o600)
        while rows_written < max_rows:
            sql, args = d.select_sql(cutoff, last_watermark, min(batch, max_rows - rows_written), guard)
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
        _fsync_file(fh)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    sha = digest.hexdigest()
    manifest = {
        'dataset': name, 'class': d.klass, 'action': d.action,
        'retention_days': d.retention_days,
        'cutoff_ts': cutoff, 'generated_at': int(time.time()),
        'rows_in_this_run': rows_written, 'watermark': last_watermark,
        'maximum_rows': max_rows,
        'resume_watermark': watermark,
        'sha256': sha, 'sums_of_this_run': sums,
        'file': str(target), 'source_table': d.name,
        'policy_evidence': d.evidence,
        'excluded': ('state NOT IN (settled, released)'
                     if name == 'budget_reservations' else ''),
        'note': ('不可变事件表：本脚本只导出快照，删除会被触发器拒绝'
                 if d.action in (retention.ACTION_NO_DELETE, retention.ACTION_NEVER) else ''),
    }
    manifest_tmp = manifest_path.with_name(manifest_path.name + '.tmp')
    with manifest_tmp.open('w', encoding='utf-8') as fh:
        fh.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        _fsync_file(fh)
    os.replace(manifest_tmp, manifest_path)
    try:
        os.chmod(manifest_path, 0o600)
    except OSError:
        pass
    _fsync_directory(out_dir)
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
    if not path.is_file() or path.is_symlink():
        return {'ok': False, 'reason': f'文件不存在：{path}'}
    actual = _sha256(path)
    ok = expected_sha is None or expected_sha == actual
    lines = 0
    bad = 0
    samples: list[dict] = []
    with path.open('rb') as fh:
        for raw in fh:
            lines += 1
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError('archive record must be an object')
                if len(samples) < sample and lines % 97 == 1:
                    samples.append(record)
            except ValueError:
                bad += 1
    return {'ok': ok and bad == 0, 'sha256': actual,
            'sha256_expected': expected_sha, 'lines': lines,
            'unparsable_sampled': bad,
            'unparsable_rows': bad,
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
    # Do not trust the caller's trio of watermark/sha/confirm-sha.  Resolve the
    # exact verified archive run and re-open its file before deleting anything.
    # Otherwise an arbitrary string that equals itself could be supplied with a
    # watermark and would delete rows that were never archived.
    run = conn.execute(
        "SELECT id,path,watermark_id,rows_archived,state FROM archive_runs "
        "WHERE dataset=? AND watermark_id=? AND sha256=? "
        "AND state IN ('verified','archived') ORDER BY id DESC LIMIT 1",
        (name, watermark, sha)).fetchone()
    if run is None:
        raise SystemExit('拒绝删除：没有匹配 dataset/watermark/sha256 的已验证归档记录')
    run_id, archive_path, archived_watermark, rows_archived, _state = run
    archive_file = Path(str(archive_path or ''))
    if not archive_path or archive_file.is_symlink() or not archive_file.is_file():
        raise SystemExit('拒绝删除：归档文件不存在或不是普通文件')
    verification = verify_archive(archive_file, expected_sha=sha)
    if not verification.get('ok'):
        raise SystemExit('拒绝删除：归档文件校验失败')
    if isinstance(rows_archived, int) and verification.get('lines') != rows_archived:
        raise SystemExit('拒绝删除：归档行数与台账不一致')
    manifest_path = archive_file.with_name(archive_file.name[:-len('.jsonl')] + '.manifest.json')
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SystemExit('拒绝删除：归档 manifest 不是普通文件')
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if (manifest.get('dataset') != name
                    or int(manifest.get('watermark', -1)) != int(watermark)
                    or manifest.get('sha256') != sha
                    or int(manifest.get('rows_in_this_run', -1)) != int(rows_archived)):
                raise ValueError('manifest mismatch')
        except (OSError, ValueError, TypeError) as exc:
            raise SystemExit('拒绝删除：归档 manifest 与台账不一致') from exc
    if conn.in_transaction:
        raise ValueError('archive deletion requires an independent idle connection')
    records = []
    digest = hashlib.sha256()
    size = 0
    with archive_file.open('rb') as handle:
        for raw in handle:
            size += len(raw)
            if size > 64 * 1024 * 1024 or len(records) >= 10000:
                raise SystemExit('拒绝删除：归档超过单轮安全上限，请使用分批归档')
            digest.update(raw)
            records.append(json.loads(raw))
    if digest.hexdigest() != sha:
        raise SystemExit('拒绝删除：归档读取期间发生变化')
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info({d.name})')}
    keys = set()
    for record in records:
        identity = record.get(d.watermark_key)
        if (isinstance(identity, bool) or not isinstance(identity, int)
                or identity <= 0 or identity > watermark or identity in keys
                or set(record) != columns | ({'_watermark'} if d.watermark == 'rowid' else set())):
            raise SystemExit('拒绝删除：归档主键、字段或水位不一致')
        keys.add(identity)
    total, retained = 0, 0
    batch = max(1, min(250, int(batch)))
    selected_columns = 'rowid AS _watermark,*' if d.watermark == 'rowid' else '*'
    predicates = [f'{d.watermark}=?']
    if d.delete_filter or d.archive_filter:
        predicates.append(d.delete_filter or d.archive_filter)
    guard = retention.active_reservation_guard(conn, d)
    if guard:
        predicates.append(guard)
    eligible = ' AND '.join(predicates)
    for offset in range(0, len(records), batch):
        conn.execute('BEGIN IMMEDIATE')
        try:
            for record in records[offset:offset + batch]:
                identity = record[d.watermark_key]
                cursor = conn.execute(f'SELECT {selected_columns} FROM {d.name} WHERE {eligible}', (identity,))
                current = cursor.fetchone()
                if current is not None and dict(current) == record:
                    total += conn.execute(f'DELETE FROM {d.name} WHERE {eligible}', (identity,)).rowcount
                else:
                    retained += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    conn.execute('UPDATE archive_runs SET state=?, approved_by=? WHERE id=?',
                 ('deleted', 'operator-confirmed-sha', run_id))
    conn.commit()
    return {'dataset': name, 'deleted_rows': total, 'retained_or_absent_rows': retained,
            'watermark': watermark, 'deletion_proof': 'verified-exact-row-equality'}


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


def _cli_connection(path: str | Path, *, readonly: bool) -> sqlite3.Connection:
    """Open an explicit database path without creating a missing file."""
    path = Path(path).resolve()
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f'数据库不存在或不是普通文件：{path}')
    mode = 'ro' if readonly else 'rw'
    try:
        connection = sqlite3.connect(path.as_uri() + f'?mode={mode}', uri=True,
                                     timeout=15)
    except sqlite3.Error as exc:
        raise SystemExit(f'无法打开数据库：{exc}') from exc
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=15000')
    return connection


def _require_archive_table(connection: sqlite3.Connection) -> None:
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_runs'"
    ).fetchone() is None:
        raise SystemExit('数据库缺少 archive_runs；先完成 schema v3 迁移')


def main(argv: list[str] | None = None) -> int:
    """Safe operator CLI for planning, exporting, verifying and pruning.

    `plan`, `archive` without `--apply`, and `verify` are read-only.  A delete
    additionally requires the exact archive run watermark and two copies of
    the archive sha256; the library re-opens and verifies the file itself.
    """
    parser = argparse.ArgumentParser(
        prog='python -m server.archival',
        description='Retention ledger archive tool (read-only unless --apply)')
    commands = parser.add_subparsers(dest='command', required=True)

    plan_cmd = commands.add_parser('plan')
    plan_cmd.add_argument('--db', required=True)
    plan_cmd.add_argument('--datasets', default='',
                          help='comma-separated retention dataset names')

    archive_cmd = commands.add_parser('archive')
    archive_cmd.add_argument('--db', required=True)
    archive_cmd.add_argument('--dataset', required=True)
    archive_cmd.add_argument('--out', required=True)
    archive_cmd.add_argument('--before-ts', type=int, default=None)
    archive_cmd.add_argument('--apply', action='store_true',
                             help='write JSONL/manifest and archive_runs')

    verify_cmd = commands.add_parser('verify')
    verify_cmd.add_argument('--file', required=True)
    verify_cmd.add_argument('--sha', required=True)

    delete_cmd = commands.add_parser('delete')
    delete_cmd.add_argument('--db', required=True)
    delete_cmd.add_argument('--dataset', required=True)
    delete_cmd.add_argument('--watermark', type=int, required=True)
    delete_cmd.add_argument('--sha', required=True)
    delete_cmd.add_argument('--confirm-sha', required=True)
    delete_cmd.add_argument('--apply', action='store_true')

    args = parser.parse_args(argv)
    if args.command == 'plan':
        datasets = [item.strip() for item in args.datasets.split(',') if item.strip()]
        with closing(_cli_connection(args.db, readonly=True)) as connection:
            result = plan(connection, datasets, int(time.time()))
        exit_code = 0
    elif args.command == 'archive':
        before_ts = int(time.time()) if args.before_ts is None else args.before_ts
        with closing(_cli_connection(args.db, readonly=not args.apply)) as connection:
            if args.apply:
                _require_archive_table(connection)
            result = archive_dataset(
                connection, args.dataset, out_dir=Path(args.out).resolve(),
                before_ts=before_ts, dry_run=not args.apply)
        exit_code = 1 if args.apply and result.get('state') != 'verified' else 0
    elif args.command == 'verify':
        result = verify_archive(Path(args.file).resolve(), expected_sha=args.sha)
        exit_code = 0 if result.get('ok') else 1
    else:
        if not args.apply:
            parser.error('delete requires --apply')
        with closing(_cli_connection(args.db, readonly=False)) as connection:
            _require_archive_table(connection)
            result = delete_archived(
                connection, args.dataset, watermark=args.watermark,
                sha=args.sha, confirm_sha=args.confirm_sha)
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))



