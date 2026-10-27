#!/opt/workbuddy-manager/venv/bin/python
"""Private consistent snapshots with optional authenticated encrypted offsite publication."""
from pathlib import Path
import fcntl,hashlib,io,json,os,sqlite3,stat,sys,tarfile,tempfile,time,subprocess

BASE=Path('/root/backups/workbuddy-daily')
MANAGER=Path('/opt/workbuddy-manager')
GATEWAY=Path('/opt/workbuddy2api')

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
        out['manager-local-config.bin']=(MANAGER/'.local-config').read_bytes()
    out['environment-references.json']=json.dumps({'paths':re.findall(r'(/[A-Za-z0-9_./-]+)\s+\(ignore_errors=',props)}).encode()
    return out

def stable_json(path):
    for _ in range(3):
        before=path.stat(); raw=path.read_bytes(); after=path.stat()
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
        raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['candidate_sha256']:
            raise RuntimeError('managed recovery source hash mismatch: '+relative)
        records.append((relative,raw,actual))
    records.append(('deploy/managed-release.json',marker_raw,stat.S_IMODE(marker.stat().st_mode)))
    if manifest.get('signature_required'):
        # Detached signatures cannot be inside the manifest they sign. Archive
        # both separately and verify against the installed custodian anchor.
        # 2026-10-26：锚与校验命令按清单声明的算法选（v9 起是 Ed25519）。
        anchor_name=trust_anchor_name(manifest)
        for relative in ('deploy/managed-release.sig',anchor_name):
            source=root/relative
            if not source.is_file() or source.is_symlink():raise RuntimeError('signed recovery material missing: '+relative)
            records.append((relative,source.read_bytes(),stat.S_IMODE(source.stat().st_mode)))
        subprocess.run(signature_verify_command(
                           release_algorithm(manifest),root/anchor_name,
                           root/'deploy/managed-release.sig',marker),
                       capture_output=True,check=True,timeout=10)
    return records

def add_release_bytes(archive,name,raw,mode):
    info=tarfile.TarInfo(name);info.size=len(raw);info.mode=mode;info.mtime=int(time.time())
    archive.addfile(info,io.BytesIO(raw))

def add_state_directory(archive,name,path):
    if path.is_symlink() or not path.is_dir():raise RuntimeError('private recovery directory missing or unsafe')
    metadata=path.stat();info=tarfile.TarInfo(name);info.type=tarfile.DIRTYPE
    info.mode=stat.S_IMODE(metadata.st_mode);info.uid=metadata.st_uid;info.gid=metadata.st_gid;info.mtime=int(time.time())
    archive.addfile(info)
    return info.uid,info.gid,info.mode

def verify_release_archive(archive):
    names=archive.getnames()
    if len(names)!=len(set(names)):raise RuntimeError('duplicate recovery archive members')
    manifest=json.load(archive.extractfile('manager/deploy/managed-release.json'))
    for relative,item in manifest['files'].items():
        name='manager/'+relative
        try:member=archive.getmember(name)
        except KeyError:raise RuntimeError('managed recovery member missing: '+relative) from None
        if not member.isfile() or member.mode!=item['mode']:
            raise RuntimeError('managed recovery archive permissions mismatch: '+relative)
        if hashlib.sha256(archive.extractfile(member).read()).hexdigest()!=item['candidate_sha256']:
            raise RuntimeError('managed recovery archive hash mismatch: '+relative)
    signed=False
    if manifest.get('signature_required'):
        # 恢复侧同样按清单算法校验（v9 起 Ed25519）；锚必须与本机 custodian 锚一致。
        anchor_name=trust_anchor_name(manifest)
        anchor=archive.extractfile('manager/'+anchor_name).read()
        installed=MANAGER/anchor_name
        if not installed.is_file() or installed.is_symlink() or installed.read_bytes()!=anchor:
            raise RuntimeError('recovery trust anchor does not match installed custodian key')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for path,name in (('anchor','manager/'+anchor_name),('signature','manager/deploy/managed-release.sig'),('manifest','manager/deploy/managed-release.json')):
                (root/path).write_bytes(archive.extractfile(name).read())
            subprocess.run(signature_verify_command(
                               release_algorithm(manifest),root/'anchor',
                               root/'signature',root/'manifest'),
                           capture_output=True,check=True,timeout=10)
        signed=True
    # 本地清单（sign_release.py 生成）用的是 version 字段；旧清单曾叫 release_id。
    # 两者都认，缺失时回落到空串——备份不该因一个展示用字段而整体失败。
    release_id=manifest.get('release_id') or manifest.get('version') or ''
    return {'release_id':release_id,'files_verified':len(manifest['files']),'check':'ok','signature_verified':signed}

def main():
    os.umask(0o077)
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True); BASE.chmod(0o700)
    lock=(BASE/'backup.lock').open('a')
    try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('backup already running')
    stamp=time.strftime('%Y%m%d-%H%M%S')
    output=BASE/('recovery-'+stamp+'.tar.gz')
    managed=managed_sources(MANAGER)
    managed_names={name for name,raw,mode in managed}
    state_owners={}
    state_directories={}
    with tempfile.TemporaryDirectory(prefix='snapshot-',dir=BASE) as scratch:
        database=Path(scratch)/'manager.db'
        source=sqlite3.connect('file:'+str(MANAGER/'data/manager.db')+'?mode=ro',uri=True)
        dest=sqlite3.connect(database); source.backup(dest); source.close()
        if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('database snapshot invalid')
        count=dest.execute('SELECT count(*) FROM request_logs').fetchone()[0]; dest.close()
        with tarfile.open(str(output)+'.tmp','w:gz') as archive:
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
            add_bytes(archive,'snapshot.json',json.dumps({'ts':int(time.time()),'request_rows':count,'database_quick_check':'ok','auth_consistency':'per-file stable reads'}).encode())
            for path in (Path('/etc/nginx/sites-available/zhihui.kaogong.art'),Path('/etc/nginx/conf.d/workbuddy-audit.conf')):
                if path.is_file() and not path.is_symlink():
                    add_bytes(archive,'nginx/'+path.name,path.read_bytes())
            for name,raw in recovery_host_metadata().items():
                add_bytes(archive,'private-host/'+name,raw)
        os.replace(str(output)+'.tmp',output); output.chmod(0o600)
        with tarfile.open(output) as archive:
            release_check=verify_release_archive(archive)
            for name,owners in state_owners.items():
                member=archive.getmember(name)
                if (member.uid,member.gid)!=owners or member.mode!=0o600:
                    raise RuntimeError('private recovery ownership mismatch')
            for name,metadata in state_directories.items():
                member=archive.getmember(name)
                if not member.isdir() or (member.uid,member.gid,member.mode)!=metadata:
                    raise RuntimeError('private recovery directory metadata mismatch')
            restored=Path(scratch)/'restore.db'; restored.write_bytes(archive.extractfile('manager/data/manager.db').read())
            con=sqlite3.connect(restored)
            if con.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('restore check failed')
            if con.execute('SELECT count(*) FROM request_logs').fetchone()[0]!=count: raise RuntimeError('restore row mismatch')
            con.close()
        result={'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'bytes':output.stat().st_size,'request_rows':count,'restore_check':'ok','release_check':release_check,'state_owner_files_verified':len(state_owners),'state_owner_dirs_verified':len(state_directories),'offsite':False,'retention':'retained pending offsite policy'}
        # The local verified recovery remains available if publication fails.
        if Path('/root/backups/workbuddy-recovery/offsite.json').is_file():
            sys.path.insert(0,str(Path(__file__).resolve().parent))
            import offsitebackup
            try:result['offsite']=offsitebackup.configured_publish(output)
            except Exception as exc:
                result['offsite_error']=type(exc).__name__+': '+str(exc)
        status=BASE/'latest.json.tmp'; status.write_text(json.dumps(result,indent=2)); status.chmod(0o600)
        os.replace(status,BASE/'latest.json'); print(json.dumps(result))
        if 'offsite_error' in result:raise SystemExit(1)

if __name__=='__main__': main()
