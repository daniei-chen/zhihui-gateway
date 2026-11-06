"""Bind a receipt directory to a durable identity outside its mount point.

A used legacy channel needs an explicit, offline adoption; a health probe never
adopts an empty replacement directory or rewrites an existing registration.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from . import config

REGISTRY = '.spool-channel.json'
IDENTITY = '.spool-channel'


def _read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or getattr(info,'st_file_attributes',0)&0x400:
        raise ValueError('unsafe channel identity file')
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as handle:
        current=os.fstat(handle.fileno())
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):
            raise ValueError('channel identity changed during verification')
        raw=handle.read(4097)
        if len(raw)>4096:raise ValueError('channel identity exceeds its size limit')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('channel identity must be an object')
    return value


def _publish(path, value):
    payload = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    tmp = path.with_name('.identity-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)  # no overwrite, including concurrent initialization
        except FileExistsError:
            pass
        if os.name != 'nt':
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        tmp.unlink(missing_ok=True)


def check(directory, *, create=False, adopt=False):
    directory = Path(directory)
    registry = Path(config.DATA_DIR) / REGISTRY
    identity = directory / IDENTITY
    expected_path = os.path.normcase(os.path.abspath(directory))
    try:
        if directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)():
            raise ValueError('receipt directory must not be a link')
        if registry.exists() or registry.is_symlink():
            record = _read(registry)
            ident=record.get('id')
            if (record.get('schema') != 1 or record.get('path') != expected_path or
                    not isinstance(ident,str) or len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident)):
                raise ValueError('receipt channel registration differs from configured path')
            actual = _read(identity)
            if actual != {'schema': 1, 'id': record.get('id')}:
                raise ValueError('receipt channel identity was lost or replaced')
            return {'ok': True, 'id': record['id'], 'reason': ''}
        if (identity.exists() or identity.is_symlink()) and not adopt:
            raise ValueError('orphaned receipt identity requires verified offline registration')
        used = (Path(config.DATA_DIR) / '.spool-used').exists() or (directory / '.spool-used').exists()
        if used and not adopt:
            raise ValueError('used legacy receipt channel requires verified offline registration')
        if directory.exists() and not adopt and any(directory.iterdir()):
            raise ValueError('nonempty unregistered receipt channel requires verified offline registration')
        if not create:
            return {'ok': True, 'id': None, 'reason': ''}
        directory.mkdir(parents=True, exist_ok=True)
        registry.parent.mkdir(parents=True, exist_ok=True)
        if not identity.exists():
            _publish(identity, {'schema': 1, 'id': uuid.uuid4().hex})
        actual = _read(identity)
        if actual.get('schema') != 1 or not isinstance(actual.get('id'), str) or len(actual['id']) != 32:
            raise ValueError('invalid receipt channel identity')
        _publish(registry, {'schema': 1, 'path': expected_path, 'id': actual['id']})
        return check(directory)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return {'ok': False, 'id': None, 'reason': str(exc)[:250]}


def inventory(directory):
    """Read-only digest for an offline legacy registration receipt."""
    directory = Path(directory)
    if directory.is_symlink() or getattr(directory,'is_junction',lambda:False)() or not directory.is_dir():
        raise ValueError('original receipt directory is unavailable')
    files = {}
    for current,dirs,names in os.walk(directory,followlinks=False):
        for name in dirs+names:
            path=Path(current)/name
            info=path.lstat()
            if path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:
                raise ValueError('receipt inventory contains a link')
        for name in names:
            path=Path(current)/name
            if name!=IDENTITY:
                files[path.relative_to(directory).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def adopt_legacy(directory, expected_inventory, actor, evidence):
    """Operator-only offline migration, never a request/health recovery shortcut.

    All known local accounting owners must be demonstrably stopped. The caller
    must compare the inventory with the complete recovery backup, not merely
    bless a newly-created empty directory.
    """
    from . import budgetowner
    if not (isinstance(actor, str) and 1 <= len(actor.strip()) <= 64 and
            isinstance(evidence, str) and 8 <= len(evidence.strip()) <= 500):
        raise ValueError('offline registration requires an actor and recovery evidence')
    registry=Path(config.DATA_DIR)/REGISTRY
    if registry.exists() or registry.is_symlink():
        raise ValueError('channel already registered; restore its original identity instead of adopting a replacement')
    owners = Path(config.DB_PATH).parent / '.budget-owners'
    if owners.exists():
        for path in owners.iterdir():
            host, sep, nonce = path.name.rpartition('_')
            if not sep or budgetowner.alive(host + ':' + nonce) is not False:
                raise ValueError('accounting owner is active or its liveness is unknown')
    directory = Path(directory)
    actual_inventory=inventory(directory)
    if actual_inventory != expected_inventory:
        raise ValueError('receipt inventory differs from verified recovery backup')
    if actual_inventory==hashlib.sha256(b'{}').hexdigest():
        raise ValueError('empty legacy inventory cannot prove recovery; this command cannot adopt an empty replacement')
    result = check(directory, create=True, adopt=True)
    if not result['ok'] or inventory(directory) != expected_inventory:
        raise ValueError('offline channel registration failed or inventory changed')
    _publish(Path(config.DATA_DIR) / '.spool-adoption.json', {
        'actor': actor.strip(), 'evidence': evidence.strip(), 'inventory': expected_inventory,
        'identity': result['id']})
    return result


def main():
    import argparse
    from .ledgerreplay import spool_dir
    parser=argparse.ArgumentParser(description='Read-only inventory or explicit offline legacy channel registration')
    parser.add_argument('operation',choices=('inventory','adopt-legacy'))
    parser.add_argument('--verified-inventory')
    parser.add_argument('--actor')
    parser.add_argument('--evidence')
    parser.add_argument('--offline-confirmed',action='store_true')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    directory=spool_dir()
    if args.operation=='inventory':
        print(json.dumps({'inventory':inventory(directory),'channel':check(directory)}))
        return
    if not args.apply or not args.offline_confirmed:
        parser.error('adopt-legacy requires --apply and --offline-confirmed; inventory alone does not prove recovery')
    result=adopt_legacy(directory,args.verified_inventory,args.actor,args.evidence)
    print(json.dumps({'registered':result['ok'],'channel_id':result['id']}))


if __name__=='__main__':main()
