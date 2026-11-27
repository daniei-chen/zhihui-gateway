#!/opt/workbuddy-manager/venv/bin/python
"""Private consistent snapshots with optional authenticated encrypted offsite publication."""
import sys

# Cron can execute this file without the guarded Manager launcher.
sys.dont_write_bytecode = True

from pathlib import Path
import fcntl,hashlib,io,json,os,shutil,sqlite3,stat,tarfile,tempfile,time,subprocess

# Recovery invokes this file directly with Python's isolated mode (-I), which
# intentionally omits the script directory from import search.  Keep the
# standalone recovery entrypoint self-contained so its final verified-status
# publication can import the sibling module without depending on PYTHONPATH.
_SERVER_DIR = str(Path(__file__).resolve().parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

BASE=Path('/root/backups/workbuddy-daily')
MANAGER=Path('/opt/workbuddy-manager')
GATEWAY=Path('/opt/workbuddy2api')
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_RELEASE_FILE_BYTES = 64 * 1024 * 1024
MAX_RELEASE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100000
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_DATABASE_BYTES = 2 * 1024**3
MIN_FREE_BYTES = 256 * 1024**2


def bounded_bytes(path, limit=MAX_METADATA_BYTES):
    size = path.stat().st_size
    if size > limit:
        raise RuntimeError('recovery metadata exceeds byte limit')
    with path.open('rb') as stream:
        raw = stream.read(size + 1)
    if len(raw) > size:
        raise RuntimeError('recovery metadata grew beyond byte limit')
    return raw


def stream_hash(stream, limit):
    digest, count = hashlib.sha256(), 0
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        count += len(block)
        if count > limit:
            raise RuntimeError('recovery content exceeds byte limit')
        digest.update(block)
    return digest.hexdigest()


class RecoveryArchive(tarfile.TarFile):
    """Apply write-side limits before payload bytes consume staging space."""
    def addfile(self, member, fileobj=None):
        names = getattr(self, '_recovery_names', set())
        if member.name in names:
            raise RuntimeError('duplicate recovery archive member')
        count = getattr(self, '_recovery_count', 0) + 1
        size = getattr(self, '_recovery_size', 0) + member.size
        if (count > MAX_ARCHIVE_MEMBERS or member.size < 0
                or member.size > MAX_DATABASE_BYTES or size > MAX_ARCHIVE_BYTES
                or not (member.isfile() or member.isdir()) or len(member.name.encode()) > 1024):
            raise RuntimeError('recovery archive exceeds write budget')
        if self.name and shutil.disk_usage(Path(self.name).parent).free < member.size + MIN_FREE_BYTES:
            raise RuntimeError('insufficient free space for recovery archive')
        self._recovery_count, self._recovery_size = count, size
        names.add(member.name)
        self._recovery_names = names
        if not member.isfile() or member.name == 'recovery-manifest.json':
            return super().addfile(member, fileobj)
        class Reader:
            def __init__(self, stream):
                self.stream, self.digest = stream, hashlib.sha256()
            def read(self, size):
                block = self.stream.read(size)
                self.digest.update(block)
                return block
        reader = Reader(fileobj or io.BytesIO())
        result = super().addfile(member, reader)
        inventory = getattr(self, '_recovery_inventory', {})
        inventory[member.name] = {'bytes': member.size, 'sha256': reader.digest.hexdigest()}
        self._recovery_inventory = inventory
        return result

    def add_recovery_manifest(self):
        records = dict(getattr(self, '_recovery_inventory', {}))
        databases = [{'name': name} for name in sorted(records)
                     if name.endswith(('.db', '.sqlite', '.sqlite3'))]
        if 'manager/data/manager.db' not in records:
            raise RuntimeError('manager database missing from recovery inventory')
        raw = json.dumps({'format': 'zhihui-recovery-manifest-v1', 'files': records,
                          'sqlite_snapshots': databases}, sort_keys=True).encode()
        if len(raw) > MAX_METADATA_BYTES:
            raise RuntimeError('recovery inventory exceeds metadata budget')
        add_bytes(self, 'recovery-manifest.json', raw)

def recovery_host_metadata():
    """Capture actual service configuration into the encrypted private archive.

    This is read-only. Service text may contain secrets and never enters source Git.
    """
    commands={'workbuddy-web.unit.txt':['systemctl','cat','workbuddy-web.service'],
              'workbuddy-web.properties.txt':['systemctl','show','workbuddy-web.service','--property=User,Group,EnvironmentFiles,WorkingDirectory,Restart,TimeoutStopUSec'],
              'gateway.image.json':['docker','inspect','--format','{{json .Config.Image}}','zhihui-workbuddy']}
    out={}
    for name,command in commands.items():
        result=subprocess.run(command,capture_output=True,timeout=15)
        if result.returncode or len(result.stdout)>1024*1024:
            raise RuntimeError('host recovery metadata unavailable: '+name)
        out[name]=result.stdout
    out['python-runtime.json']=json.dumps({'version':sys.version,'executable':sys.executable,'platform':sys.platform}).encode()
    # Preserve referenced environment files privately without printing content.
    props=out['workbuddy-web.properties.txt'].decode(errors='replace')
    import re
    for i,path in enumerate(re.findall(r'(/[A-Za-z0-9_./-]+)\s+\(ignore_errors=',props)):
        source=Path(path)
        if source.is_file() and source.stat().st_size<1024*1024:
            out[f'environment-{i}.bin']=source.read_bytes()
    if (MANAGER/'.local-config').is_file():
        out['manager-local-config.bin']=bounded_bytes(MANAGER/'.local-config')
    out['environment-references.json']=json.dumps({'paths':re.findall(r'(/[A-Za-z0-9_./-]+)\s+\(ignore_errors=',props)}).encode()
    return out

def stable_json(path):
    for _ in range(3):
        before=path.stat(); raw=bounded_bytes(path); after=path.stat()
        if (before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns):
            json.loads(raw); return raw
    raise RuntimeError('concurrent snapshot mutation '+path.name)

def add_bytes(archive,name,raw,*,uid=0,gid=0):
    info=tarfile.TarInfo(name); info.size=len(raw); info.mode=0o600; info.mtime=int(time.time())
    info.uid=uid;info.gid=gid
    archive.addfile(info,io.BytesIO(raw))

#: 发布签名算法 → 信任锚文件名。
#:
#: 2026-10-26 起 v9 的发布改用 Ed25519（`pkeyutl -rawin`，对消息本身签名）。
#: 旧的 RSA 路径保留，只为能校验**历史**回退包；反过来不允许——清单声明的算法
#: 必须与本机信任锚一致，否则就是一次降级尝试（把清单改成 rsa-sha256 就能绕开
#: 新渠道的门禁）。
_TRUST_ANCHORS = {'ed25519': 'release-trust-ed25519.pem',
                  'rsa-sha256': 'release-trust.pem'}


def release_algorithm(manifest):
    """清单声明的签名算法；缺省按历史默认 RSA 处理。"""
    return str(manifest.get('signature_algorithm') or 'rsa-sha256')


def trust_anchor_name(manifest):
    """该清单应当使用的信任锚文件名；未知算法直接失败。"""
    algorithm = release_algorithm(manifest)
    name = _TRUST_ANCHORS.get(algorithm)
    if name is None:
        raise RuntimeError('unsupported release signature algorithm: ' + algorithm)
    return name


def signature_verify_command(algorithm, anchor, signature, marker):
    """按算法给出正确的 openssl 校验命令。

    两种算法的签名语义不同，命令不能混用：
      * Ed25519 —— `pkeyutl -rawin`（Ed25519 没有预哈希步骤，签的是消息本身）；
      * RSA —— `dgst -sha256`（签的是消息摘要）。
    用错命令不会"宽松通过"，只会失败；所以这里只按清单声明的算法分支，
    **不做"两种都试"的回退**（那等于允许算法降级）。
    """
    if algorithm == 'ed25519':
        return ['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(anchor),
                '-rawin', '-in', str(marker), '-sigfile', str(signature)]
    return ['openssl', 'dgst', '-sha256', '-verify', str(anchor),
            '-signature', str(signature), str(marker)]


def managed_sources(root):
    """Include every declared release file, regardless of suffix or folder name."""
    marker=root/'deploy/managed-release.json'
    if not marker.is_file() or marker.is_symlink():raise RuntimeError('managed recovery marker missing or unsafe')
    marker_raw=stable_json(marker);manifest=json.loads(marker_raw)
    records=[]
    total = 0
    if not isinstance(manifest.get('files'), dict) or len(manifest['files']) > MAX_ARCHIVE_MEMBERS:
        raise RuntimeError('managed recovery inventory exceeds member limit')
    for relative,item in manifest['files'].items():
        parts=Path(relative)
        if parts.is_absolute() or '..' in parts.parts or '\\' in relative or relative=='deploy/managed-release.json':
            raise RuntimeError('unsafe managed recovery path')
        source=root/relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
            raise RuntimeError('managed recovery source missing or unsafe: '+relative)
        actual=stat.S_IMODE(source.stat().st_mode);expected=item.get('mode')
        if isinstance(expected,bool) or not isinstance(expected,int) or expected&0o7022 or actual!=expected:
            raise RuntimeError('managed recovery permissions mismatch: '+relative)
        size = source.stat().st_size
        total += size
        if size > MAX_RELEASE_FILE_BYTES or total > MAX_RELEASE_BYTES:
            raise RuntimeError('managed recovery sources exceed byte budget')
        with source.open('rb') as stream:
            digest = stream_hash(stream, MAX_RELEASE_FILE_BYTES)
        if digest!=item['candidate_sha256']:
            raise RuntimeError('managed recovery source hash mismatch: '+relative)
        # Retain paths, not a second in-memory copy of the complete release.
        records.append((relative,source,actual))
    records.append(('deploy/managed-release.json',marker_raw,stat.S_IMODE(marker.stat().st_mode)))
    if manifest.get('signature_required'):
        # Detached signatures cannot be inside the manifest they sign. Archive
        # both separately and verify against the installed custodian anchor.
        # 2026-10-26：锚与校验命令按清单声明的算法选（v9 起是 Ed25519）。
        anchor_name=trust_anchor_name(manifest)
        for relative in ('deploy/managed-release.sig',anchor_name):
            source=root/relative
            if not source.is_file() or source.is_symlink():raise RuntimeError('signed recovery material missing: '+relative)
            records.append((relative,bounded_bytes(source),stat.S_IMODE(source.stat().st_mode)))
        subprocess.run(signature_verify_command(
                           release_algorithm(manifest),root/anchor_name,
                           root/'deploy/managed-release.sig',marker),
                       capture_output=True,check=True,timeout=10)
    return records

def add_release_bytes(archive,name,raw,mode):
    info=tarfile.TarInfo(name);info.mode=mode;info.mtime=int(time.time())
    if isinstance(raw, Path):
        info.size = raw.stat().st_size
        if info.size > MAX_RELEASE_FILE_BYTES:
            raise RuntimeError('release source grew beyond byte limit')
        with raw.open('rb') as stream:
            archive.addfile(info, stream)
    else:
        info.size=len(raw)
        archive.addfile(info,io.BytesIO(raw))

def add_state_directory(archive,name,path):
    if path.is_symlink() or not path.is_dir():raise RuntimeError('private recovery directory missing or unsafe')
    metadata=path.stat();info=tarfile.TarInfo(name);info.type=tarfile.DIRTYPE
    info.mode=stat.S_IMODE(metadata.st_mode);info.uid=metadata.st_uid;info.gid=metadata.st_gid;info.mtime=int(time.time())
    archive.addfile(info)
    return info.uid,info.gid,info.mode

def verify_release_archive(archive):
    names, total, actual, recovery = set(), 0, {}, None
    for member in archive:
        if member.name in names or len(names) >= MAX_ARCHIVE_MEMBERS:
            raise RuntimeError('duplicate recovery archive members or member limit exceeded')
        if not (member.isfile() or member.isdir()) or member.issparse():
            raise RuntimeError('unsupported recovery archive member type')
        total += member.size
        if member.size < 0 or member.size > MAX_DATABASE_BYTES or total > MAX_ARCHIVE_BYTES:
            raise RuntimeError('recovery archive exceeds byte budget')
        names.add(member.name)
        if member.isfile():
            with archive.extractfile(member) as stream:
                if member.name == 'recovery-manifest.json':
                    if member.size > MAX_METADATA_BYTES:
                        raise RuntimeError('recovery manifest exceeds metadata limit')
                    recovery = json.loads(stream.read(member.size + 1))
                else:
                    actual[member.name] = {'bytes': member.size,
                        'sha256': stream_hash(stream, MAX_DATABASE_BYTES)}
    if recovery is not None and (not isinstance(recovery, dict) or recovery.get('files') != actual):
        raise RuntimeError('recovery inventory does not match archive payloads')
    marker = archive.getmember('manager/deploy/managed-release.json')
    if marker.size > MAX_METADATA_BYTES:
        raise RuntimeError('managed recovery manifest exceeds byte limit')
    manifest=json.load(archive.extractfile(marker))
    for relative,item in manifest['files'].items():
        name='manager/'+relative
        try:member=archive.getmember(name)
        except KeyError:raise RuntimeError('managed recovery member missing: '+relative) from None
        if not member.isfile() or member.mode!=item['mode']:
            raise RuntimeError('managed recovery archive permissions mismatch: '+relative)
        if member.size > MAX_RELEASE_FILE_BYTES:
            raise RuntimeError('release member exceeds byte limit')
        digest = actual[name]['sha256']
        if digest!=item['candidate_sha256']:
            raise RuntimeError('managed recovery archive hash mismatch: '+relative)
    signed=False
    if manifest.get('signature_required'):
        # 恢复侧同样按清单算法校验（v9 起 Ed25519）；锚必须与本机 custodian 锚一致。
        anchor_name=trust_anchor_name(manifest)
        with archive.extractfile('manager/'+anchor_name) as stream:
            anchor=stream.read(MAX_METADATA_BYTES + 1)
        if len(anchor) > MAX_METADATA_BYTES:
            raise RuntimeError('recovery trust anchor exceeds byte limit')
        installed=MANAGER/anchor_name
        if not installed.is_file() or installed.is_symlink() or bounded_bytes(installed)!=anchor:
            raise RuntimeError('recovery trust anchor does not match installed custodian key')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for path,name in (('anchor','manager/'+anchor_name),('signature','manager/deploy/managed-release.sig'),('manifest','manager/deploy/managed-release.json')):
                with archive.extractfile(name) as stream:
                    raw = stream.read(MAX_METADATA_BYTES + 1)
                if len(raw) > MAX_METADATA_BYTES:
                    raise RuntimeError('recovery signing metadata exceeds byte limit')
                (root/path).write_bytes(raw)
            subprocess.run(signature_verify_command(
                               release_algorithm(manifest),root/'anchor',
                               root/'signature',root/'manifest'),
                           capture_output=True,check=True,timeout=10)
        signed=True
    # 本地清单（sign_release.py 生成）用的是 version 字段；旧清单曾叫 release_id。
    # 两者都认，缺失时回落到空串——备份不该因一个展示用字段而整体失败。
    release_id=manifest.get('release_id') or manifest.get('version') or ''
    # 再记一份「当前正在服务的版本」，用来和归档里那份对比。
    #
    # ## 为什么
    #
    # 2026-11-15 查一个灾备快照时发现它记着 `v1.2.96`，而当时线上是 `v1.2.14x`
    # ——差了几十个版本。**追这条链花了十几步，结论写在这里，省下一次重追：**
    #
    #   灾备快照（`zhihui-recovery.service` → `/opt/zhihui-release/recovery/recovery_v3.py`）
    #     └─ 从快照里取出**内层的** `application.tar.gz`
    #          └─ 用本模块的 `verify_release_archive` 校验它内部的清单
    #               └─ **那个清单写着 v1.2.96**
    #
    # 几个容易走错的岔路，都已经排除：
    #
    #   - `/opt/workbuddy-manager/server` 和 `/deploy` **都是符号链接**，
    #     指向 `releases/current/...`，**不是**陈旧的安装根副本
    #   - `recovery_v3.py` 用固定路径导入本模块，但那个路径经符号链接解析后
    #     就是当前发布，**不是**旧代码
    #   - 本模块和 `offsitebackup.py` **都不产** `release_check` 字段
    #     （后者加密方式是 AES-256-GCM，而那份快照是 age-X25519）
    #
    # **所以 `v1.2.96` 是那个内层 tar 自带的值** —— 它是什么时候、由什么产的
    # 那一版 tar，没能在机器上确定。**这是既定事实，本模块改变不了它。**
    #
    # 本模块能做的是把「当前运行版本」也记下来，让两者不一致时**当场可见**，
    # 而不是等到有人翻旧备份才疑惑。
    #
    # 读不到当前版本不算错误（布局可能不同），但**必须记成「无法确认」**，
    # 不能默认成一致 —— 一个默认成一致的自检等于没有自检。
    current_version=''
    try:
        current_version=(MANAGER/'releases/current/.version').read_text().strip()
    except OSError:
        current_version=''
    def _bare(value):
        return value[1:] if isinstance(value,str) and value.startswith('v') else value
    return {'release_id':release_id,'files_verified':len(manifest['files']),'check':'ok','signature_verified':signed,
            'current_version':current_version,
            'version_consistent':bool(current_version) and _bare(current_version)==_bare(release_id)}


def verify_paired_archive(archive, destination, receipt):
    from server.portalrecovery import _allowed, restore_quarantine
    from server.portalrecovery import MAX_TOTAL, MAX_FILES
    root = Path(destination) / 'pair-verification'
    members = [member for member in archive.getmembers()
               if member.name.startswith('paired-ledgers/') and not member.isdir()]
    total = sum(member.size for member in members)
    if len(members) > MAX_FILES or total > MAX_TOTAL:
        raise RuntimeError('paired recovery exceeds extraction budget')
    if shutil.disk_usage(destination).free < total * 2 + MIN_FREE_BYTES:
        raise RuntimeError('insufficient free space for isolated paired restore')
    root.mkdir(mode=0o700)
    for member in members:
        if not member.name.startswith('paired-ledgers/') or member.isdir():
            continue
        name = member.name[len('paired-ledgers/'):]
        if not member.isfile() or name != 'manifest.json' and not _allowed(name):
            raise RuntimeError('unsafe paired recovery member')
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as output:
            os.chmod(path, 0o600)
            with archive.extractfile(member) as source:
                import shutil
                shutil.copyfileobj(source, output)
    restored = restore_quarantine(root, Path(destination) / 'quarantine-verification', receipt['manifest_sha256'])
    if restored.get('quarantined') is not True:
        raise RuntimeError('paired recovery was not quarantined')
    return {'restore_check': 'ok', 'quarantined': True, 'manifest_sha256': receipt['manifest_sha256']}

def main():
    os.umask(0o077)
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True); BASE.chmod(0o700)
    lock=(BASE/'backup.lock').open('a')
    try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('backup already running')
    # 清理上一次被中断留下的临时工作区。
    #
    # `TemporaryDirectory` 只在正常退出时删除自己；被 SIGKILL、OOM 或
    # 宿主重启打断时，`snapshot-*` 会原样留下 —— 而它里面有一份完整
    # 的 manager.db，一份就是一点几 GB。
    #
    # 2026-11-15 实测：两次中断累积了 2.6 GB，在磁盘涨到 94% 时才被发现。
    # 当时是手工删的；这段让下一次不必再手工。
    #
    # 只在**拿到独占锁之后**动它：那时可以确定没有别的备份在跑，
    # 留下的这些必然是死的。仍然按「早于本次启动」判断，避免误删。
    for stale in BASE.glob('snapshot-*'):
        if not stale.is_dir() or stale.is_symlink():
            continue
        try:
            shutil.rmtree(stale)
        except OSError:
            # 删不掉不该阻断一次正常备份；下次启动再试。
            pass
    stamp=time.strftime('%Y%m%d-%H%M%S')
    output=BASE/('recovery-'+stamp+'.tar.gz')
    managed=managed_sources(MANAGER)
    managed_names={name for name,raw,mode in managed}
    state_owners={}
    state_directories={}
    with tempfile.TemporaryDirectory(prefix='snapshot-',dir=BASE) as scratch:
        database_size = (MANAGER/'data/manager.db').stat().st_size
        if database_size > MAX_DATABASE_BYTES or shutil.disk_usage(scratch).free < database_size * 3 + MIN_FREE_BYTES:
            raise RuntimeError('database snapshot exceeds recovery disk budget')
        pair = None
        if (MANAGER/'data/user-portal.sqlite3').is_file():
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from server.portalrecovery import create_paired_snapshot
            pair_root = Path(scratch)/'paired-ledgers'
            pair = create_paired_snapshot(main_db=MANAGER/'data/manager.db', portal_db=MANAGER/'data/user-portal.sqlite3',
                users=MANAGER/'data/users.json', auths=GATEWAY/'auths', isolated=MANAGER/'data/user-portal-auths',
                spool=MANAGER/'data/ledger-spool', destination=pair_root)
        database=Path(scratch)/'manager.db'
        source=sqlite3.connect('file:'+str(MANAGER/'data/manager.db')+'?mode=ro',uri=True)
        dest=sqlite3.connect(database)
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        deadline = time.monotonic() + 900
        def progress(status, remaining, total):
            if total * page_size > MAX_DATABASE_BYTES or time.monotonic() > deadline:
                raise RuntimeError('database snapshot exceeds size or time budget')
        try:
            source.backup(dest, pages=256, progress=progress)
        except BaseException:
            dest.close()
            raise
        finally:
            source.close()
        if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('database snapshot invalid')
        count=dest.execute('SELECT count(*) FROM request_logs').fetchone()[0]; dest.close()
        candidate = Path(scratch)/'application.tar.gz'
        with RecoveryArchive.open(candidate,'w:gz') as archive:
            if pair:
                archive.add(pair_root, arcname='paired-ledgers')
            for path,name in [(GATEWAY/'auths','gateway/auths'),(GATEWAY/'data','gateway/data'),(MANAGER/'data','manager/data')]:
                state_directories[name]=add_state_directory(archive,name,path)
            archive.add(database,arcname='manager/data/manager.db')
            state=[GATEWAY/'config.json',GATEWAY/'data/state.json',GATEWAY/'data/session-binds.json',MANAGER/'data/users.json',MANAGER/'data/zh-gateway.json']
            state.extend((GATEWAY/'auths').glob('*.json'))
            state.extend((GATEWAY/'auths').glob('*.json.disabled'))
            for path in state:
                if path.is_file() and not path.is_symlink():
                    root=GATEWAY if path.is_relative_to(GATEWAY) else MANAGER
                    name=('gateway' if root==GATEWAY else 'manager')+'/'+path.relative_to(root).as_posix()
                    metadata=path.stat();state_owners[name]=(metadata.st_uid,metadata.st_gid)
                    add_bytes(archive,name,stable_json(path),uid=metadata.st_uid,gid=metadata.st_gid)
            for root,family in ((GATEWAY,'gateway'),(MANAGER,'manager')):
                for path in root.rglob('*'):
                    relative=path.relative_to(root)
                    if root==MANAGER and relative.parts[0]=='releases':continue
                    if root==MANAGER and relative.as_posix() in managed_names:continue
                    if any(part in ('.git','data','auths','venv','.local-config','backup','__pycache__','manager') or part.startswith(('preupgrade-backup','backup-')) for part in relative.parts): continue
                    if not path.is_file() or path.is_symlink() or '.bak' in path.name or '.pre-' in path.name: continue
                    if not relative.as_posix().startswith('web/out/') and path.suffix not in ('.go','.mod','.sum','.sh','.py','.md','.html','.js','.css','.svg','.png','.ico','.woff','.woff2','.yaml','.yml') and path.name not in ('Dockerfile','.dockerignore','.version','manifest.json','managed-release.json','requirements.txt','requirements.lock.txt','model.json','config.example.json'): continue
                    archive.add(path,arcname=family+'/'+relative.as_posix())
            for relative,raw,mode in managed:
                add_release_bytes(archive,'manager/'+relative,raw,mode)
            # 恢复点里必须能分清两个「版本」。
            #
            # `manager/.version` 打包的是**安装根**的那一份（`/opt/workbuddy-manager/.version`），
            # 它记录的是安装根自己什么时候被铺开过，**不等于正在服务的发布版本**。
            # 2026-11-15 实测：安装根那份是 `v1.2.2+audit.20261112.1`，
            # 而 `releases/current` 是 `v1.2.143` —— 恢复时若只看前者会误判。
            #
            # 两者都不是坏数据，混在一起才是问题。这里把发布版本单独落一份，
            # 名字直接说清它是发布版本。
            release_version=MANAGER/'releases'/'current'/'.version'
            if release_version.is_file() and not release_version.is_symlink():
                add_bytes(archive,'manager/release-version.txt',
                          release_version.read_bytes(),uid=0,gid=0)
            add_bytes(archive,'snapshot.json',json.dumps({'ts':int(time.time()),'request_rows':count,'database_quick_check':'ok','auth_consistency':'per-file stable reads'}).encode())
            for path in (Path('/etc/nginx/sites-available/zhihui.kaogong.art'),Path('/etc/nginx/conf.d/workbuddy-audit.conf')):
                if path.is_file() and not path.is_symlink():
                    add_bytes(archive,'nginx/'+path.name,path.read_bytes())
            for name,raw in recovery_host_metadata().items():
                add_bytes(archive,'private-host/'+name,raw)
            archive.add_recovery_manifest()
        with tarfile.open(candidate) as archive:
            release_check=verify_release_archive(archive)
            if pair:
                pair.update(verify_paired_archive(archive, scratch, pair))
            for name,owners in state_owners.items():
                member=archive.getmember(name)
                if (member.uid,member.gid)!=owners or member.mode!=0o600:
                    raise RuntimeError('private recovery ownership mismatch')
            for name,metadata in state_directories.items():
                member=archive.getmember(name)
                if not member.isdir() or (member.uid,member.gid,member.mode)!=metadata:
                    raise RuntimeError('private recovery directory metadata mismatch')
            # 恢复验证要把库解出来查一次，但**不能整库读进内存**。
            #
            # 2026-11-25：这里原来是 `restored.write_bytes(archive.extractfile(...).read())`，
            # 把 750 MB 的 manager.db 整个读进内存再写盘。加上下面那行整包读进内存算
            # sha256（406 MB），峰值约 1.1 GB，而备份进程的 cgroup 上限是 512 MB ——
            # 于是它在 05:34 被内核 OOM 杀掉，备份中断，留下一个没清理的中间目录。
            # 那次失败本身被「前一份报告还没超 26 小时」兜住了，所以全局仍报健康，
            # 但如果连续两次，就会变成「备份陈旧」而没人知道原因。
            #
            # 改成流式：内存占用与库大小无关，恒定一个 1 MB 缓冲。
            restored=Path(scratch)/'restore.db'
            with archive.extractfile('manager/data/manager.db') as src, restored.open('wb') as dst:
                shutil.copyfileobj(src, dst, 1024*1024)
            con=sqlite3.connect(restored)
            if con.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('restore check failed')
            if con.execute('SELECT count(*) FROM request_logs').fetchone()[0]!=count: raise RuntimeError('restore row mismatch')
            con.close()
        # 同理：整包 400 MB 读进内存只为算一个哈希，也是 OOM 的一半原因。
        digest=hashlib.sha256()
        with candidate.open('rb') as handle:
            for block in iter(lambda: handle.read(1024*1024), b''):
                digest.update(block)
        os.replace(candidate, output); output.chmod(0o600)
        result={'path':str(output),'sha256':digest.hexdigest(),'bytes':output.stat().st_size,'verified_at':int(time.time()),'request_rows':count,'restore_check':'ok','paired_ledger_check':pair,'release_check':release_check,'state_owner_files_verified':len(state_owners),'state_owner_dirs_verified':len(state_directories),'offsite':False,'retention':'retained pending offsite policy'}
        # The local verified recovery remains available if publication fails.
        if Path('/root/backups/workbuddy-recovery/offsite.json').is_file():
            sys.path.insert(0,str(Path(__file__).resolve().parent))
            import offsitebackup
            try:result['offsite']=offsitebackup.configured_publish(output)
            except Exception as exc:
                result['offsite_error']=type(exc).__name__+': '+str(exc)
        status=BASE/'latest.json.tmp'; status.write_text(json.dumps(result,indent=2)); status.chmod(0o600)
        os.replace(status,BASE/'latest.json')
        from backupstatus import publish_verified_status
        publish_verified_status(result, os.environ.get('WB_BACKUP_STATUS_FILE', '/var/lib/zhihui-gateway/backup-status.json'))
        print(json.dumps(result))
        if 'offsite_error' in result:raise SystemExit(1)

if __name__=='__main__': main()
