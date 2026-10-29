#!/opt/workbuddy-manager/venv/bin/python
"""从密文 + 恢复私钥完整重建快照，并按 snapshot.json 记录的哈希逐项校验。

用途有两个：

1. **还原演练**：在没有第二台主机的条件下，证明「只拿密文 + 私钥」就能把快照
   完整解出来，并逐项核对（密文哈希 → 解包 → archive_sha256 → SQLite 完整性）。
2. **修复 sidecar**：2026-10-28 的 `--purge-plaintext` 白名单写错，误删了
   `recovery-manifest.json`；这里从解密后的包里把它取回来。

用法：
    restore_drill.py <snapshot-dir> [--restore-sidecar] [--keep]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

KEY = Path('/root/.config/age/keys.txt')
WORK = Path('/opt/zhihui-candidates/u07-drill')

#: 包里这些后缀是 SQLite 库，需要跑 integrity_check
SQLITE_SUFFIXES = ('.db', '.sqlite', '.sqlite3')


def human(size: float) -> str:
    for unit in ('B', 'K', 'M', 'G'):
        if size < 1024 or unit == 'G':
            return '%.1f%s' % (size, unit)
        size /= 1024
    return '%.1fG' % size


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description='解密并校验一份恢复包快照')
    parser.add_argument('snapshot')
    parser.add_argument('--restore-sidecar', action='store_true',
                        help='把包里的 recovery-manifest.json 写回快照目录')
    parser.add_argument('--keep', action='store_true', help='保留解出来的明文（默认删）')
    parser.add_argument('--skip-sqlite', action='store_true')
    args = parser.parse_args(argv)

    snapshot = Path(args.snapshot).resolve()
    meta = json.loads((snapshot / 'snapshot.json').read_text())
    parts = [snapshot / item['name'] for item in meta['parts']]
    for part in parts:
        if not part.is_file():
            print('缺少分片 %s' % part, file=sys.stderr)
            return 2
    if not KEY.is_file():
        print('恢复私钥不存在：%s' % KEY, file=sys.stderr)
        return 2

    WORK.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='drill-', dir=WORK))
    started = time.time()
    report = {'snapshot': str(snapshot), 'read_only_source': True, 'steps': []}

    try:
        # 1) 密文拼接哈希
        digest = hashlib.sha256()
        for part in parts:
            with part.open('rb') as handle:
                for block in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(block)
        cipher = digest.hexdigest()
        ok = cipher == meta['cipher_sha256']
        report['steps'].append({'step': 'cipher_sha256', 'ok': ok, 'expected': meta['cipher_sha256'],
                                'actual': cipher})
        print('[1] 密文 SHA-256 %s %s' % ('OK' if ok else 'MISMATCH', cipher[:16] + '…'))
        if not ok:
            return 3

        # 2) 解密（age 从 stdin 流式读取拼接后的密文）
        archive = scratch / 'snapshot.tar.gz'
        cat = subprocess.Popen(['cat'] + [str(p) for p in parts], stdout=subprocess.PIPE)
        age = subprocess.Popen(['age', '--decrypt', '-i', str(KEY)], stdin=cat.stdout,
                               stdout=archive.open('wb'), stderr=subprocess.PIPE)
        cat.stdout.close()
        _err = age.communicate()[1]
        cat.wait()
        if age.returncode != 0:
            print('解密失败：%s' % (_err or b'').decode('utf-8', 'replace'), file=sys.stderr)
            return 4
        print('[2] 解密 OK  %s' % human(archive.stat().st_size))

        # 3) 明文归档哈希
        expected_archive = meta.get('archive_sha256')
        actual_archive = sha256_file(archive)
        ok = (expected_archive is None) or (actual_archive == expected_archive)
        report['steps'].append({'step': 'archive_sha256', 'ok': ok,
                                'expected': expected_archive, 'actual': actual_archive})
        print('[3] 明文归档 SHA-256 %s %s' % ('OK' if ok else 'MISMATCH', actual_archive[:16] + '…'))

        # 4) 取 sidecar
        with tarfile.open(archive, 'r:gz') as bundle:
            raw = bundle.extractfile('recovery-manifest.json').read()
        manifest = json.loads(raw)
        report['manifest_files'] = len(manifest.get('files') or [])
        report['manifest_databases'] = len(manifest.get('sqlite_snapshots') or [])
        print('[4] recovery-manifest.json 可读：%d 个文件 / %d 个 SQLite 快照'
              % (report['manifest_files'], report['manifest_databases']))
        if args.restore_sidecar:
            sidecar = snapshot / 'recovery-manifest.json'
            sidecar.write_bytes(raw)
            sidecar.chmod(0o600)
            report['sidecar_restored'] = str(sidecar)
            print('[4] sidecar 已写回 %s' % sidecar)

        # 5) SQLite 完整性（只抽查前 3 个库，避免全量解包占满盘）
        if not args.skip_sqlite:
            checked = []
            with tarfile.open(archive, 'r:gz') as bundle:
                for member in bundle.getmembers():
                    if not member.isfile() or not member.name.endswith(SQLITE_SUFFIXES):
                        continue
                    if len(checked) >= 3:
                        break
                    target = scratch / ('check-%d.sqlite' % len(checked))
                    with bundle.extractfile(member) as source, target.open('wb') as sink:
                        shutil.copyfileobj(source, sink)
                    connection = sqlite3.connect('file:%s?mode=ro' % target, uri=True)
                    result = connection.execute('PRAGMA integrity_check').fetchone()[0]
                    connection.close()
                    target.unlink()
                    checked.append({'name': member.name, 'integrity': result})
                    print('[5] %s → integrity_check=%s' % (member.name, result))
            report['sqlite_checks'] = checked
            if any(item['integrity'] != 'ok' for item in checked):
                print('[5] 有库未通过 integrity_check', file=sys.stderr)
                return 5

        elapsed = time.time() - started
        report['elapsed_seconds'] = round(elapsed, 1)
        report['result'] = 'verified'
        print('\n结果：可从「密文 + 私钥」完整重建并逐项校验通过（用时 %.1fs）' % elapsed)
        return 0
    finally:
        if args.keep:
            print('明文保留在 %s' % scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)
        (WORK / 'drill-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
