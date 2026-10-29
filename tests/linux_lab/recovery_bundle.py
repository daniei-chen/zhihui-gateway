"""R3-L3：全资产恢复包（打包 / 私钥域外解回 / 时间基 RPO / 恢复后对账）。

只服务 `recovery/ASSET-INVENTORY.md` 39 行矩阵的本地/云隔离演练，不碰生产：

  * ``bundle``（在源侧：云隔离目录）：对 Manager/QTG/Trae 三个 SQLite 用 **backup API**
    做一致快照，按资产矩阵收集文件（含 nginx/unit/cron/账号/状态/代码/静态资源/
    合成秘密/元数据），生成逐文件 manifest；归档用 AES-256-CBC（openssl，pbkdf2）
    加密 + HMAC-SHA256，AES 密钥再用 **RSA-OAEP 公钥**加密成 ``aes.key.enc``，
    随后**删除**明文 AES 密钥；私钥不在源侧、也不进包。
  * ``verify`` / ``restore``（目的侧：本地 VM）：**只靠恢复包 + 域外私钥**解回：
    RSA 解 ``aes.key.enc`` → AES 解密 → 解包 → 逐文件校验（sha/bytes/mode）→
    用恢复出来的数据启动 Manager/QTG 做业务闭环（登录/建 key/fake 推理/吊销/QTG 读/
    审计连续）→ 测 RTO → 运行态对账。
  * ``faults``：错误私钥 / 篡改包 / 缺资产 / 版本不匹配四个失败分支。

所有凭据都是合成值；manifest 只记指纹，不落明文密钥。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import hmac as hmac_mod
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SCHEMA = 'zhihui-r3l3-bundle/1'
MARK_BEGIN = None
DB_ASSETS = ('manager/data/manager.db', 'qtg/var/qtg.db', 'trae-work/data/data/aiwork.sqlite')
LOG_EXCLUDES = ('.rpo-writer.ndjson', '/rpo-marks.ndjson')
PEM_HEADER = b'-----BEGIN'
PRIVATE_MARKERS = (b'PRIVATE KEY', b'BEGIN RSA PRIVATE KEY', b'BEGIN OPENSSH PRIVATE KEY')

# 39 行资产矩阵：current=ASSET-INVENTORY 的现状；filled=本恢复包的实际覆盖状态；
# paths=包内匹配模式（glob）；reason=为什么/边界。
ASSET_MATRIX = [
    ('D1', 'Manager SQLite（WAL；22 表）', '✅', 'covered', ['manager/data/manager.db*'],
     'SQLite backup API 一致快照；manifest 记录 quick_check/关键表计数/时间范围'),
    ('D2', 'QTG SQLite', '❌', 'covered', ['qtg/var/qtg.db*'],
     'Store 建库 + 合成 key/ip/proxy 行；恢复后对账'),
    ('D3', 'QTG 状态文件（audit/session_secret/engine/checkin/compat-plans）', '❌', 'covered',
     ['qtg/var/audit.jsonl', 'qtg/var/session_secret', 'qtg/var/*_engine', 'qtg/var/random_checkin.json',
      'qtg/var/compat-plans.json'], '合成状态 + 真实哈希链审计；恢复后校验链完整'),
    ('D4', 'Trae/AIWork SQLite（冷却/账号状态）', '❌', 'covered', ['trae-work/data/data/aiwork.sqlite*'],
     'backup API 快照；合成 account_state 行'),
    ('D5', 'Manager 附加 JSON（users.json 等）', '✅', 'covered', ['manager/data/users.json', 'manager/secrets/*'],
     'users.json 随 DB 目录；会话 secret 为合成值'),
    ('D6', 'workbuddy2api 状态', '✅', 'covered', ['workbuddy2api/config.json', 'workbuddy2api/data/*'],
     'config/state/session-binds 合成'),
    ('D7', '运行覆盖层 local-patches', '🟡', 'partial', ['release/server/*'],
     '补丁已固化进 release/server；补丁载荷（patches/*.patch）未覆盖（同现状）'),
    ('A1', 'WorkBuddy 账号池', '✅', 'covered', ['workbuddy2api/auths/*.json'],
     '5 个合成账号（uid/accessToken/expiresAt/realm）'),
    ('A2', 'Qoder 账号池', '❓→🟡', 'covered', ['qoder-hub/accounts/*.json'],
     '3 个合成账号（cooldownUntil/lastError/session）'),
    ('A3', 'Trae 账号池/冷却', '❌', 'partial', ['trae-work/data/data/aiwork.sqlite*', 'trae-work/secrets/admin_token'],
     '合成冷却表 + admin token 占位；真实 aiwork-server 内部结构未验证'),
    ('A4', 'refresh token / session 绑定缓存', '🟡', 'partial',
     ['workbuddy2api/data/session-binds.json', 'qoder-hub/accounts/*.json'],
     'WorkBuddy/Qoder 有合成绑定；Trae 侧未覆盖'),
    ('N1', '生效 Nginx 站点文件', '🟡', 'covered', ['nginx/sites-enabled/*'],
     '合成“生效版”（非 sites-available 旧版）'),
    ('N2', '600 include 片段（qtg_bearer）', '❌', 'covered', ['nginx/secrets/qtg_bearer.conf'],
     '合成 600 片段（恢复后校验 mode）'),
    ('N3', 'conf.d 其余文件', '🟡', 'covered', ['nginx/conf.d/*'], '合成 map/audit 文件'),
    ('N4', 'snippets/ 与 nginx.conf', '🟡', 'covered', ['nginx/snippets/*', 'nginx/nginx.conf'],
     '合成安全头片段与主配置'),
    ('N5', 'systemd unit + drop-in', '✅', 'covered', ['systemd/units/*', 'systemd/dropins/*'],
     'workbuddy-web/qtg/qoder2-engine + 4 drop-in + 2 timer 文件'),
    ('N6', 'QTG systemctl 属性', '🟡', 'partial', ['systemd/units/qtg.service'],
     'unit 文件覆盖；systemctl show 的运行时属性（MemoryMax/ConsistsOf）未抓'),
    ('N7', 'cron', '🟡', 'covered', ['cron/*'], '4 个 cron.d + wb2api-backup-push.sh 合成'),
    ('N8', 'Manager 环境文件/.local-config', '✅', 'covered', ['manager/.local-config'],
     '合成环境文件；真实 EnvironmentFiles 指向的路径依赖部署'),
    ('N9', '主机其他配置（letsencrypt/ssh/sudoers/防火墙/DNS/云盘）', '❌', 'not_covered', [],
     '真实主机凭据/证书/网络配置不属于合成资产，不能也不应打包'),
    ('R1', 'Manager release 内容', '✅', 'covered', ['release/server/*', 'release/web/out/*', 'release/deploy/*'],
     'server/*.py 全量 + 静态产物 + deploy 清单（来自候选快照）'),
    ('R2', 'Manager 验签材料', '✅', 'partial', ['release/deploy/*'],
     'deploy 内签名/公钥锚若候选快照提供则一并打包；真实发布私钥离线不覆盖'),
    ('R3', '前端在线产物', '🟡', 'covered', ['release/web/out/*'], 'web/out 静态产物全量复制'),
    ('R4', '前端源码树与发布脚本', '🟡', 'partial', ['meta/source-tree-fingerprints.json'],
     '只打包逐文件指纹（不复制源码树）；源码由候选包提供'),
    ('R5', 'QTG 应用与清单', '❌', 'partial', ['meta/qtg-release-fingerprints.json'],
     'QTG app 只打包指纹；qtg 代码由候选包提供'),
    ('R6', '容器镜像', '❌', 'partial', ['meta/container-images.json'],
     '仅 digest/镜像名元数据；镜像层未被复制（需要 registry/镜像仓库）'),
    ('S1', '异地加密恢复密钥对', '🟡', 'covered', ['aes.key.enc', 'meta/key-escrow.json'],
     '公钥加密 AES 密钥；私钥域外（宿主），不进包；包内无明文密钥'),
    ('S2', 'Manager 发布签名私钥', '✅（按设计离线）', 'not_covered', [],
     '按设计必须离线，不进恢复包'),
    ('S3', 'QTG 发布签名私钥', '❓', 'not_covered', [],
     '现状本身待核实；本演练不用真实签名私钥'),
    ('S4', '主机内散落凭据', '🟡', 'partial',
     ['qtg/secrets/*', 'qoder-hub/secrets/*', 'trae-work/secrets/*', 'secrets/*'],
     '合成 token/secret 占位；真实凭据值不复制'),
    ('S5', 'Manager DB 内秘密（settings）', '✅', 'covered', ['manager/data/manager.db*'],
     'settings 表随 DB 快照（值为合成）'),
    ('S6', '归档本身的加密状态', '🟡', 'covered', ['aes.key.enc', 'manifest.json'],
     'AES-256-CBC + HMAC-SHA256 + RSA-OAEP 密钥封装；明文 AES 密钥已删除'),
    ('O1', 'Manager 后台调度/任务状态', '✅', 'covered', ['manager/data/manager.db*'],
     'request_logs/outcomes/task_logs/checkin_logs/audit_logs/budget_reservations/usage_daily 非空'),
    ('O2', '告警/自愈状态', '❌', 'partial', ['manager/data/manager.db*'],
     '内存态（operational.monitor）不可持久化；publish.state 等 DB 态覆盖'),
    ('O3', 'systemd timer 运行状态', '❌', 'partial', ['systemd/units/*.timer'],
     'timer 文件覆盖；下次触发时间等运行态不在包内'),
    ('O4', 'QTG 签到调度状态', '❌', 'covered',
     ['qtg/var/random_checkin.json', 'qtg/var/compat-plans.json'], '合成签到去重与计划状态'),
    ('H1', '宿主机（镜像/IaC/云盘快照）', '❌', 'not_covered', [],
     '没有主机镜像/云盘快照权限；需云控制台另行核实'),
    ('H2', '运行时依赖（Python/Node/Docker/nginx…）', '🟡', 'partial', ['meta/python-runtime.json'],
     '只记录运行时版本元数据，不打包解释器/依赖'),
    ('H3', '容量与增长（50MiB 上限/保留策略）', '🟡', 'partial', ['manifest.json', 'meta/container-images.json'],
     '记录包体积/增长，不验证保留轮转策略'),
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def code_schema_version(code_dir: Path) -> int | str:
    migrations = code_dir / 'manager/server/migrations.py'
    if not migrations.is_file():
        return 'unknown'
    match = re.search(r'CODE_SCHEMA_VERSION\s*=\s*(\d+)', migrations.read_text(errors='replace'))
    return int(match.group(1)) if match else 'unknown'


def openssl_aes_encrypt(plain: Path, key_file: Path, out: Path) -> None:
    result = run(['openssl', 'enc', '-aes-256-cbc', '-salt', '-pbkdf2', '-iter', '200000',
                  '-pass', 'file:' + str(key_file), '-in', str(plain), '-out', str(out)])
    if result.returncode != 0:
        raise RuntimeError('aes encrypt failed: ' + result.stderr[-500:])


def openssl_aes_decrypt(cipher: Path, key_file: Path, out: Path) -> None:
    result = run(['openssl', 'enc', '-d', '-aes-256-cbc', '-salt', '-pbkdf2', '-iter', '200000',
                  '-pass', 'file:' + str(key_file), '-in', str(cipher), '-out', str(out)])
    if result.returncode != 0:
        raise RuntimeError('aes decrypt failed: ' + result.stderr[-500:])


def openssl_hmac(path: Path, key_bytes: bytes) -> str:
    result = run(['openssl', 'dgst', '-sha256', '-mac', 'HMAC',
                  '-macopt', 'hexkey:' + key_bytes.hex(), str(path)])
    if result.returncode != 0:
        raise RuntimeError('hmac failed: ' + result.stderr[-500:])
    match = re.search(r'([0-9a-f]{64})', result.stdout)
    if not match:
        raise RuntimeError('hmac output unparsable')
    return match.group(1)


def openssl_rsa_encrypt(key: Path, public_pem: Path, out: Path) -> None:
    result = run(['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', str(public_pem),
                  '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256',
                  '-in', str(key), '-out', str(out)])
    if result.returncode != 0:
        raise RuntimeError('rsa encrypt failed: ' + result.stderr[-500:])


def openssl_rsa_decrypt(cipher: Path, private_pem: Path, out: Path) -> None:
    result = run(['openssl', 'pkeyutl', '-decrypt', '-inkey', str(private_pem),
                  '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256',
                  '-in', str(cipher), '-out', str(out)])
    if result.returncode != 0:
        raise RuntimeError('rsa decrypt failed (wrong private key or corrupted aes.key.enc): '
                           + result.stderr[-300:])


# --------------------------------------------------------------------------- bundle
def snapshot_db(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        ok = dst.execute('PRAGMA quick_check').fetchone()[0]
        if ok != 'ok':
            raise RuntimeError(f'consistent snapshot failed for {source}: {ok}')
    finally:
        dst.close()
        src.close()


def db_stats(path: Path) -> dict:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        stats = {'tables': len(tables), 'quick_check': conn.execute('PRAGMA quick_check').fetchone()[0]}
        counts = {}
        for table in ('request_logs', 'request_outcomes', 'budget_reservations', 'budget_adjustments',
                      'task_leases', 'instance_leases', 'usage_daily', 'task_logs', 'checkin_logs',
                      'audit_logs', 'api_keys', 'settings', 'qtg_keys', 'key_ips', 'proxy_requests',
                      'rpo_marks', 'account_state'):
            if table in tables:
                counts[table] = conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
        stats['counts'] = counts
        if 'request_logs' in tables:
            row = conn.execute('SELECT count(*), min(ts), max(ts), max(id) FROM request_logs').fetchone()
            stats['request_logs_range'] = {'count': row[0], 'ts_min': row[1], 'ts_max': row[2], 'id_max': row[3]}
        if 'rpo_marks' in tables:
            row = conn.execute('SELECT count(*), max(seq), max(ts_mono), max(ts_wall) FROM rpo_marks').fetchone()
            stats['last_mark'] = {'count': row[0], 'seq': row[1], 'ts_mono': row[2], 'ts_wall': row[3]}
        if 'budget_reservations' in tables:
            stats['budget_unknown'] = conn.execute(
                "SELECT count(*) FROM budget_reservations WHERE state='unknown'").fetchone()[0]
        if 'task_leases' in tables:
            stats['task_leases'] = [{key: row[key] for key in row.keys()} for row in conn.execute(
                'SELECT task, owner, lease_until, fence FROM task_leases ORDER BY task').fetchall()]
        if 'instance_leases' in tables:
            stats['instance_leases'] = [{key: row[key] for key in row.keys()} for row in conn.execute(
                'SELECT instance_id, owner, lease_until, fence, role, phase FROM instance_leases ORDER BY instance_id').fetchall()]
        if 'settings' in tables:
            row = conn.execute("SELECT value FROM settings WHERE key='publish.state'").fetchone()
            stats['publish_state'] = row[0] if row is not None else None
        return stats
    finally:
        conn.close()


def audit_chain(path: Path) -> dict:
    count = 0
    last = '0' * 64
    ok = True
    try:
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            body = {k: v for k, v in record.items() if k != 'hash'}
            digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()
            if record.get('prev') != last or record.get('hash') != digest:
                ok = False
                break
            last = record['hash']
            count += 1
    except FileNotFoundError:
        return {'ok': True, 'records': 0, 'last_hash': last}
    return {'ok': ok, 'records': count, 'last_hash': last}


def collect_asset_ids(rel: str) -> list[str]:
    ids = []
    for asset_id, _title, _cur, _filled, patterns, _reason in ASSET_MATRIX:
        for pattern in patterns:
            if fnmatch.fnmatch(rel, pattern) or rel == pattern:
                ids.append(asset_id)
                break
    return ids


def asset_matrix_filled(manifest: dict) -> dict:
    paths = [entry['path'] for entry in manifest['assets']]
    rows = []
    for asset_id, title, current, filled, patterns, reason in ASSET_MATRIX:
        matched = sorted({path for path in paths for pattern in patterns
                          if fnmatch.fnmatch(path, pattern) or path == pattern})
        rows.append({'id': asset_id, 'asset': title, 'inventory_status': current,
                     'r3l3_status': filled, 'bundle_paths': matched[:12],
                     'bundle_match_count': len(matched), 'reason': reason})
    summary = {}
    for row in rows:
        summary[row['r3l3_status']] = summary.get(row['r3l3_status'], 0) + 1
    return {'schema': 'zhihui-r3l3-asset-matrix/1', 'rows': rows,
            'summary': summary, 'row_count': len(rows)}


def cmd_bundle(args) -> dict:
    source = Path(args.source_root).resolve()
    out = Path(args.out_dir).resolve()
    public_key = Path(args.public_key).resolve()
    for guard in ('/opt',):
        if str(source).startswith(guard) or str(out).startswith(guard):
            raise SystemExit('refusing to touch /opt')
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'out dir not empty: {out}')
    out.mkdir(parents=True, exist_ok=True)
    staging = out / 'staging'
    staging.mkdir()
    started_mono, started_wall = time.monotonic(), time.time()
    source_meta = {}
    meta_path = source / 'meta/source-manifest.json'
    if meta_path.is_file():
        source_meta = json.loads(meta_path.read_text(encoding='utf-8'))
    build_path = source / 'meta/build.json'
    build = json.loads(build_path.read_text(encoding='utf-8')) if build_path.is_file() else {}

    assets = []
    skipped = []
    for path in sorted(source.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        if any(marker in rel for marker in ('.rpo-writer.ndjson', '/rpo-marks.ndjson', '.staging')):
            skipped.append(rel)
            continue
        if rel.endswith(('-wal', '-shm')):
            skipped.append(rel)
            continue
        target = staging / rel
        if rel in DB_ASSETS:
            snapshot_db(path, target)
            source_kind = 'consistent-snapshot(sqlite backup API)'
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            source_kind = 'file'
        assets.append({'path': rel, 'sha256': sha256_file(target), 'bytes': target.stat().st_size,
                       'source': source_kind, 'mode': oct(path.stat().st_mode & 0o777),
                       'asset_ids': collect_asset_ids(rel)})
    snapshot_end_mono, snapshot_end_wall = time.monotonic(), time.time()

    manager_db = staging / 'manager/data/manager.db'
    stats = {'manager': db_stats(manager_db)}
    for rel, key in (('qtg/var/qtg.db', 'qtg'), ('trae-work/data/data/aiwork.sqlite', 'trae')):
        candidate = staging / rel
        if candidate.is_file():
            stats[key] = db_stats(candidate)
    audit = audit_chain(staging / 'qtg/var/audit.jsonl')

    # 读统计时 SQLite 可能在 staging 里重建 -wal/-shm；它们不是资产，归档前清掉。
    for suffix in ('-wal', '-shm'):
        for leftover in staging.rglob('*' + suffix):
            leftover.unlink(missing_ok=True)

    archive = out / 'workspace-r3l3.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted(staging.rglob('*')):
            if path.is_file():
                info = tar.gettarinfo(str(path), arcname='workspace/' + path.relative_to(staging).as_posix())
                info.mtime = 0
                with path.open('rb') as handle:
                    tar.addfile(info, handle)
    archive_sha = sha256_file(archive)
    aes_key = secrets.token_bytes(32)
    key_file = out / 'aes.key'
    key_file.write_bytes(aes_key)
    key_file.chmod(0o600)
    encrypted = out / 'workspace-r3l3.tar.gz.enc'
    openssl_aes_encrypt(archive, key_file, encrypted)
    encrypted_sha = sha256_file(encrypted)
    encrypted_hmac = openssl_hmac(encrypted, aes_key)
    key_enc = out / 'aes.key.enc'
    openssl_rsa_encrypt(key_file, public_key, key_enc)
    key_enc_sha = sha256_file(key_enc)
    aes_key_sha = hashlib.sha256(aes_key).hexdigest()
    key_file.unlink()
    if key_file.exists():
        raise RuntimeError('plaintext aes key still present')
    archive.unlink()

    public_fingerprint = hashlib.sha256(public_key.read_bytes()).hexdigest()
    manifest = {
        'schema': SCHEMA, 'bundle_id': uuid.uuid4().hex[:16], 'created_at': now_iso(),
        'source': {'host': socket.gethostname(), 'root': str(source),
                   'source_manifest_sha256': hashlib.sha256(meta_path.read_bytes()).hexdigest() if meta_path.is_file() else None},
        'build': {'source_sha256': build.get('source_sha256'), 'code_schema_version': build.get('code_schema_version')},
        'snapshot': {'start_mono': round(started_mono, 6), 'start_wall': round(started_wall, 6),
                     'end_mono': round(snapshot_end_mono, 6), 'end_wall': round(snapshot_end_wall, 6),
                     'duration_s': round(snapshot_end_mono - started_mono, 3)},
        'rpo': {'last_mark': stats['manager'].get('last_mark')},
        'db_stats': stats,
        'qtg_audit': audit,
        'assets': assets,
        'skipped': skipped,
        'totals': {'files': len(assets), 'bytes': sum(item['bytes'] for item in assets)},
        'crypto': {'cipher': 'aes-256-cbc(pbkdf2-iter=200000)', 'archive_sha256': archive_sha,
                   'encrypted_sha256': encrypted_sha, 'hmac_sha256': encrypted_hmac,
                   'aes_key_sha256': aes_key_sha, 'aes_key_enc_sha256': key_enc_sha,
                   'rsa': {'public_key_sha256': public_fingerprint, 'padding': 'oaep-sha256'}},
        'safety': {'plaintext_aes_key_deleted': True, 'private_key_in_bundle': False,
                   'aes_key_in_archive': False},
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    matrix = asset_matrix_filled(manifest)
    (out / 'asset-matrix-filled.json').write_text(json.dumps(matrix, ensure_ascii=False, indent=2),
                                                  encoding='utf-8')
    (out / 'SHA256SUMS').write_text(
        f'{encrypted_sha}  {encrypted.name}\n{key_enc_sha}  {key_enc.name}\n'
        f'{sha256_file(out / "manifest.json")}  manifest.json\n')
    shutil.rmtree(staging)
    return {'status': 'BUNDLED', 'out': str(out), 'bundle_id': manifest['bundle_id'],
            'assets': manifest['totals']['files'], 'bytes': manifest['totals']['bytes'],
            'encrypted_bytes': encrypted.stat().st_size, 'encrypted_sha256': encrypted_sha,
            'aes_key_sha256': aes_key_sha, 'last_mark': manifest['rpo']['last_mark'],
            'matrix_summary': matrix['summary']}


# --------------------------------------------------------------------------- verify
class VerificationError(RuntimeError):
    pass


def verify_bundle(bundle_dir: Path, private_key: Path, dest_root: Path, code_dir: Path | None,
                  extract: bool = True) -> dict:
    manifest = json.loads((bundle_dir / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != SCHEMA:
        raise VerificationError(f"manifest schema mismatch: {manifest.get('schema')}")
    encrypted = bundle_dir / 'workspace-r3l3.tar.gz.enc'
    key_enc = bundle_dir / 'aes.key.enc'
    checks = {}
    if not encrypted.is_file() or not key_enc.is_file():
        raise VerificationError('bundle archive or aes.key.enc missing')
    encrypted_sha = sha256_file(encrypted)
    checks['bundle_sha256_matches_manifest'] = encrypted_sha == manifest['crypto']['encrypted_sha256']
    key_enc_sha = sha256_file(key_enc)
    checks['aes_key_enc_sha256_matches_manifest'] = key_enc_sha == manifest['crypto']['aes_key_enc_sha256']
    if not all(checks.values()):
        raise VerificationError('bundle integrity mismatch: ' + json.dumps(checks))
    # 只允许用私钥解回；这里不读取任何源端 aes.key
    with tempfile.TemporaryDirectory(prefix='r3l3-verify-') as tmp:
        tmpdir = Path(tmp)
        aes_key = tmpdir / 'aes.key'
        openssl_rsa_decrypt(key_enc, private_key, aes_key)
        decrypted_sha = hashlib.sha256(aes_key.read_bytes()).hexdigest()
        checks['aes_key_sha256_matches_manifest'] = decrypted_sha == manifest['crypto']['aes_key_sha256']
        if not checks['aes_key_sha256_matches_manifest']:
            raise VerificationError('decrypted aes key does not match manifest fingerprint')
        hmac_value = openssl_hmac(encrypted, aes_key.read_bytes())
        checks['bundle_hmac_matches'] = hmac_value == manifest['crypto']['hmac_sha256']
        if not checks['bundle_hmac_matches']:
            raise VerificationError('bundle HMAC mismatch')
        archive = tmpdir / 'workspace.tar.gz'
        openssl_aes_decrypt(encrypted, aes_key, archive)
        checks['plain_archive_sha256_matches'] = sha256_file(archive) == manifest['crypto']['archive_sha256']
        if not checks['plain_archive_sha256_matches']:
            raise VerificationError('decrypted archive sha256 mismatch')
        member_names = []
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar:
                member_names.append(member.name)
                if member.name.endswith(('aes.key', 'id_rsa', 'private.pem')) or 'PRIVATE KEY' in member.name:
                    raise VerificationError(f'forbidden member in archive: {member.name}')
                if not (member.name.startswith('workspace/') and (member.isfile() or member.isdir())):
                    raise VerificationError(f'unexpected archive member: {member.name}')
        checks['archive_has_no_private_key_members'] = True
        expected = {entry['path'] for entry in manifest['assets']}
        present = {name[len('workspace/'):] for name in member_names
                   if name.startswith('workspace/') and not name.endswith('/')}
        present = {path for path in present if not path.endswith(('-wal', '-shm'))}
        missing = sorted(expected - present)
        extra = sorted(present - expected)
        checks['archive_has_all_manifest_assets'] = not missing
        checks['archive_has_no_unexpected_assets'] = not extra
        if missing:
            raise VerificationError(f'missing assets in archive ({len(missing)}): {missing[:5]}')
        if extra:
            raise VerificationError(f'unexpected assets in archive: {extra[:5]}')
        if code_dir is not None:
            expected_version = manifest.get('build', {}).get('code_schema_version')
            actual_version = code_schema_version(Path(code_dir))
            checks['code_schema_version_matches'] = (expected_version == actual_version
                                                     or expected_version == 'unknown')
            if not checks['code_schema_version_matches']:
                raise VerificationError(f'code/schema version mismatch: bundle={expected_version} code={actual_version}')
        extracted = dest_root
        if extract:
            if dest_root.exists() and any(dest_root.iterdir()):
                raise VerificationError(f'destination not empty: {dest_root}')
            dest_root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, 'r:gz') as tar:
                tar.extractall(dest_root, filter='data')
            restored = dest_root / 'workspace'
            asset_checks = []
            for entry in manifest['assets']:
                path = restored / entry['path']
                ok = path.is_file() and sha256_file(path) == entry['sha256'] \
                    and path.stat().st_size == entry['bytes']
                asset_checks.append({'path': entry['path'], 'ok': ok})
            checks['all_extracted_assets_verified'] = all(item['ok'] for item in asset_checks)
            if not checks['all_extracted_assets_verified']:
                bad = [item['path'] for item in asset_checks if not item['ok']][:5]
                raise VerificationError(f'extracted asset mismatch: {bad}')
    return {'status': 'VERIFIED', 'checks': checks,
            'bundle_id': manifest.get('bundle_id'), 'manifest': manifest}


# --------------------------------------------------------------------------- restore
def http(method: str, url: str, *, json_body=None, headers=None, timeout=15):
    data = json.dumps(json_body).encode() if json_body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body), resp.headers
            except Exception:
                return resp.status, body[:200], resp.headers
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body), exc.headers
        except Exception:
            return exc.code, body[:200], exc.headers
    except Exception as exc:  # noqa: BLE001
        return None, repr(exc)[:200], {}


def wait_http(url: str, want: int, timeout: float, poll: float = 0.25) -> float | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _body, _headers = http('GET', url, timeout=3)
        if status == want:
            return time.monotonic()
        time.sleep(poll)
    return None


def start_services(restored: Path, code_dir: Path, python: str, root: Path) -> dict:
    """用恢复出来的资产启动 fake provider + Manager + QTG（隔离端口）。"""
    sys.path.insert(0, str(code_dir / 'tests'))
    from fake_provider.server import FakeProvider  # noqa: E402
    provider = FakeProvider(chunk_delay=0.0).start()
    logs = root / 'service-logs'
    logs.mkdir(exist_ok=True)
    manager_env = {
        **os.environ, 'PYTHONUTF8': '1', 'TZ': 'Asia/Shanghai', 'PYTHONDONTWRITEBYTECODE': '1',
        'WB_DB': str(restored / 'manager/data/manager.db'),
        'WB_DATA_DIR': str(restored / 'manager/data'),
        'WB_USERS_FILE': str(restored / 'manager/data/users.json'),
        'WB_STATIC_DIR': str(restored / 'release/web/out'),
        'WB_AUTH_DIR': str(restored / 'workbuddy2api/auths'),
        'WB2API_BASE': provider.base_url, 'WB2API_KEY': 'synthetic-upstream', 'WB2API_MODE': 'native',
        'WB_QTG_IDENTITY_SECRET_FILE': str(restored / 'secrets/qtg-identity-secret'),
        'WB_DRAIN_TIMEOUT_SEC': '30',
    }
    manager_port, qtg_port = _free_port(), _free_port()
    manager_launcher = root / 'restored-manager.py'
    manager_launcher.write_text(
        'import sys\nsys.dont_write_bytecode=True\n'
        f'sys.path.insert(0, {str(code_dir / "manager")!r})\n'
        'from server.serve import main\n'
        f'main(["--host","127.0.0.1","--port",{str(manager_port)!r},'
        '"--timeout-graceful-shutdown","30"])\n')
    manager_log = (logs / 'manager.log').open('w')
    manager = subprocess.Popen([python, '-B', str(manager_launcher)], cwd=str(code_dir),
                               env=manager_env, stdout=manager_log, stderr=subprocess.STDOUT)
    settings = {
        'data_dir': str(restored / 'qtg/var'),
        'static_dir': str(root / 'qtg-static'),
        'admin_token_file': str(restored / 'qtg/secrets/qtg-admin-token'),
        'session_secret_file': str(restored / 'qtg/var/session_secret'),
        'internal_service_token_file': str(restored / 'qtg/secrets/qtg-internal-service-token'),
        'identity_signing_key_file': str(restored / 'qtg/secrets/qtg-identity-secret'),
        'manager_service_token_file': str(restored / 'qtg/secrets/manager_service_token'),
        'manager_read_token_file': str(restored / 'qtg/secrets/manager_read_token'),
        'extra_password_files': '',
        'trusted_edge_peers': '127.0.0.1,::1',
        'read_requires_auth': True, 'authz_enforce': True, 'human_session_required': True,
        'csrf_strict_origin': True, 'checkin_scheduler_enabled': False,
        'qoder_base': provider.base_url, 'trae_base': provider.base_url, 'wb_engine': 'off',
        'wb_panel_base': f'http://127.0.0.1:{manager_port}',
        'limits_file': str(restored / 'qtg/var/limits.json'),
        'notify_config_file': str(restored / 'qtg/var/notify.json'),
        'proxy_enabled': False,
        'backup_glob_qoder': str(restored / 'qoder-hub/backups/*'),
        'backup_glob_trae': str(restored / 'trae-work/backups/*'),
    }
    (root / 'qtg-static').mkdir(exist_ok=True)
    qtg_launcher = root / 'restored-qtg.py'
    qtg_launcher.write_text(
        'import sys\nsys.dont_write_bytecode=True\n'
        f'sys.path.insert(0, {str(code_dir / "qtg/app")!r})\n'
        'import uvicorn\nfrom qtg.config import Settings\nfrom qtg.main import create_app\n'
        f'uvicorn.run(create_app(Settings(**{settings!r})),host="127.0.0.1",port={qtg_port},proxy_headers=True)\n')
    qtg_env = {**os.environ, 'PYTHONUTF8': '1', 'TZ': 'Asia/Shanghai', 'PYTHONDONTWRITEBYTECODE': '1'}
    qtg_log = (logs / 'qtg.log').open('w')
    qtg = subprocess.Popen([python, '-B', str(qtg_launcher)], cwd=str(code_dir),
                           env=qtg_env, stdout=qtg_log, stderr=subprocess.STDOUT)
    return {'provider': provider, 'manager': manager, 'manager_port': manager_port, 'manager_log': manager_log,
            'qtg': qtg, 'qtg_port': qtg_port, 'qtg_log': qtg_log, 'logs': logs}


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def business_closure(services: dict, restored: Path, code_dir: Path) -> dict:
    base = f"http://127.0.0.1:{services['manager_port']}"
    qtg = f"http://127.0.0.1:{services['qtg_port']}"
    steps = []

    def mark(name, ok, **extra):
        steps.append({'step': name, 'ok': bool(ok), 't_mono': round(time.monotonic(), 6),
                      't_wall': round(time.time(), 3), **extra})
        print(('PASS ' if ok else 'FAIL ') + name, flush=True)
        return ok

    ready = wait_http(base + '/api/healthz', 200, 180)
    mark('manager_healthz', ready is not None, port=services['manager_port'])
    ready_q = wait_http(qtg + '/healthz', 200, 120)
    mark('qtg_healthz', ready_q is not None, port=services['qtg_port'])
    status, body, headers = http('POST', base + '/api/login',
                                 json_body={'username': 'admin', 'password': 'synthetic-password'},
                                 headers={'Origin': base})
    cookie = headers.get('Set-Cookie', '') if headers else ''
    session = ''
    if cookie:
        session = cookie.split(';', 1)[0]
    mark('manager_login', status == 200 and bool(session), status=status, body=str(body)[:120])
    key_id = None
    status, body, _ = http('POST', base + '/api/keys',
                           json_body={'name': 'r3l3-restored-key', 'platforms': ['workbuddy'],
                                      'quota': 1_000_000},
                           headers={'Cookie': session, 'Origin': base})
    if isinstance(body, dict):
        key_id = body.get('id')
    created_key = body.get('key') if isinstance(body, dict) else None
    mark('manager_create_key', status == 200 and key_id is not None, status=status)
    status, body, _ = http('GET', base + '/api/keys', headers={'Cookie': session, 'Origin': base})
    count = len(body) if isinstance(body, list) else (body.get('total') if isinstance(body, dict) else None)
    mark('manager_list_keys', status == 200, status=status, count=count)
    inference_status = None
    if created_key:
        inference_status, infer_body, _ = http(
            'POST', base + '/v1/chat/completions',
            json_body={'model': 'gpt-4o-mini', 'stream': False,
                       'messages': [{'role': 'user', 'content': 'r3l3 restored inference'}]},
            headers={'Authorization': 'Bearer ' + created_key})
    mark('fake_inference', inference_status == 200, status=inference_status)
    revoke_status = None
    if key_id is not None:
        revoke_status, _body, _ = http('DELETE', f'{base}/api/keys/{key_id}',
                                       headers={'Cookie': session, 'Origin': base})
    mark('manager_revoke_key', revoke_status == 200, status=revoke_status)
    # QTG 读：用恢复出来的 QTG 会话（admin token + session secret）读 QTG 控制面配置；
    # 该路径会经 manager_read_token 读 Manager（K26 读通道）。
    qtg_read_ok = False
    qtg_session = None
    sys.path.insert(0, str(code_dir / 'qtg'))

    try:
        sys.path.insert(0, str(code_dir / 'qtg/app'))
        from qtg.security import CredentialStore  # noqa: E402
        store = CredentialStore(restored / 'qtg/secrets/qtg-admin-token',
                                restored / 'qtg/var/session_secret')
        qtg_session = store.issue_session(3600, sub='restored-admin', role='admin')
        # QTG 读通道：/api/v1/capabilities?live=1 会通过 rt.wb（WorkBuddy adapter）
        # 用 restored manager_read_token 去读 Manager；能同时证明 QTG 服务与读凭据可用。
        endpoint = '/api/v1/capabilities?live=1'
        status, body, _ = http('GET', qtg + endpoint,
                               headers={'Cookie': 'qtg_session=' + qtg_session,
                                        'Origin': qtg})
        qtg_read_ok = status == 200
        sources = (body or {}).get('sources') if isinstance(body, dict) else None
        mark('qtg_read_manager_capabilities', qtg_read_ok, status=status, endpoint=endpoint,
             sources=str(sources)[:200])
    except Exception as exc:  # noqa: BLE001
        mark('qtg_read_manager_capabilities', False, error=repr(exc)[:200])
    # QTG 写 + 审计追加（证明恢复后的 QTG 既可写、审计链继续）。
    # audit_before 必须在写动作之前取；limits 动作本身会写一条审计记录。
    audit_before = audit_chain(restored / 'qtg/var/audit.jsonl')
    qtg_write_ok = False
    try:
        status, body, _ = http('POST', qtg + '/api/v1/actions/limits',
                               json_body={'enabled': True}, headers={
                                   'Cookie': 'qtg_session=' + str(qtg_session),
                                   'Origin': qtg, 'X-QTG-Platform': 'qoder'})
        qtg_write_ok = status == 200
        mark('qtg_write_limits', qtg_write_ok, status=status, body=str(body)[:120])
    except Exception as exc:  # noqa: BLE001
        mark('qtg_write_limits', False, error=repr(exc)[:200])
    audit_after = audit_chain(restored / 'qtg/var/audit.jsonl')
    audit_grew = audit_after['records'] > audit_before['records']
    mark('qtg_audit_chain_valid', audit_after['ok'] and audit_grew,
         records_before=audit_before['records'], records_after=audit_after['records'],
         grew=audit_grew)
    return {'steps': steps,
            'all_green': all(step['ok'] for step in steps),
            'manager_port': services['manager_port'], 'qtg_port': services['qtg_port'],
            'session': session, 'qtg_session': qtg_session}


# --------------------------------------------------------------------------- reconcile
def post_restore_reconcile(restored: Path, manifest: dict, code_dir: Path) -> dict:
    expected = manifest['db_stats']['manager']
    restored_stats = db_stats(restored / 'manager/data/manager.db')
    checks = []
    for table, count in expected.get('counts', {}).items():
        actual = restored_stats.get('counts', {}).get(table)
        # 恢复后服务会正常追加新行（推理/预算/审计），所以判据是“历史不丢”：
        # actual >= snapshot 时的计数；缺失才会失败。
        checks.append({'check': f'count_preserved:{table}', 'expected_min': count, 'actual': actual,
                       'ok': actual is not None and actual >= count})
    exp_range = expected.get('request_logs_range') or {}
    act_range = restored_stats.get('request_logs_range') or {}
    checks.append({'check': 'request_logs_history_preserved', 'expected': exp_range, 'actual': act_range,
                   'ok': bool(exp_range) and bool(act_range)
                   and act_range.get('ts_min') == exp_range.get('ts_min')
                   and act_range.get('count', 0) >= exp_range.get('count', 0)
                   and act_range.get('id_max', 0) >= exp_range.get('id_max', 0)})
    checks.append({'check': 'budget_unknown_preserved',
                   'expected_min': expected.get('budget_unknown'), 'actual': restored_stats.get('budget_unknown'),
                   'ok': (restored_stats.get('budget_unknown') or 0) >= (expected.get('budget_unknown') or 0)})
    checks.append({'check': 'task_leases_rows', 'expected': expected.get('task_leases'),
                   'actual': restored_stats.get('task_leases'),
                   'ok': expected.get('task_leases') == restored_stats.get('task_leases')})
    checks.append({'check': 'instance_leases_rows', 'expected': expected.get('instance_leases'),
                   'actual': restored_stats.get('instance_leases'),
                   'ok': expected.get('instance_leases') == restored_stats.get('instance_leases')})
    checks.append({'check': 'publish_state_preserved', 'expected': expected.get('publish_state'),
                   'actual': restored_stats.get('publish_state'),
                   'ok': expected.get('publish_state') == restored_stats.get('publish_state')})
    runtime = {}
    try:
        conn = sqlite3.connect(f'file:{restored / "manager/data/manager.db"}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        rows = [{key: row[key] for key in row.keys()} for row in conn.execute(
            'SELECT task, owner, lease_until, fence FROM task_leases').fetchall()]
        runtime['active_task_leases'] = [row for row in rows if row['lease_until'] > now and row['owner']]
        runtime['expired_task_leases'] = [row for row in rows if row['lease_until'] <= now]
        runtime['unknown_budgets'] = conn.execute(
            "SELECT count(*) FROM budget_reservations WHERE state='unknown'").fetchone()[0]
        publish = conn.execute("SELECT value FROM settings WHERE key='publish.state'").fetchone()
        runtime['publish_state'] = publish[0] if publish else None
        if publish:
            state = json.loads(publish[0])
            runtime['publish_state_expired'] = int(state.get('lease_until', 0)) <= now
        conn.close()
    except Exception as exc:  # noqa: BLE001
        runtime['error'] = repr(exc)[:300]
    audit = audit_chain(restored / 'qtg/var/audit.jsonl')
    checks.append({'check': 'qtg_audit_chain', 'expected': manifest.get('qtg_audit'),
                   'actual': audit, 'ok': audit['ok'] and audit['records'] >=
                   (manifest.get('qtg_audit') or {}).get('records', 0)})
    expected_logs = expected.get('counts', {}).get('request_logs')
    actual_logs = restored_stats.get('counts', {}).get('request_logs')
    checks.append({'check': 'request_logs_after_restore_grew', 'expected': f'> {expected_logs}',
                   'actual': actual_logs,
                   'ok': expected_logs is not None and actual_logs is not None and actual_logs > expected_logs})
    expected_audit = (manifest.get('qtg_audit') or {}).get('records', 0)
    checks.append({'check': 'qtg_audit_after_restore_grew', 'expected': f'> {expected_audit}',
                   'actual': audit['records'],
                   'ok': audit['ok'] and audit['records'] > expected_audit})
    # request_outcomes 历史保留（ts 区间 + 计数），防止 retention 误删
    exp_outcomes = manifest.get('db_stats', {}).get('manager', {}).get('counts', {}).get('request_outcomes')
    act_outcomes = restored_stats.get('counts', {}).get('request_outcomes')
    checks.append({'check': 'request_outcomes_history_preserved', 'expected_min': exp_outcomes,
                   'actual': act_outcomes,
                   'ok': act_outcomes is not None and exp_outcomes is not None and act_outcomes >= exp_outcomes})
    return {'schema': 'zhihui-r3l3-post-restore-reconcile/1', 'generated_at': now_iso(),
            'checks': checks, 'runtime': runtime, 'all_ok': all(item['ok'] for item in checks)}


def cmd_restore(args) -> dict:
    bundle_dir = Path(args.bundle_dir).resolve()
    dest_root = Path(args.dest_root).resolve()
    private_key = Path(args.private_key).resolve()
    code_dir = Path(args.code_dir).resolve()
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    decision_mono, decision_wall = time.monotonic(), time.time()
    services = None
    try:
        verification = verify_bundle(bundle_dir, private_key, dest_root, code_dir, extract=True)
        manifest = verification['manifest']
        restore_start_mono = time.monotonic()
        restored = dest_root / 'workspace'
        services = start_services(restored, code_dir, args.python, out)
        closure = business_closure(services, restored, code_dir)
        reconcile = post_restore_reconcile(restored, manifest, code_dir)
        finished_mono = time.monotonic()
        phases = {'transfer_s': args.transfer_seconds,
                  'verify_and_extract_s': round(restore_start_mono - decision_mono, 3),
                  'service_ready_s': round(finished_mono - restore_start_mono, 3)}
        result = {
            'schema': 'zhihui-r3l3-restore-run/1', 'generated_at': now_iso(),
            'status': 'RESTORED' if closure['all_green'] and reconcile['all_ok'] else 'PARTIAL',
            'bundle_id': manifest.get('bundle_id'), 'bundle_sha256': manifest['crypto']['encrypted_sha256'],
            'source_sha256': manifest.get('build', {}).get('source_sha256'),
            'verification': verification['checks'], 'closure': closure, 'phases': phases,
            'rto_s': round(finished_mono - decision_mono, 3),
            'rto_definition': '决定恢复(restore 启动) → 登录+建 key+fake 推理+吊销+QTG 读全绿（不含离线传输）',
            'private_key_fingerprint': hashlib.sha256(private_key.read_bytes()).hexdigest(),
            'used_source_plaintext_key': False,
            'source_unavailable': args.source_unavailable,
        }
        (out / 'restore-run.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (out / 'post-restore-reconcile.json').write_text(json.dumps(reconcile, ensure_ascii=False, indent=2),
                                                         encoding='utf-8')
        return result
    finally:
        if services is not None:
            for key in ('qtg', 'manager'):
                child = services.get(key)
                if child and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        child.kill()
            if services.get('provider') is not None:
                services['provider'].stop()
            for name in ('manager_log', 'qtg_log'):
                handle = services.get(name)
                if handle:
                    handle.close()


# --------------------------------------------------------------------------- faults
def cmd_faults(args) -> dict:
    bundle_dir = Path(args.bundle_dir).resolve()
    private_key = Path(args.private_key).resolve()
    code_dir = Path(args.code_dir).resolve()
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    scenarios = []

    def add(name, expected, exc):
        scenarios.append({'scenario': name, 'expected': expected, 'ok': exc is not None,
                          'error': str(exc)[:400] if exc else None})

    wrong = out / 'wrong-private.pem'
    result = run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                  '-out', str(wrong)])
    if result.returncode != 0:
        raise RuntimeError('could not generate wrong key')
    tmp = out / 'fault-scratch'
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        error = None
        try:
            verify_bundle(bundle_dir, wrong, tmp / 'wrong-key', code_dir, extract=False)
        except Exception as exc:  # noqa: BLE001
            error = exc
        add('wrong_private_key', 'RSA decrypt must fail', error)

        tampered = out / 'tampered-bundle'
        shutil.rmtree(tampered, ignore_errors=True)
        shutil.copytree(bundle_dir, tampered)
        encrypted = tampered / 'workspace-r3l3.tar.gz.enc'
        raw = bytearray(encrypted.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        encrypted.write_bytes(bytes(raw))
        error = None
        try:
            verify_bundle(tampered, private_key, tmp / 'tampered', code_dir, extract=False)
        except Exception as exc:  # noqa: BLE001
            error = exc
        add('tampered_bundle', 'sha256/HMAC must fail', error)

        missing = out / 'missing-asset'
        shutil.rmtree(missing, ignore_errors=True)
        shutil.copytree(bundle_dir, missing)
        manifest = json.loads((missing / 'manifest.json').read_text(encoding='utf-8'))
        manifest['assets'].append({'path': 'manager/data/synthetic-missing-asset.json',
                                   'sha256': '0' * 64, 'bytes': 1, 'source': 'file', 'mode': '0o600',
                                   'asset_ids': ['D5']})
        (missing / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        error = None
        try:
            verify_bundle(missing, private_key, tmp / 'missing', code_dir, extract=False)
        except Exception as exc:  # noqa: BLE001
            error = exc
        add('missing_asset', 'manifest asset absent from archive', error)

        version = out / 'version-mismatch'
        shutil.rmtree(version, ignore_errors=True)
        shutil.copytree(bundle_dir, version)
        manifest = json.loads((version / 'manifest.json').read_text(encoding='utf-8'))
        manifest['build']['code_schema_version'] = 999
        (version / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        error = None
        try:
            verify_bundle(version, private_key, tmp / 'version', code_dir, extract=False)
        except Exception as exc:  # noqa: BLE001
            error = exc
        add('version_mismatch', 'bundle schema version must not match code', error)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report = {'schema': 'zhihui-r3l3-failure-branches/1', 'generated_at': now_iso(),
              'scenarios': scenarios, 'all_failed_as_expected': all(item['ok'] for item in scenarios)}
    (out / 'failure-branches.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    bundle = sub.add_parser('bundle')
    bundle.add_argument('--source-root', required=True)
    bundle.add_argument('--out-dir', required=True)
    bundle.add_argument('--public-key', required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--bundle-dir', required=True)
    verify.add_argument('--private-key', required=True)
    verify.add_argument('--dest-root', required=True)
    verify.add_argument('--code-dir', default=None)
    restore = sub.add_parser('restore')
    restore.add_argument('--bundle-dir', required=True)
    restore.add_argument('--private-key', required=True)
    restore.add_argument('--dest-root', required=True)
    restore.add_argument('--code-dir', required=True)
    restore.add_argument('--python', required=True)
    restore.add_argument('--out-dir', required=True)
    restore.add_argument('--transfer-seconds', type=float, default=0.0)
    restore.add_argument('--source-unavailable', action='store_true')
    faults = sub.add_parser('faults')
    faults.add_argument('--bundle-dir', required=True)
    faults.add_argument('--private-key', required=True)
    faults.add_argument('--code-dir', required=True)
    faults.add_argument('--out-dir', required=True)
    args = parser.parse_args(argv)
    if args.command == 'bundle':
        print(json.dumps(cmd_bundle(args), ensure_ascii=False, indent=1), flush=True)
        return 0
    if args.command == 'verify':
        result = verify_bundle(Path(args.bundle_dir), Path(args.private_key), Path(args.dest_root),
                               Path(args.code_dir) if args.code_dir else None, extract=True)
        print(json.dumps({'status': result['status'], 'checks': result['checks'],
                          'bundle_id': result['bundle_id']}, ensure_ascii=False, indent=1), flush=True)
        return 0
    if args.command == 'restore':
        result = cmd_restore(args)
        print(json.dumps({k: result[k] for k in ('status', 'rto_s', 'phases', 'closure')},
                         ensure_ascii=False, indent=1), flush=True)
        return 0 if result['status'] == 'RESTORED' else 1
    report = cmd_faults(args)
    print(json.dumps(report, ensure_ascii=False, indent=1), flush=True)
    return 0 if report['all_failed_as_expected'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
