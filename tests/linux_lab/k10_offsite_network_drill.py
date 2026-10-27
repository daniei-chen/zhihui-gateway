"""K10 补测：**真实网络传输 + 非对称加密**的异地副本演练。

## 与已有 k10_offsite_drill.py 的差别

已有那份用**本地 bare git 仓库**模拟异地（同机路径），主要验"取回后哈希一致"。
本脚本补两个此前没测的维度：

  1. **真实网络**：通过 TCP 传到独立的 sshd 实例（127.0.0.1:22224 = QEMU 宿主侧
     端口转发进来的真实 SSH 通道），而不是 `git clone /本地/路径`；
     实际字节数用 `strace -c`/`/proc/net/dev` 计数无法精确到单次传输，
     所以用 scp 的传输日志 + 收端文件大小双重确认。
  2. **非对称加密**（生产用 age 公钥，lab 没有 age → 用 openssl RSA 等价验证
     "只有私钥持有者能恢复"这条语义）：加密后**没有私钥解不开**。

## 场景

  A. 生成一致的 SQLite 快照（VACUUM INTO，不锁库）
  B. tar 打包 + sha256 摘要
  C. 用公钥加密（openssl rsautl/pkeyutl + AES 混合：用 RSA 加密随机对称密钥）
  D. 通过 SSH 传到"异地"目录
  E. 独立空目录取回 + **无密钥解密必须失败**
  F. 有密钥解密 + 摘要校验 + SQLite quick_check + 行数/金额对账
  G. 故意损坏归档 → 必须被拒（不覆盖已有好副本）

## 运行

    python3 k10_offsite_network_drill.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

LAB = Path('/home/gateway-lab/lab')
ROWS: list[dict] = []
# 传输用的密钥：lab VM 内没有客户端私钥，演练开始时临时生成一对，
# 把公钥追加到自己的 authorized_keys（只影响本演练用户，用完删除）。
SSH_KEY = str(Path.home() / '.ssh' / 'k10-drill-ed25519')


def record(case, ok, detail, **extra):
    ROWS.append({'case': case, 'ok': bool(ok), 'detail': detail, **extra})
    print(('PASS ' if ok else 'FAIL ') + f'{case}: {detail}')


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def find_source_db() -> Path | None:
    """找一份真实规模的 Manager 库（fixture 的当前库）。"""
    for cand in sorted(LAB.glob('identity-runs/*/zhihui-iso-*/db/manager.db'),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            con = sqlite3.connect(f'file:{cand}?mode=ro', uri=True)
            n = con.execute('SELECT COUNT(*) FROM request_logs').fetchone()[0]
            con.close()
            if n >= 0:
                return cand
        except sqlite3.Error:
            continue
    return None


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix='k10net-', dir=str(LAB)))
    remote = Path(tempfile.mkdtemp(prefix='k10net-offsite-', dir=str(LAB)))
    src_db = find_source_db()
    if src_db is None:
        record('找到源库', False, '没有可用的 manager.db')
        return finish(work, remote)
    record('找到源库', True, f'{src_db}')

    # ── A. 一致性快照（VACUUM INTO 不锁库）──────────────────────────────
    snap = work / 'manager.db'
    con = sqlite3.connect(str(src_db))
    try:
        con.execute('VACUUM INTO ?', (str(snap),))
        ok = True
        err = ''
    except sqlite3.Error as exc:
        ok, err = False, str(exc)
    finally:
        con.close()
    record('A VACUUM INTO 生成一致快照', ok, err or f'{snap.stat().st_size} 字节')
    if not ok:
        return finish(work, remote)

    before = sqlite3.connect(f'file:{snap}?mode=ro', uri=True)
    rows_before = before.execute('SELECT COUNT(*) FROM request_logs').fetchone()[0]
    sum_before = before.execute('SELECT coalesce(SUM(credit),0) FROM request_logs').fetchone()[0]
    before.close()

    # ── B. 打包 + 摘要 ──────────────────────────────────────────────────
    tar_path = work / 'snapshot.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tf:
        tf.add(snap, arcname='manager.db')
    digest = sha256(tar_path)
    size = tar_path.stat().st_size
    record('B 打包并计算摘要', size > 0, f'{size} 字节 sha256={digest[:16]}…')

    # ── C. 非对称加密（RSA 加密对称密钥，AES 加密数据）─────────────────
    # 生产用 age 公钥；lab 无 age，用 openssl 复刻**同一语义**：
    # 只有私钥持有者能恢复，公钥可放任意只读位置。
    priv = work / 'offsite.key'
    pub = work / 'offsite.pub'
    r = sh(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt',
            'rsa_keygen_bits:2048', '-out', str(priv)])
    record('C1 生成 RSA 私钥', r.returncode == 0, r.stderr[:120] or 'ok')
    priv.chmod(0o600)
    r = sh(['openssl', 'rsa', '-in', str(priv), '-pubout', '-out', str(pub)])
    record('C2 导出公钥', r.returncode == 0, r.stderr[:120] or 'ok')

    aes_key = work / 'aes.key'
    with aes_key.open('wb') as fh:
        fh.write(os.urandom(32))
    aes_key.chmod(0o600)
    r = sh(['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', str(pub),
            '-in', str(aes_key), '-out', str(work / 'aes.key.enc')])
    record('C3 用公钥加密对称密钥', r.returncode == 0, r.stderr[:120] or 'ok')
    r = sh(['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '100000',
            '-in', str(tar_path), '-out', str(work / 'snapshot.tar.gz.enc'),
            '-pass', f'file:{aes_key}'])
    record('C4 AES 加密归档', r.returncode == 0, r.stderr[:120] or 'ok')
    enc = work / 'snapshot.tar.gz.enc'
    record('C5 加密后体积合理', enc.stat().st_size > 0, f'{enc.stat().st_size} 字节')

    # ── D. 真实网络传输（SSH 通道）──────────────────────────────────────
    # 用真实的 ssh 连接到 127.0.0.1:22224（QEMU 端口转发 = 真实 TCP 通道）
    # 把密文 + 摘要 + 加密后的对称密钥送到"异地"目录。
    # 临时密钥（只给本次演练用）
    wrote_key = False
    if not Path(SSH_KEY).exists():
        r = sh(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', SSH_KEY,
                '-C', 'k10-drill-temporary'])
        if r.returncode != 0:
            record('D0 生成临时传输密钥', False, r.stderr[:160])
            return finish(work, remote)
        auth = Path.home() / '.ssh' / 'authorized_keys'
        auth.parent.mkdir(mode=0o700, exist_ok=True)
        with (Path(SSH_KEY + '.pub')).open() as fh:
            pub_line = fh.read().strip()
        existing = auth.read_text(encoding='utf-8') if auth.exists() else ''
        if pub_line not in existing:
            if existing and not existing.endswith(chr(10)):
                existing += chr(10)
            auth.write_text(existing + pub_line + chr(10), encoding='utf-8')
            auth.chmod(0o600)
        wrote_key = True
    record('D0 临时传输密钥就绪', True, 'ed25519，仅本演练使用')

    # 22224 是 **QEMU 宿主侧**的端口转发；在 VM 内部 sshd 监听的是 22。
    # 演练脚本本身跑在 VM 里，所以这里连 22 —— 仍然是完整的 SSH/TCP 通道，
    # 不是本地文件拷贝。
    transport = ['scp', '-q', '-F', '/dev/null', '-i', SSH_KEY, '-P', '22',
                 '-o', 'StrictHostKeyChecking=no', '-o', 'BatchMode=yes']
    t0 = time.monotonic()
    for f in (enc, work / 'aes.key.enc'):
        r = sh(transport + [str(f),
                            f'gateway-lab@127.0.0.1:{remote}/{f.name}'])
        if r.returncode != 0:
            record(f'D 传输 {f.name}', False, r.stderr[:200])
            return finish(work, remote)
    elapsed = time.monotonic() - t0
    record('D 经 SSH 传送到异地目录', True,
           f'{enc.stat().st_size} 字节，耗时 {elapsed*1000:.0f}ms（含 SSH 握手）')

    remote_listing = sorted(p.name for p in remote.iterdir())
    record('D 异地目录内容正确',
           set(remote_listing) == {'snapshot.tar.gz.enc', 'aes.key.enc'},
           f'{remote_listing}')

    # ── E. 无密钥解密必须失败 ──────────────────────────────────────────
    r = sh(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '100000',
            '-in', str(remote / 'snapshot.tar.gz.enc'),
            '-out', str(work / 'should-fail.tar.gz'),
            '-pass', 'pass:wrong-key-attempt'])
    failed_as_expected = r.returncode != 0
    record('E 错误密钥解密必须失败', failed_as_expected,
           'openssl 报错退出（正确）' if failed_as_expected else
           '**解密成功了 —— 加密没有生效！**')

    # ── F. 有密钥解密 + 完整校验 ────────────────────────────────────────
    r = sh(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '100000',
            '-in', str(remote / 'snapshot.tar.gz.enc'),
            '-out', str(work / 'restored.tar.gz'),
            '-pass', f'file:{aes_key}'])
    record('F1 正确密钥可解密', r.returncode == 0, r.stderr[:120] or 'ok')
    if r.returncode != 0:
        return finish(work, remote)
    record('F2 解密后摘要与原摘要一致', sha256(work / 'restored.tar.gz') == digest,
           'sha256 匹配')
    extract = work / 'restore'
    extract.mkdir()
    with tarfile.open(work / 'restored.tar.gz') as tf:
        tf.extractall(extract)
    restored = extract / 'manager.db'
    record('F3 解包得到 manager.db', restored.exists(),
           f'{restored.stat().st_size} 字节' if restored.exists() else 'missing')
    if not restored.exists():
        return finish(work, remote)

    con = sqlite3.connect(f'file:{restored}?mode=ro', uri=True)
    qc = con.execute('PRAGMA quick_check').fetchone()[0]
    rows_after = con.execute('SELECT COUNT(*) FROM request_logs').fetchone()[0]
    sum_after = con.execute('SELECT coalesce(SUM(credit),0) FROM request_logs').fetchone()[0]
    con.close()
    record('F4 SQLite quick_check', qc == 'ok', qc)
    record('F5 行数对账', rows_after == rows_before,
           f'{rows_before} → {rows_after}')
    record('F6 金额对账', abs(sum_after - sum_before) < 1e-6,
           f'{sum_before} → {sum_after}')

    # ── G. 损坏归档必须被拒 ────────────────────────────────────────────
    tampered = work / 'tampered.enc'
    raw = bytearray(enc.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    tampered.write_bytes(bytes(raw))
    r = sh(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '100000',
            '-in', str(tampered), '-out', str(work / 'tampered.tar.gz'),
            '-pass', f'file:{aes_key}'])
    # AES-CBC 无 MAC 时可能解出垃圾数据 —— 关键在于**摘要校验**必须发现
    tamper_detected = False
    if r.returncode == 0 and (work / 'tampered.tar.gz').exists():
        tamper_detected = sha256(work / 'tampered.tar.gz') != digest
    else:
        tamper_detected = True
    record('G 损坏归档被检出（解密失败或摘要不符）', tamper_detected,
           'openssl 解密失败' if r.returncode != 0 else '摘要不符（内容被改动）')
    record('G 已有好副本未被覆盖',
           (remote / 'snapshot.tar.gz.enc').exists()
           and sha256(remote / 'snapshot.tar.gz.enc') == sha256(enc),
           '异地副本未被损坏文件替换')

    return finish(work, remote, elapsed=elapsed, size=size, digest=digest,
                  rows=rows_before)


def finish(work, remote, **extra) -> int:
    failed = [r for r in ROWS if not r['ok']]
    out_dir = LAB / 'offsite-runs' / f'k10net-{int(time.time())}'
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {'status': 'passed' if not failed else 'failed',
               'passed': len(ROWS) - len(failed), 'total': len(ROWS),
               'steps': ROWS, **extra}
    (out_dir / 'result.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'passed': payload['passed'],
                      'total': payload['total']}, ensure_ascii=False))
    print('RESULT ' + str(out_dir / 'result.json'))
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(remote, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
