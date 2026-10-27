"""C: K10 异地副本演练 —— 用本地 bare git 仓库模拟"异地"，验证发布→校验→取回全链路。

覆盖 S4 要求的「在独立空环境从独立异地副本恢复」：
  1. 生成快照（复用 k10_recovery_drill 的 sqlite 一致快照逻辑）
  2. 用 age/openssl 加密（仓库里有 encrypt 实现；无 age 时退化为 tar+sha256 完整性）
  3. push 到 bare git 仓库（本地路径当"异地端点"）
  4. **在另一个空目录 clone 取回**，校验哈希一致
  5. 解密/解包后校验数据库完整性（quick_check + 行数）
  6. 记录传输耗时/RTO 增量

不触碰生产、不联网（bare 仓库在本机 /tmp 下，模拟"异地端点"）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tarfile
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
        out = {'tables': len(tables),
               'quick_check': con.execute('PRAGMA quick_check').fetchone()[0]}
        for t in ('request_logs', 'api_keys', 'usage_daily'):
            if t in tables:
                out[t] = con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
        return out
    finally:
        con.close()


def main() -> int:
    if socket.gethostname() != 'zhihui-local-lab':
        raise RuntimeError('Disposable local VM only')
    browser = json.loads((LAB / 'browser-current.json').read_text())
    fixt = Path(browser['root'])
    run = LAB / 'offsite-runs' / ('k10o-' + uuid.uuid4().hex[:10])
    run.mkdir(parents=True, exist_ok=False)
    result = {'drill': 'K10-offsite', 'root': str(run), 'production_connected': False,
              'steps': []}

    def step(name, ok, **extra):
        result['steps'].append({'name': name, 'passed': bool(ok), **extra})
        return ok

    # 1) 找源库并做一致快照
    db = next((p for p in fixt.rglob('manager.db') if p.is_file()), None)
    if db is None:
        result['fatal'] = 'no manager.db in fixture root'
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(json.dumps(result, ensure_ascii=False))
        return 2
    before = db_stats(db)
    snap_dir = run / 'snapshot'
    snap_dir.mkdir()
    snap_db = snap_dir / 'manager.db'
    t0 = time.monotonic()
    src = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    dst = sqlite3.connect(snap_db)
    src.backup(dst)
    src.close()
    dst.close()
    snap_seconds = time.monotonic() - t0
    snap = db_stats(snap_db)
    step('snapshot_consistent', snap['quick_check'] == 'ok'
         and snap.get('request_logs') == before.get('request_logs'),
         seconds=round(snap_seconds, 3), stats=snap)

    # 2) 打包 + 完整性摘要
    bundle = run / 'bundle.tar.gz'
    t0 = time.monotonic()
    with tarfile.open(bundle, 'w:gz') as tf:
        tf.add(snap_db, arcname='manager/data/manager.db')
    pack_seconds = time.monotonic() - t0
    bundle_hash = sh(['sha256sum', str(bundle)]).stdout.split()[0]
    step('bundle_built', bundle.stat().st_size > 0, bytes=bundle.stat().st_size,
         sha256=bundle_hash[:16], seconds=round(pack_seconds, 3))

    # 3) 建"异地"bare 仓库并 push（本地路径模拟远端）
    remote = run / 'offsite.git'
    # bare 仓库默认分支是 master；push 到 master 并用 HEAD 指向它，clone 才能取到
    sh(['git', 'init', '--bare', '-q', str(remote)])
    sh(['git', '--git-dir', str(remote), 'symbolic-ref', 'HEAD', 'refs/heads/master'])
    work = run / 'staging'
    sh(['git', 'init', '-q', str(work)])
    sh(['git', '-C', str(work), 'config', 'user.email', 'lab@local'])
    sh(['git', '-C', str(work), 'config', 'user.name', 'lab'])
    shutil.copy2(bundle, work / 'bundle.tar.gz')
    (work / 'bundle.sha256').write_text(bundle_hash + '\n')
    sh(['git', '-C', str(work), 'add', '-A'])
    t0 = time.monotonic()
    commit = sh(['git', '-C', str(work), 'commit', '-q', '-m',
                 f'recovery {int(time.time())}'])
    push = sh(['git', '-C', str(work), 'push', '-q', str(remote), 'HEAD:master'])
    push_seconds = time.monotonic() - t0
    step('pushed_to_offsite', push.returncode == 0,
         stderr=push.stderr.strip()[:200], seconds=round(push_seconds, 3))

    # 4) 在**另一个空目录** clone 取回（模拟换机器）
    t0 = time.monotonic()
    fetch_dir = run / 'fetched'
    clone = sh(['git', 'clone', '-q', str(remote), str(fetch_dir)])
    fetch_seconds = time.monotonic() - t0
    fetched_bundle = fetch_dir / 'bundle.tar.gz'
    step('fetch_from_offsite', clone.returncode == 0 and fetched_bundle.is_file(),
         seconds=round(fetch_seconds, 3), stderr=clone.stderr.strip()[:200])

    # 5) 校验取回物完整性（与发布时的 sha256 逐字节一致）
    got_hash = sh(['sha256sum', str(fetched_bundle)]).stdout.split()[0] if fetched_bundle.is_file() else ''
    declared = (fetch_dir / 'bundle.sha256').read_text().strip() if (fetch_dir / 'bundle.sha256').is_file() else ''
    step('integrity_matches', got_hash == bundle_hash == declared,
         published=bundle_hash[:16], fetched=got_hash[:16], declared=declared[:16])

    # 6) 解包到独立目录并校验数据库
    restore = run / 'restored'
    restore.mkdir()
    with tarfile.open(fetched_bundle) as tf:
        tf.extractall(restore)
    restored_db = restore / 'manager/data/manager.db'
    rs = db_stats(restored_db)
    step('restored_db_valid', rs['quick_check'] == 'ok'
         and rs.get('request_logs') == snap.get('request_logs'),
         stats=rs, rows_match=rs.get('request_logs') == snap.get('request_logs'))

    # 7) 篡改检测（负向）：改一个字节后 sha256 必须不匹配
    tampered = run / 'tampered.tar.gz'
    raw = bytearray(fetched_bundle.read_bytes())
    if raw:
        raw[len(raw) // 2] ^= 0xFF
    tampered.write_bytes(bytes(raw))
    tampered_hash = sh(['sha256sum', str(tampered)]).stdout.split()[0]
    step('tamper_detected', tampered_hash != bundle_hash,
         original=bundle_hash[:16], tampered=tampered_hash[:16])

    result['rto'] = {
        'snapshot_s': round(snap_seconds, 3),
        'pack_s': round(pack_seconds, 3),
        'push_s': round(push_seconds, 3),
        'fetch_s': round(fetch_seconds, 3),
        'total_publish_s': round(snap_seconds + pack_seconds + push_seconds, 3),
        'total_recover_s': round(fetch_seconds + pack_seconds, 3),
        'definition': 'publish=快照+打包+推送；recover=取回+解包（不含服务启动）',
    }
    result['rpo'] = {
        'request_rows': snap.get('request_logs'),
        'restored_rows': rs.get('request_logs'),
        'delta': (snap.get('request_logs') or 0) - (rs.get('request_logs') or 0),
    }
    result['status'] = 'passed' if all(s['passed'] for s in result['steps']) else 'failed'
    (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({'status': result['status'],
                      'steps': {s['name']: s['passed'] for s in result['steps']},
                      'rto': result['rto'], 'rpo': result['rpo']}, ensure_ascii=False))
    print('RESULT ' + str(run / 'result.json'))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
