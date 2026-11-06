"""Decrypt, verify EVERY paired-snapshot asset, restore only to new quarantine.

No production paths, service start, ledger activation, network or key discovery.
The three expected hashes must come from an independently retained receipt.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import portalrecovery as recovery


def _sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def run(*, cipher, identity, destination, cipher_sha256, archive_sha256,
        manifest_sha256, age='age'):
    cipher, identity, destination = map(recovery._absolute, (cipher,identity,destination))
    for value in (cipher_sha256,archive_sha256,manifest_sha256):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
            raise ValueError('必须提供独立保存的三个 SHA-256 摘要')
    recovery._regular(cipher)
    recovery._regular(identity)
    if destination.exists() or _sha(cipher)!=cipher_sha256:
        raise ValueError('目标必须是新目录，且密文摘要必须匹配')
    executable=shutil.which(age)
    if executable is None:
        raise ValueError('age 解密程序不可用')
    with tempfile.TemporaryDirectory(prefix='.paired-drill-', dir=destination.parent) as folder:
        stage=Path(folder)
        archive=stage/'archive.tar.gz'
        result=subprocess.run([executable,'--decrypt','-i',str(identity),'-o',str(archive),str(cipher)],
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=300,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise ValueError('age 解密或认证失败；未恢复任何目标')
        if archive.stat().st_size>recovery.MAX_TOTAL or _sha(archive)!=archive_sha256:
            raise ValueError('明文归档摘要或大小不匹配')
        extracted=stage/'source'
        extracted.mkdir(mode=0o700)
        names=set()
        total=0
        with tarfile.open(archive,'r:gz') as bundle:
            for member in bundle:
                path=PurePosixPath(member.name)
                if (member.name in names or len(names)>recovery.MAX_FILES*2
                        or path.is_absolute() or '..' in path.parts or path.as_posix()!=member.name
                        or '\\' in member.name or ':' in member.name
                        or not (member.isfile() or member.isdir())):
                    raise ValueError('归档包含重复、链接或不规范路径')
                names.add(member.name)
                if member.isdir():
                    continue
                if member.name!='manifest.json' and not recovery._allowed(member.name):
                    raise ValueError('归档含配对快照范围外的文件')
                total+=member.size
                if member.size<0 or member.size>recovery.MAX_FILE or total>recovery.MAX_TOTAL:
                    raise ValueError('归档展开超出预算')
                target=extracted/Path(*path.parts)
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                descriptor=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(descriptor,'wb') as output,bundle.extractfile(member) as source:
                    shutil.copyfileobj(source,output,1<<20)
                    output.flush()
                    os.fsync(output.fileno())
        manifest=recovery.verify_snapshot(extracted,manifest_sha256)
        result=recovery.restore_quarantine(extracted,destination,manifest_sha256)
        return {**result,'asset_closure_verified':True,'all_sqlite_verified':True,
            'paired_ledger_verified':True,'verified_files':len(manifest['files']),
            'complete_disaster_recovery_verified':False,'service_started':False,
            'warning':'隔离恢复通过；不等于现网账务核对、服务启动或生产灾备验收通过'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ('cipher','identity','destination','cipher-sha256','archive-sha256','manifest-sha256'):
        parser.add_argument('--'+field,required=True)
    parser.add_argument('--age',default='age')
    args=vars(parser.parse_args(argv))
    try:
        result=run(**args)
    except (OSError,ValueError,subprocess.SubprocessError,tarfile.TarError) as exc:
        print(json.dumps({'ok':False,'error':type(exc).__name__,'message':str(exc)[:200]}))
        return 2
    print(json.dumps({'ok':True,**result},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
