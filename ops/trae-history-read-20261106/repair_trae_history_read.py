#!/usr/bin/env -S /usr/bin/python3 -I -S -B
"""Grant one existing service group read-only access to the live Trae DB.

No data writes, service restart, world access, recursive chmod, or owner change.
The inner dedicated SQLite directory inherits group IDs for recreated sidecars.
"""
import argparse,json,os,sqlite3,stat,subprocess,sys
from pathlib import Path

BASE=Path('/opt/trae-work/data')
DATABASE=BASE/'data/aiwork.sqlite'
STATE=Path('/opt/zhihui-release/audit-20261029-staging/v007-merge-cutover-20261106-r4/trae-history-permissions.json')
GID=10001
NAMES={'aiwork.sqlite','aiwork.sqlite-wal','aiwork.sqlite-shm'}
READ_PROGRAM="""import sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]);c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=2)
c.execute('PRAGMA query_only=ON')
required={'api_usage':{'bucket','day','data'},'credits_daily':{'date','total','earned','consumed'},'checkin_results':{'day','uid','name','status','updated_at'}}
for table,columns in required.items():
 actual={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
 if not columns<=actual:raise ValueError('required existing live history schema unavailable')
c.close();print('LIVE_HISTORY_READ_OK')
"""


def metadata(path):
    info=path.lstat()
    return {'path':str(path),'device':info.st_dev,'inode':info.st_ino,'uid':info.st_uid,'gid':info.st_gid,
            'mode':stat.S_IMODE(info.st_mode),'directory':stat.S_ISDIR(info.st_mode)}


def targets(base=BASE):
    inner=base/'data'
    for entry in base.iterdir():
        if entry==inner:continue
        info=entry.lstat()
        if entry.is_symlink() or info.st_uid!=0 or info.st_mode&0o077:
            raise ValueError('unrelated outer data-directory entry is not root-private')
    found=set()
    for entry in inner.iterdir():
        if entry.name in NAMES:found.add(entry.name);continue
        info=entry.lstat()
        if entry.is_symlink() or info.st_uid!=0 or info.st_mode&0o077:
            raise ValueError('unrelated history-directory entry is not root-private')
    if found!=NAMES:raise ValueError('the three known live SQLite artifacts are required')
    paths=[base,inner,*[inner/name for name in sorted(NAMES)]]
    for path in (base.parent,*base.parent.parents):
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('untrusted history parent')
    rows=[]
    for path in paths:
        info=path.lstat();directory=path in (base,inner)
        if (path.is_symlink() or info.st_uid!=0 or info.st_mode&0o007
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1))
            or (directory and not stat.S_ISDIR(info.st_mode))
            or any(name.startswith('system.posix_acl_') for name in os.listxattr(path))):
            raise ValueError('history file identity, privacy or ACL needs separate review')
        row=metadata(path)
        row['target_mode']=0o2710 if path==inner else (0o710 if directory else 0o640)
        if row['gid'] not in (0,GID) or row['mode'] not in (0o700,0o600,row['target_mode']):
            raise ValueError('unreviewed original history permissions')
        rows.append(row)
    return rows


def mutate(row,gid,mode):
    path=Path(row['path']);flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
    if row['directory']:flags|=os.O_DIRECTORY
    descriptor=os.open(path,flags)
    try:
        info=os.fstat(descriptor)
        if (info.st_dev,info.st_ino,info.st_uid)!=(row['device'],row['inode'],row['uid']):
            raise ValueError('history inode changed; refusing metadata replacement')
        os.fchown(descriptor,-1,gid);os.fchmod(descriptor,mode);os.fsync(descriptor)
    finally:os.close(descriptor)


def apply(rows):
    # The outside traversal permission is granted last; no list/write access.
    for row in [*rows[2:],rows[1],rows[0]]:mutate(row,GID,row['target_mode'])


def restore(rows):
    # Close outside traversal first; restore only the recorded matching inodes.
    errors=[]
    for row in rows:
        try:mutate(row,row['gid'],row['mode'])
        except FileNotFoundError:
            if Path(row['path']).name not in ('aiwork.sqlite-wal','aiwork.sqlite-shm'):errors.append('missing required inode')
        except (OSError,ValueError) as exc:errors.append(type(exc).__name__)
    if errors:raise ValueError('metadata rollback incomplete; restored all unchanged inodes; retained intent for review')


def check_read(path=DATABASE):
    return subprocess.run(['/usr/bin/setpriv','--reuid=10001','--regid=10001','--clear-groups',
        '/usr/bin/python3','-I','-S','-B','-c',READ_PROGRAM,str(path)],capture_output=True,timeout=8)


def save(path,value):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as handle:json.dump(value,handle,indent=2);handle.write('\n');handle.flush();os.fsync(handle.fileno())
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['plan','apply','rollback']);args=parser.parse_args()
    if os.geteuid()!=0 or not sys.flags.isolated or not sys.flags.no_site:raise ValueError('protected isolated root entry required')
    os.umask(0o077)
    if args.action=='rollback':
        value=json.loads(STATE.read_bytes());restore(value['before']);print(json.dumps({'permissions_restored':True,'data_restored':False}));return
    actual=subprocess.check_output(['/usr/bin/docker','inspect','zhihui-trae'],timeout=5)
    container=json.loads(actual)[0]
    if not container['State']['Running'] or not any(row.get('Source')==str(BASE) and row.get('Destination')=='/app/data' and row.get('RW') is True for row in container['Mounts']):
        raise ValueError('configured database is not the live Trae engine mount')
    expected=DATABASE.stat();opened=False
    # Only inspect descriptors owned by the current container's exact PID.
    for fd in (Path('/proc')/str(container['State']['Pid'])/'fd').iterdir():
        try:current=fd.stat()
        except FileNotFoundError:continue
        if (current.st_dev,current.st_ino)==(expected.st_dev,expected.st_ino):opened=True;break
    if not opened:raise ValueError('database is not opened by the current live Trae engine')
    rows=targets()
    root_read=subprocess.run(['/usr/bin/python3','-I','-S','-B','-c',READ_PROGRAM,str(DATABASE)],capture_output=True,timeout=5)
    if root_read.returncode or root_read.stdout.strip()!=b'LIVE_HISTORY_READ_OK':raise ValueError('existing live history schema rejected')
    before=check_read();result={'live_engine_mount_verified':True,'live_database_open_verified':True,'existing_history_schema_verified':True,
        'service_readable_before':before.returncode==0,'changed':False,'service_restart':False,'world_access':False,'business_data_written':False}
    if args.action=='apply':
        if STATE.exists():raise ValueError('existing permission intent retained; do not overwrite')
        save(STATE,{'before':rows,'group_read_role':GID})
        try:
            apply(rows);after=check_read()
            if after.returncode or after.stdout.strip()!=b'LIVE_HISTORY_READ_OK':raise ValueError('service read verification failed')
        except BaseException:restore(rows);raise
        result.update(changed=True,service_readable_after=True)
        save(STATE.with_name('trae-history-permissions-receipt.json'),result)
    print(json.dumps(result))


if __name__=='__main__':main()
