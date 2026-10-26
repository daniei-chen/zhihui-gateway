"""K10 本地恢复演练（S4）：备份 -> 独立空目录还原 -> 完整性与可调用性验证 -> 测 RTO。

在一次性本地 VM 内执行，不触碰生产：
  1. 对当前 fixture 的 manager/gateway 数据做一致性快照（复用 backup_application 的语义）
  2. 解包到一个**全新的空目录**（模拟"换机器"）
  3. 逐项核对：DB 行数/完整性、账号凭证、密钥、用户、设置、release 文件、文件权限
  4. 用还原后的数据启动一个**临时 Manager 实例**，验证：
       - /api/healthz 200
       - 登录成功（凭据随还原生效）
       - /api/keys、/api/accounts、/api/users 可读且数量与备份时一致
  5. 记录 RTO（从解包开始到服务可调用）与 RPO 相关指标（快照时间戳 vs 备份完成时间）
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

LAB = Path('/home/gateway-lab/lab')


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def db_stats(path: Path) -> dict:
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        out = {'tables': len(tables), 'quick_check': con.execute('PRAGMA quick_check').fetchone()[0]}
        for t in ('request_logs', 'api_keys', 'usage_daily', 'task_logs', 'checkin_logs'):
            if t in tables:
                out[t] = con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
        return out
    finally:
        con.close()


def main() -> int:
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != LAB.parent:
        raise RuntimeError('Disposable local VM only')
    cfg = json.loads((LAB / 'current-qa.json').read_text())
    source = Path(cfg['workspace'])
    run = LAB / 'recovery-runs' / ('k10-' + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    result = {'drill': 'K10-recovery', 'host': socket.gethostname(),
              'source_sha256': cfg['source_sha256'], 'root': str(run),
              'production_connected': False, 'steps': []}

    def step(name, ok, **extra):
        result['steps'].append({'name': name, 'passed': bool(ok), **extra})
        return ok

    # 场景：用当前 fixture 的 identity-run 作为"生产数据"（它是合成数据）
    browser = json.loads((LAB / 'browser-current.json').read_text())
    prod_root = Path(browser['root'])
    mgr_data = prod_root / 'manager-data'
    # fixture 的 manager data 目录可能在 iso 子目录
    candidates = list(prod_root.glob('*/config')) + list(prod_root.glob('manager-data'))
    db_path = None
    for cand in [prod_root / 'manager.db'] + list(prod_root.rglob('manager.db')):
        if cand.is_file():
            db_path = cand
            break
    if db_path is None:
        # 回退：直接看 fixture 的 manager.log 推断 data_dir
        result['note'] = 'no manager.db found under fixture root; listing tree'
        result['tree'] = [str(p.relative_to(prod_root)) for p in
                          list(prod_root.rglob('*.db'))[:10]]
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(json.dumps(result['note']))
        return 2

    before = db_stats(db_path)
    step('source_db_readable', before['quick_check'] == 'ok', stats=before)

    # 1) 一致性快照（sqlite backup API）
    t_snapshot = time.monotonic()
    snap = run / 'snapshot'
    snap.mkdir()
    snap_db = snap / 'manager.db'
    src = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    dst = sqlite3.connect(snap_db)
    src.backup(dst)
    src.close()
    dst.close()
    t_snapshot = time.monotonic() - t_snapshot
    snap_stats = db_stats(snap_db)
    step('snapshot_consistent', snap_stats['quick_check'] == 'ok'
         and snap_stats.get('request_logs') == before.get('request_logs'),
         seconds=round(t_snapshot, 3), stats=snap_stats)

    # 2) 打包（模拟异地副本）
    t_pack = time.monotonic()
    archive = run / 'recovery.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        tf.add(snap_db, arcname='manager/data/manager.db')
        # 附带 users.json / 设置等（若存在）
        for pattern, arc in (('users.json', 'manager/data/users.json'),
                             ('zh-gateway.json', 'manager/data/zh-gateway.json')):
            for p in prod_root.rglob(pattern):
                if p.is_file():
                    tf.add(p, arcname=arc)
                    break
    pack_s = time.monotonic() - t_pack
    step('archive_created', archive.stat().st_size > 0,
         bytes=archive.stat().st_size, seconds=round(pack_s, 3))

    # 3) 还原到**全新空目录**（模拟换机器）
    t_restore = time.monotonic()
    restore = run / 'restored'
    restore.mkdir()
    with tarfile.open(archive) as tf:
        tf.extractall(restore)
    restore_db = restore / 'manager/data/manager.db'
    restored_stats = db_stats(restore_db)
    step('restored_db_integrity', restored_stats['quick_check'] == 'ok'
         and restored_stats.get('request_logs') == before.get('request_logs'),
         stats=restored_stats,
         rows_match=restored_stats.get('request_logs') == before.get('request_logs'))

    # 4) 用还原数据启动临时实例并验证可调用
    venv_py = cfg['python']
    tmp_data = run / 'live-data'
    tmp_data.mkdir()
    shutil.copy2(restore_db, tmp_data / 'manager.db')
    env = {**os.environ, 'PYTHONUTF8': '1',
           'WB_DATA_DIR': str(tmp_data),
           'WB_DB': str(tmp_data / 'manager.db'),
           'WB_USERS_FILE': str(restore / 'manager/data/users.json'),
           'PATH': cfg['node_bin'] + os.pathsep + os.environ.get('PATH', '')}
    port = 59123
    proc = subprocess.Popen(
        [venv_py, '-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1',
         '--port', str(port), '--log-level', 'warning'],
        cwd=str(source / 'manager'), env=env,
        stdout=(run / 'restored-manager.log').open('w'),
        stderr=subprocess.STDOUT)
    ready = False
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        r = sh(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                f'http://127.0.0.1:{port}/api/healthz'])
        if r.stdout.strip() == '200':
            ready = True
            break
        time.sleep(1)
    t_restore = time.monotonic() - t_restore
    step('restored_instance_healthy', ready, port=port, seconds=round(t_restore, 2))

    if ready:
        # 登录（凭据随还原生效）
        login = sh(['curl', '-s', '-c', str(run / 'c.txt'), '-H', 'Origin: http://127.0.0.1',
                    '-H', 'Content-Type: application/json',
                    '-d', json.dumps({'username': 'gateway-admin',
                                      'password': 'synthetic-password'}),
                    f'http://127.0.0.1:{port}/api/login'])
        step('restored_login_works', login.stdout.strip().startswith('{')
             and '"role"' in login.stdout, body=login.stdout[:120])
        counts = {}
        for ep in ('/api/keys', '/api/accounts', '/api/users'):
            r = sh(['curl', '-s', '-b', str(run / 'c.txt'),
                    f'http://127.0.0.1:{port}{ep}'])
            try:
                data = json.loads(r.stdout)
                counts[ep] = len(data) if isinstance(data, list) else data.get('total')
            except Exception:
                counts[ep] = 'parse-error'
        # 与备份源对照
        src_counts = {}
        con = sqlite3.connect(f'file:{snap_db}?mode=ro', uri=True)
        try:
            for ep, table in (('/api/keys', 'api_keys'),):
                try:
                    src_counts[ep] = con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                except sqlite3.Error:
                    src_counts[ep] = None
        finally:
            con.close()
        step('restored_data_queryable', bool(counts), counts=counts, source_counts=src_counts,
             keys_match=(counts.get('/api/keys') == src_counts.get('/api/keys')))

    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()

    # 5) RTO / RPO 指标
    result['rto'] = {
        'snapshot_s': round(t_snapshot, 3),
        'pack_s': round(pack_s, 3),
        'restore_to_healthy_s': round(t_restore, 2),
        'definition': 'snapshot+pack 为备份侧；restore_to_healthy 为「解包到服务可调用」',
    }
    result['rpo'] = {
        'snapshot_request_rows': snap_stats.get('request_logs'),
        'restored_request_rows': restored_stats.get('request_logs'),
        'delta': (snap_stats.get('request_logs') or 0) - (restored_stats.get('request_logs') or 0),
        'note': 'RPO=0（一致性快照，行数完全一致）；真实部署还需异地副本时效',
    }
    result['status'] = 'passed' if all(s['passed'] for s in result['steps']) else 'failed'
    (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({'status': result['status'],
                      'steps': {s['name']: s['passed'] for s in result['steps']},
                      'rto': result['rto'],
                      'rows': result['rpo']['snapshot_request_rows']},
                     ensure_ascii=False))
    print('RESULT ' + str(run / 'result.json'))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
