"""R3-L3：生成“合成源资产树”与时间基 RPO 标记写入器（仅本地 VM / 授权云环境）。

这是 R3-L3（完整恢复）的源侧准备工具，不碰生产、不读真实凭据：
  * ``prepare``：在给定目录里生成一份覆盖 `recovery/ASSET-INVENTORY.md` 39 行矩阵的
    **合成**资产树（Manager/QTG DB、三平台账号与状态、生效 nginx/unit/cron、
    代码与静态资源、必要秘密占位、主机/容器/运行时元数据），并用 Manager/QTG
    自己的代码建库/建表/写哈希链审计，保证恢复侧能用真实服务代码启动与对账。
  * ``write-marks``：持续写入器。每 200 ms 往 ``rpo_marks`` 写一行带**单调时钟**
    的标记（同时每秒往 request_logs 写一行），供 bundle 侧计算“最后可恢复提交时间
    vs 故障时刻”的时间基 RPO；被 SIGKILL 时最后一条已提交标记就是可恢复点。

不在脚本里写入任何真实密钥：所有 token/secret 都是运行时随机生成的合成值。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import signal
import socket
import sqlite3
import sys
import time
import uuid
from pathlib import Path

LAB = Path('/home/gateway-lab/lab')


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


def insert_minimal(conn: sqlite3.Connection, table: str, **overrides) -> None:
    """按表结构插入一行：只填 NOT NULL 且无默认值的列，其余用给定 overrides。"""
    columns = conn.execute(f'PRAGMA table_info({table})').fetchall()
    if not columns:
        raise RuntimeError(f'table {table} does not exist')
    names, values = [], []
    for row in columns:
        name = row[1]
        notnull = bool(row[3])
        default = row[4]
        pk = bool(row[5])
        if name in overrides:
            names.append(name)
            values.append(overrides[name])
            continue
        if not notnull or default is not None:
            continue
        if pk and (row[2] or '').upper() == 'INTEGER':
            continue  # AUTOINCREMENT 主键
        kind = (row[2] or '').upper()
        names.append(name)
        values.append(0 if 'INT' in kind else (0.0 if any(k in kind for k in ('REAL', 'FLOA', 'DOUB')) else ''))
    placeholders = ','.join('?' for _ in names)
    conn.execute(f'INSERT INTO {table} ({",".join(names)}) VALUES ({placeholders})', values)


def prepare(root: Path, code_dir: Path, source_sha: str) -> dict:
    if socket.gethostname() != 'zhihui-local-lab':
        raise SystemExit('prepare is restricted to the disposable local VM (synthetic data only)')
    root = root.resolve()
    if root == Path('/opt') or str(root).startswith('/opt'):
        raise SystemExit('never write under /opt')
    if root.exists():
        raise SystemExit(f'source root already exists: {root}')
    root.mkdir(parents=True)
    manifest = {'schema': 'zhihui-r3l3-source/1', 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'source_sha256': source_sha, 'synthetic': True, 'root': str(root), 'assets': {}}

    # ---- 目录骨架（镜像生产布局，但全部落在隔离根内） ----
    layout = [
        'manager/data', 'manager/secrets', 'qtg/var', 'qtg/secrets', 'qtg/app',
        'workbuddy2api/data', 'workbuddy2api/auths',
        'qoder-hub/accounts', 'qoder-hub/secrets',
        'trae-work/data/data', 'trae-work/secrets',
        'nginx/sites-enabled', 'nginx/sites-available', 'nginx/conf.d', 'nginx/snippets', 'nginx/secrets',
        'systemd/units', 'systemd/dropins', 'cron',
        'release/server', 'release/web/out', 'release/qtg',
        'secrets', 'meta',
    ]
    for rel in layout:
        (root / rel).mkdir(parents=True, exist_ok=True)

    # ---- Manager DB：用快照代码建库（真实 schema） ----
    sys.path.insert(0, str(code_dir / 'manager'))
    os.environ['WB_DB'] = str(root / 'manager/data/manager.db')
    os.environ['WB_DATA_DIR'] = str(root / 'manager/data')
    os.environ['WB_USERS_FILE'] = str(root / 'manager/data/users.json')
    from server import config, db, keysvc, security, tasklease, tokensvc  # noqa: E402
    from server import budget as budget_module  # noqa: E402
    db.connect()
    budget_module.initialize()
    admin_secret = secrets.token_hex(32)
    security.save_users({'secret': admin_secret, 'api_keys': [], 'users': [
        {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('synthetic-password')},
        {'username': 'viewer', 'role': 'viewer', 'pwd_hash': security.make_hash('synthetic-password')},
    ]})
    # QTG 需要的 Manager 服务令牌（合成；恢复后仍由 QTG 读取）
    read_token = tokensvc.create_token('qtg-read', 'read')['token']
    keys_token = tokensvc.create_token('qtg-keys', 'keys')['token']
    legacy_key = keysvc.create_key('legacy-source-key', quota=1000, platforms=['workbuddy'])
    conn = db.connect()
    now = int(time.time())
    # 历史 request_logs / request_outcomes / usage_daily（提供非空时间范围）
    for index in range(120):
        insert_minimal(conn, 'request_logs', ts=now - 3600 + index * 20, key_id=legacy_key['id'],
                       model='gpt-4o-mini', mapped_model='gpt-4o-mini', status=200,
                       prompt_tokens=10 + index, completion_tokens=5, latency_ms=40,
                       stream=0, realm='cn', ip='127.0.0.1', ua='r3l3-synthetic')
    for index in range(120):
        request_id = uuid.uuid4().hex
        insert_minimal(conn, 'request_outcomes', request_id=request_id,
                       ts=now - 3600 + index * 20, endpoint='/v1/chat/completions', method='POST',
                       protocol='openai', http_status=200, terminal_state='completed',
                       latency_ms=40)
    for day_offset in range(3):
        day = (dt.date.today() - dt.timedelta(days=day_offset)).isoformat()
        insert_minimal(conn, 'usage_daily', day=day, key_id=legacy_key['id'], model='gpt-4o-mini',
                       requests=40, prompt_tokens=800, completion_tokens=400, credit=0.5, realm='cn')
    for index in range(20):
        insert_minimal(conn, 'audit_logs', ts=now - 2000 + index * 30, action='r3l3-seed',
                       actor='admin', detail='{}')
    for index in range(10):
        insert_minimal(conn, 'task_logs', ts=now - 1800 + index * 60, task='checkin',
                       status='ok', detail='{}', dedup_key='seed-%d' % index)
    for index in range(8):
        insert_minimal(conn, 'checkin_logs', ts=now - 1500 + index * 60, uid='synthetic-uid-%d' % index,
                       result='ok', detail='{}', dedup_key='seed-ck-%d' % index)
    # budget_reservations：一条 reserved、一条 unknown（恢复后对账用）
    insert_minimal(conn, 'budget_reservations', request_id=uuid.uuid4().hex, key_id=legacy_key['id'],
                   created_at=now - 60, state='reserved', reserved_tokens=100, owner='synthetic-old-owner',
                   lease_until=now + 600, fence=1, updated_at=now)
    insert_minimal(conn, 'budget_reservations', request_id=uuid.uuid4().hex, key_id=legacy_key['id'],
                   created_at=now - 120, state='unknown', reserved_tokens=50, owner='synthetic-old-owner',
                   lease_until=now - 60, fence=2, updated_at=now - 120)
    # stale publish.state（过期 → 恢复后对账应识别为可清理）
    conn.execute("INSERT INTO settings(key, value) VALUES('publish.state', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps({'state': 'publishing', 'owner': 'synthetic-stale-release',
                              'started_at': now - 900, 'lease_until': now - 300}),))
    conn.execute("INSERT INTO settings(key, value) VALUES('github_token', 'synthetic-github-token') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.execute("INSERT INTO settings(key, value) VALUES('synthetic.marker', 'r3l3') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.commit()
    # task_leases：一条活跃、一条已过期（真实 tasklease 算法）
    tasklease.reset_identity()
    tasklease.claim('daily-active', 600, owner='zhihui-local-lab:' + 'a' * 32)
    tasklease.claim('daily-expired', 5, owner='zhihui-local-lab:' + 'b' * 32, now=now - 1000)
    # 明确落一条已过期的 task lease（对账要同时看到“活跃/过期”两种 owner/expiry）
    conn.execute("INSERT OR REPLACE INTO task_leases(task, owner, lease_until, fence, updated_at, detail) "
                 "VALUES('daily-expired', ?, ?, 7, ?, 'synthetic-expired')",
                 ('zhihui-local-lab:' + 'b' * 32, now - 120, now - 600))
    conn.commit()
    # instance_leases（R3 v8 DDL；R3 包未冻结前先合成该表以演练对账）
    conn.executescript('''
CREATE TABLE IF NOT EXISTS instance_leases (
  instance_id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0,
  fence INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0, source_sha TEXT NOT NULL DEFAULT '',
  build_id TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '', pid INTEGER NOT NULL DEFAULT 0,
  start_ticks TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'candidate', phase TEXT NOT NULL DEFAULT '',
  drain_snapshot TEXT NOT NULL DEFAULT '{}', detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_instance_lease_until ON instance_leases(lease_until);
''')
    conn.execute("INSERT OR REPLACE INTO instance_leases VALUES"
                 "('inst-serving','zhihui-local-lab:bbb',? ,3,? ,? ,'build-r3l3','zhihui-local-lab',1234,'1','serving','steady','{}','')",
                 (now + 600, now, source_sha))
    conn.execute("INSERT OR REPLACE INTO instance_leases VALUES"
                 "('inst-expired','zhihui-local-lab:ccc',? ,1,? ,? ,'build-r3l3','zhihui-local-lab',0,'0','candidate','expired','{}','')",
                 (now - 300, now - 900, source_sha))
    # RPO 标记表：写手持续追加；manifest 记录最后可恢复提交
    conn.executescript('''
CREATE TABLE IF NOT EXISTS rpo_marks (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_mono REAL NOT NULL,
  ts_wall REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '');
''')
    conn.commit()
    # 种子阶段写入 5 个初始标记，确保快照点一定有可恢复提交
    for index in range(5):
        conn.execute('INSERT INTO rpo_marks(ts_mono, ts_wall, note) VALUES(?,?,?)',
                     (time.monotonic() - 1.0 + index * 0.2, time.time() - 1.0 + index * 0.2, 'seed'))
    conn.commit()

    # ---- QTG DB / 状态 / 审计哈希链 ----
    sys.path.insert(0, str(code_dir / 'qtg'))
    sys.path.insert(0, str(code_dir / 'qtg/app'))
    from qtg.store import Store  # noqa: E402
    qtg_db = root / 'qtg/var/qtg.db'
    store = Store(qtg_db)
    qconn = sqlite3.connect(qtg_db)
    for index in range(3):
        insert_minimal(qconn, 'qtg_keys', key_hash=sha256_bytes(b'qtg-key-%d' % index),
                       name='synthetic-qtg-key-%d' % index, prefix='sk-qtg-syn%d' % index,
                       created_at=now - 800 + index, revoked=0)
    qconn.commit()
    qconn.close()
    from qtg.audit import AuditLog  # noqa: E402
    audit = AuditLog(root / 'qtg/var/audit.jsonl')
    for index in range(25):
        audit.append('r3l3-seed', actor='admin:synthetic', target='seed-%d' % index,
                     result='ok', request_id='seed-%d' % index)
    write_private(root / 'qtg/var/session_secret', secrets.token_urlsafe(48))
    (root / 'qtg/var/qoder_engine').write_text('qoder2\n')
    (root / 'qtg/var/wb_engine').write_text('off\n')
    (root / 'qtg/var/trae_engine').write_text('off\n')
    (root / 'qtg/var/random_checkin.json').write_text(json.dumps(
        {'last_day': dt.date.today().isoformat(), 'done': ['synthetic-uid-0']}))
    (root / 'qtg/var/compat-plans.json').write_text(json.dumps(
        {'plans': [{'id': 'synthetic', 'enabled': False, 'daily': {'checkin': {'enabled': False}}}]}))
    internal_token = secrets.token_urlsafe(48)
    identity_secret = secrets.token_urlsafe(48)
    qtg_admin_token = secrets.token_urlsafe(48)
    write_private(root / 'qtg/secrets/qtg-admin-token', qtg_admin_token)
    write_private(root / 'qtg/secrets/qtg-internal-service-token', internal_token)
    write_private(root / 'qtg/secrets/qtg-identity-secret', identity_secret)
    write_private(root / 'qtg/secrets/manager_service_token', keys_token)
    write_private(root / 'qtg/secrets/manager_read_token', read_token)
    write_private(root / 'qtg/secrets/release-signing.pub', 'synthetic-release-public-key\n')
    # QTG 应用指纹（R5）：代码文件指纹，不复制整棵 QTG app
    qtg_files = sorted(p for p in (code_dir / 'qtg/app/qtg').rglob('*.py'))
    meta_dir = root / 'meta'
    (meta_dir / 'qtg-release-fingerprints.json').write_text(json.dumps({
        'schema': 'zhihui-release-fingerprints/1', 'root': 'qtg/app/qtg',
        'files': {p.relative_to(code_dir).as_posix(): sha256_bytes(p.read_bytes()) for p in qtg_files},
    }, ensure_ascii=False, indent=2))

    # ---- 三平台账号与状态 ----
    for index in range(5):
        uid = 'synthetic-wb-%d' % index
        (root / 'workbuddy2api/auths' / f'workbuddy-{uid}.json').write_text(json.dumps({
            'account': {'uid': uid, 'nickname': '合成账号%d' % index, 'phone': '1380000000%d' % index},
            'auth': {'uid': uid, 'accessToken': secrets.token_urlsafe(24),
                     'refreshToken': secrets.token_urlsafe(16), 'expiresAt': now + 86400,
                     'domain': '', 'realm': 'cn'},
        }, ensure_ascii=False, indent=2))
    write_private(root / 'workbuddy2api/config.json', json.dumps(
        {'synthetic': True, 'api_key': 'synthetic-upstream', 'base_url': 'http://127.0.0.1:1/v1'}))
    (root / 'workbuddy2api/data/state.json').write_text(json.dumps(
        {'connected': False, 'synthetic': True, 'accounts': 5}))
    (root / 'workbuddy2api/data/session-binds.json').write_text(json.dumps(
        {'binds': [{'uid': 'synthetic-wb-0', 'session': 'synthetic-session-0'}]}))
    for index in range(3):
        uid = 'synthetic-qoder-%d' % index
        (root / 'qoder-hub/accounts' / f'{uid}.json').write_text(json.dumps({
            'uid': uid, 'cooldownUntil': now + 300 * index, 'lastError': None,
            'session': 'synthetic-qoder-session-%d' % index,
        }, ensure_ascii=False, indent=2))
    write_private(root / 'qoder-hub/secrets/qoder-panel-password', secrets.token_urlsafe(24))
    trae_db = root / 'trae-work/data/data/aiwork.sqlite'
    tconn = sqlite3.connect(trae_db)
    tconn.executescript('CREATE TABLE IF NOT EXISTS account_state (uid TEXT PRIMARY KEY, '
                        'cooldown_until INTEGER NOT NULL DEFAULT 0, last_error TEXT);')
    for index in range(2):
        tconn.execute('INSERT OR REPLACE INTO account_state VALUES(?,?,?)',
                      ('synthetic-trae-%d' % index, now + index * 60, None))
    tconn.commit()
    tconn.close()
    write_private(root / 'trae-work/secrets/admin_token', secrets.token_urlsafe(32))

    # ---- 生效 nginx / systemd / cron ----
    site = (root / 'nginx/sites-enabled/gateway.example.com')
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text('# synthetic effective site (R3-L3)\n'
                    'server { listen 443 ssl; server_name gateway.example.com;\n'
                    '  include /etc/nginx/snippets/sec-headers.conf;\n'
                    '  location /api/ { proxy_pass http://127.0.0.1:7864; }\n'
                    '  location /qtg/ { proxy_pass http://127.0.0.1:8795; }\n'
                    '}\n')
    (root / 'nginx/sites-available/gateway.example.com').write_text(
        '# synthetic older variant (sites-available)\nserver { listen 443 ssl; }\n')
    (root / 'nginx/conf.d/zh-threeway-map.conf').write_text(
        'include /etc/nginx/secrets/qtg_bearer.conf;\nmap $http_x_qtg_platform $zh_control_backend { default http://127.0.0.1:8795; }\n')
    (root / 'nginx/conf.d/workbuddy-audit.conf').write_text('access_log /var/log/nginx/zh-workbuddy-audit.log;\n')
    write_private(root / 'nginx/secrets/qtg_bearer.conf', 'proxy_set_header X-QTG-Internal-Auth "synthetic";\n')
    (root / 'nginx/snippets/sec-headers.conf').write_text(
        'add_header X-Content-Type-Options nosniff always;\nadd_header Referrer-Policy no-referrer always;\n')
    (root / 'nginx/nginx.conf').write_text('events {}\nhttp { include /etc/nginx/conf.d/*.conf; }\n')
    units = {
        'workbuddy-web.service': '[Unit]\nDescription=synthetic Manager\n[Service]\nExecStart=/opt/workbuddy-manager/venv/bin/python -m server.serve\n',
        'qtg.service': '[Unit]\nDescription=synthetic QTG\n[Service]\nExecStart=/opt/qtg/venv/bin/uvicorn qtg.main:app\n',
        'qoder2-engine.service': '[Unit]\nDescription=synthetic qoder engine\n[Service]\nExecStart=/opt/qoder2/bin/run\n',
        'wb-gateway-alert.timer': '[Unit]\nDescription=synthetic alert timer\n[Timer]\nOnUnitActiveSec=1m\n',
        'healthcheck.timer': '[Unit]\nDescription=synthetic health timer\n[Timer]\nOnUnitActiveSec=5m\n',
    }
    for name, body in units.items():
        (root / 'systemd/units' / name).write_text(body)
    for name in ('upstream-timeout.conf', 'zz-body-ceiling.conf', 'zz-gateway-concurrency.conf', 'zz-gateway-rate.conf'):
        (root / 'systemd/dropins' / name).write_text('[Service]\nEnvironment=SYNTHETIC=1\n')
    for name, body in {
        'qtg-engine': '*/5 * * * * root /opt/qtg/scripts/engine-check.sh\n',
        'gateway-config-backup': '17 3 * * * root /opt/gateway-ops/backup.sh\n',
        'kagong-maintenance': '30 4 * * * root /opt/gateway-ops/maintenance.sh\n',
        'qoder-offsite': '30 3 * * * root /opt/qoder-hub/scripts/offsite.sh\n',
    }.items():
        (root / 'cron' / name).write_text(body)
    write_private(root / 'cron/wb2api-backup-push.sh', '#!/bin/bash\nexit 0\n')
    write_private(root / 'manager/.local-config', 'WB_SYNTHETIC=1\n')

    # ---- 代码与静态资源 ----
    server_files = sorted(p for p in (code_dir / 'manager/server').rglob('*.py'))
    for path in server_files:
        target = root / 'release/server' / path.relative_to(code_dir / 'manager/server')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    web_out = code_dir / 'src/three-platform/web/out'
    static_count = 0
    if web_out.is_dir():
        for path in sorted(web_out.rglob('*')):
            if path.is_file():
                target = root / 'release/web/out' / path.relative_to(web_out)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                static_count += 1
    deploy_dir = code_dir / 'manager/deploy'
    if deploy_dir.is_dir():
        for path in sorted(deploy_dir.rglob('*')):
            if path.is_file():
                target = root / 'release/deploy' / path.relative_to(deploy_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
    (root / 'release/BUILD_INFO.json').write_text(json.dumps(
        {'source_sha256': source_sha, 'synthetic': True, 'static_files': static_count}))
    # R4 源码树指纹（只指纹不落源码）
    three = code_dir / 'src/three-platform'
    source_files = sorted(p for p in three.rglob('*.py')) + sorted(p for p in three.rglob('*.tsx')) \
        if three.is_dir() else []
    (meta_dir / 'source-tree-fingerprints.json').write_text(json.dumps({
        'schema': 'zhihui-source-fingerprints/1',
        'files': {p.relative_to(code_dir).as_posix(): sha256_bytes(p.read_bytes()) for p in source_files},
    }, ensure_ascii=False, indent=2))

    # ---- 容器/主机/运行时元数据 ----
    baseline = {}
    baseline_path = code_dir / 'baseline/baseline.json'
    if baseline_path.is_file():
        baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    containers = baseline.get('containers') or baseline.get('container_images') or {}
    (meta_dir / 'container-images.json').write_text(json.dumps({
        'schema': 'zhihui-container-digests/1',
        'images': containers or {
            'workbuddy2api': {'image': 'workbuddy2api-upgrade:20261022.4', 'digest': 'sha256:' + sha256_bytes(b'workbuddy2api')},
            'qoder-proxy': {'image': 'qoder-proxy', 'digest': 'sha256:' + sha256_bytes(b'qoder-proxy')},
            'aiwork-server': {'image': 'aiwork-server', 'digest': 'sha256:' + sha256_bytes(b'aiwork-server')},
        },
        'note': 'synthetic hosts only; production images are not pulled or copied',
    }, ensure_ascii=False, indent=2))
    (meta_dir / 'host.json').write_text(json.dumps({
        'hostname': 'r3l3-synthetic-source', 'os': 'ubuntu-24.04', 'mem_mb': 3723, 'cpu': 4,
        'disk_root_gb': 59, 'synthetic': True,
    }))
    (meta_dir / 'python-runtime.json').write_text(json.dumps({
        'python': sys.version, 'synthetic': True,
    }))
    (meta_dir / 'build.json').write_text(json.dumps({
        'source_sha256': source_sha, 'code_dir': str(code_dir),
        'code_schema_version': _code_schema_version(code_dir),
    }, ensure_ascii=False, indent=2))

    # ---- 顶层秘密（合成） ----
    write_private(root / 'secrets/manager_read_token', read_token)
    write_private(root / 'secrets/manager_service_token', keys_token)
    write_private(root / 'secrets/manager_admin.secret', admin_secret)
    write_private(root / 'secrets/qtg-internal-service-token', internal_token)
    write_private(root / 'secrets/qtg-identity-secret', identity_secret)
    write_private(root / 'secrets/qtg-admin-token', qtg_admin_token)
    write_private(root / 'secrets/synthetic-upstream-token', 'synthetic-upstream')
    write_private(root / 'secrets/README.txt', 'synthetic secrets only; no production credential is copied\n')

    # 关闭 DB 连接，避免把 -wal/-shm 带进快照源
    db._conn.close() if getattr(db, '_conn', None) is not None else None
    db._conn = None
    # 清理 SQLite 伴生文件（由 bundle 用 backup API 生成一致快照）
    for suffix in ('-wal', '-shm'):
        candidate = root / f'manager/data/manager.db{suffix}'
        if candidate.exists():
            candidate.unlink()

    manifest['assets']['manager_db'] = 'manager/data/manager.db'
    manifest['assets']['qtg_db'] = 'qtg/var/qtg.db'
    manifest['assets']['qtg_audit'] = 'qtg/var/audit.jsonl'
    manifest['tokens'] = {'manager_read_sha256': sha256_bytes(read_token.encode()),
                          'manager_service_sha256': sha256_bytes(keys_token.encode())}
    (root / 'meta/source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def _code_schema_version(code_dir: Path) -> int | str:
    migrations = code_dir / 'manager/server/migrations.py'
    if not migrations.is_file():
        return 'unknown'
    text = migrations.read_text(encoding='utf-8', errors='replace')
    import re
    match = re.search(r'CODE_SCHEMA_VERSION\s*=\s*(\d+)', text)
    return int(match.group(1)) if match else 'unknown'


def write_marks(db_path: Path, stop_file: Path, log_path: Path, interval: float, log_every: float) -> int:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    log = log_path.open('a', encoding='utf-8')
    seq_start = conn.execute('SELECT COALESCE(MAX(seq),0) FROM rpo_marks').fetchone()[0]
    last_log = 0.0
    wrote = 0
    print(json.dumps({'event': 'writer_started', 'ts_mono': round(time.monotonic(), 6),
                      'ts_wall': round(time.time(), 6), 'seq_start': seq_start}), flush=True)
    try:
        while not stop_file.exists():
            mono = time.monotonic()
            wall = time.time()
            cursor = conn.execute('INSERT INTO rpo_marks(ts_mono, ts_wall, note) VALUES(?,?,?)',
                                  (mono, wall, 'writer'))
            conn.commit()
            wrote += 1
            row = {'event': 'commit', 'seq': cursor.lastrowid, 'ts_mono': round(mono, 6),
                   'ts_wall': round(wall, 6), 'table': 'rpo_marks'}
            log.write(json.dumps(row) + '\n')
            log.flush()
            if wall - last_log >= log_every:
                conn.execute('INSERT INTO request_logs(ts,key_id,model,mapped_model,status,'
                             'prompt_tokens,completion_tokens,latency_ms,stream,realm,ip,ua) '
                             "VALUES(?, (SELECT id FROM api_keys LIMIT 1), 'gpt-4o-mini', 'gpt-4o-mini', "
                             "200, 8, 4, 30, 0, 'cn', '127.0.0.1', 'r3l3-writer')", (int(wall),))
                conn.commit()
                last_log = wall
            time.sleep(interval)
        print(json.dumps({'event': 'writer_stopped', 'ts_mono': round(time.monotonic(), 6),
                          'ts_wall': round(time.time(), 6), 'writes': wrote}), flush=True)
        return 0
    finally:
        log.close()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--root', required=True)
    prep.add_argument('--code-dir', required=True)
    prep.add_argument('--source-sha', required=True)
    wm = sub.add_parser('write-marks')
    wm.add_argument('--db', required=True)
    wm.add_argument('--stop-file', required=True)
    wm.add_argument('--log', required=True)
    wm.add_argument('--interval', type=float, default=0.2)
    wm.add_argument('--log-every', type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        manifest = prepare(Path(args.root), Path(args.code_dir), args.source_sha)
        print('PREPARE_OK ' + json.dumps({'root': manifest['root'],
                                          'manager_db': manifest['assets']['manager_db'],
                                          'tokens': manifest['tokens']}), flush=True)
        return 0
    return write_marks(Path(args.db), Path(args.stop_file), Path(args.log),
                       args.interval, args.log_every)


if __name__ == '__main__':
    raise SystemExit(main())
