#!/opt/workbuddy-manager/venv/bin/python
"""前端 `_next` 资源池的清单生成与校验（U12，2026-10-28）。

## 为什么需要它

`/var/www/zhihui-console-releases/<buildId>/` 有 `manifest.sha256` + `.sig`，
由 `deploy/web_preflight.py` 在启动时校验。但 nginx 的 `/_next/` 实际由
`/var/www/zhihui-next-pool/static/<buildId>/` 提供 —— **池里的 chunk 没有任何
签名清单**。能写池的人可以替换 `security/page-*.js` 或 `layout-*.js`，注入读取
cookie 的脚本，而启动门禁不会有任何反应（它只校验 console-releases 那棵树）。

本脚本把同一套 Ed25519 通道延伸到池：为每个 `static/<buildId>/` 生成
`manifest.sha256`（逐行 `<sha256>  <size>  <path>`）并用同一把密钥签名。

## 用法

    web_pool_manifest.py --pool DIR --build <id> [--key FILE]   # 生成并签名
    web_pool_manifest.py --pool DIR --verify [--build <id>]     # 校验（不写）
    web_pool_manifest.py --pool DIR --list                      # 列出池内 build 与状态

退出码：0 成功/全部通过；3 签名失败；4 哈希或清单不符；5 池内 build 无清单。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
import subprocess
import sys
from pathlib import Path, PurePosixPath

TRUST = Path('/opt/workbuddy-manager/release-trust-ed25519.pem')
DEFAULT_POOL = Path('/var/www/zhihui-next-pool')
MANIFEST_NAME = 'manifest.sha256'
SIGNATURE_NAME = 'manifest.sig'
#: 清单本身不进哈希（与后端 manifest 同一约定：签名覆盖清单字节）
SKIP_NAMES = {MANIFEST_NAME, SIGNATURE_NAME}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()



def build_dir(pool: Path, build: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', build or ''):
        raise ValueError('build ID 格式无效')
    root = pool / 'static' / build
    if pool.is_symlink() or (pool/'static').is_symlink() or root.is_symlink():
        raise ValueError('资源池目录不能是链接')
    return root



def iter_assets(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('无效资源目录')
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('资源目录包含链接')
        if not path.is_file():
            continue
        if path.stat().st_nlink != 1:
            raise ValueError('资源文件不能是硬链接')
        if path.relative_to(root).as_posix() in SKIP_NAMES:
            continue
        yield path


def write_manifest(root: Path) -> int:
    lines = []
    for path in iter_assets(root):
        rel = path.relative_to(root).as_posix()
        lines.append('%s  %d  %s' % (sha256_file(path), path.stat().st_size, rel))
    target = root / MANIFEST_NAME
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return len(lines)



def sign_manifest(root: Path, key: Path, trust: Path = TRUST) -> None:
    """Build and sign privately, verify before touching either published file.

    Publishing manifest+signature is not one filesystem transaction. Consumers
    fail closed on a mismatch; a versioned publisher is required for activation.
    """
    assets=list(iter_assets(root))
    if not assets:
        raise ValueError('资源目录为空')
    content=''.join('%s  %d  %s\n'%(sha256_file(p),p.stat().st_size,p.relative_to(root).as_posix()) for p in assets)
    with tempfile.TemporaryDirectory(prefix='.pool-sign-',dir=root.parent) as name:
        stage=Path(name)
        (stage/MANIFEST_NAME).write_text(content,encoding='utf-8')
        result=subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin',
            '-in',str(stage/MANIFEST_NAME),'-out',str(stage/SIGNATURE_NAME)],capture_output=True,timeout=60)
        if result.returncode:
            raise ValueError('资源清单签名失败；原清单/签名未修改')
        verified=subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(trust),
            '-rawin','-in',str(stage/MANIFEST_NAME),'-sigfile',str(stage/SIGNATURE_NAME)],capture_output=True,timeout=30)
        if verified.returncode:
            raise ValueError('签名与受信公钥不匹配；原文件未修改')
        for name in (MANIFEST_NAME,SIGNATURE_NAME):
            with (stage/name).open('r+b') as handle:
                os.fsync(handle.fileno())
            os.replace(stage/name,root/name)
        if os.name!='nt':
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)



def verify_manifest(root: Path, trust: Path = TRUST) -> tuple[bool, str]:
    try:
        manifest, signature = root/MANIFEST_NAME, root/SIGNATURE_NAME
        if any(p.is_symlink() or not p.is_file() for p in (manifest,signature,trust)):
            return False, '清单、签名或信任锚缺失/为链接'
        if manifest.stat().st_size > 4*1024*1024:
            return False, '清单超出预算'
        proc = subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(trust),
            '-rawin','-in',str(manifest),'-sigfile',str(signature)],capture_output=True,timeout=30)
        if proc.returncode:
            return False, '签名校验失败'
        actual = {p.relative_to(root).as_posix():p for p in iter_assets(root)}
        listed = set()
        for line in manifest.read_text(encoding='utf-8').splitlines():
            digest,size,rel = line.split('  ')
            name=PurePosixPath(rel)
            if (not re.fullmatch('[0-9a-f]{64}',digest) or not size.isdecimal()
                    or name.is_absolute() or name.as_posix()!=rel or '..' in name.parts
                    or '\\' in rel or rel in listed or rel not in actual):
                return False, '清单路径、重复条目或摘要格式无效'
            listed.add(rel)
            path=actual[rel]
            if path.stat().st_size!=int(size) or sha256_file(path)!=digest:
                return False, '资源内容不匹配'
        if not listed or listed!=set(actual):
            return False, '资源闭包不匹配或为空'
        return True, '%d 个文件通过'%len(listed)
    except (ValueError,OSError,UnicodeError,subprocess.SubprocessError):
        return False, '无法核实资源清单'


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--pool', default=str(DEFAULT_POOL))
    parser.add_argument('--build', default=None)
    parser.add_argument('--key', default=None)
    parser.add_argument('--trust', default=str(TRUST))
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args(argv)

    pool = Path(args.pool)
    if not (pool / 'static').is_dir():
        print('池不存在或结构不符：%s' % pool, file=sys.stderr)
        return 2
    builds = sorted(p.name for p in (pool / 'static').iterdir() if p.is_dir())
    if args.list:
        for name in builds:
            root = build_dir(pool, name)
            if not (root / MANIFEST_NAME).is_file():
                print('%-28s %s' % (name, 'NO-MANIFEST'))
                continue
            ok, detail = verify_manifest(root, Path(args.trust))
            print('%-28s %s  %s' % (name, 'OK' if ok else 'FAIL', detail))
        return 0

    targets = [args.build] if args.build else builds
    if not targets:
        print('没有可验证或签名的 build', file=sys.stderr)
        return 2
    try:
        for name in targets:
            if not build_dir(pool,name).is_dir():
                raise ValueError('指定 build 不存在')
    except ValueError as exc:
        print(str(exc),file=sys.stderr)
        return 2
    if args.verify:
        failed = 0
        for name in targets:
            root = build_dir(pool, name)
            if not root.is_dir():
                print('%-28s 目录不存在' % name)
                failed += 1
                continue
            ok, detail = verify_manifest(root, Path(args.trust))
            print('%-28s %s  %s' % (name, 'OK' if ok else 'FAIL', detail))
            if not ok:
                failed += 1
        return 0 if failed == 0 else (5 if any(
            not (build_dir(pool, n) / MANIFEST_NAME).is_file() for n in targets) else 4)

    key = Path(args.key) if args.key else None
    if key is None or not key.is_file():
        print('生成清单需要 --key <私钥>', file=sys.stderr)
        return 2
    for name in targets:
        root = build_dir(pool, name)
        if not root.is_dir():
            print('%-28s 目录不存在，跳过' % name)
            continue
        try:
            count = len(list(iter_assets(root)))
            sign_manifest(root, key, Path(args.trust))
        except (ValueError,OSError,subprocess.SubprocessError) as exc:
            print(str(exc),file=sys.stderr)
            return 3
        print('%-28s 已生成并签名（%d 文件）' % (name, count))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
