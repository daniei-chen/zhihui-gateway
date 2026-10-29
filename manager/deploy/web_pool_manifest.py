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
import subprocess
import sys
from pathlib import Path

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
    return pool / 'static' / build


def iter_assets(root: Path):
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name in SKIP_NAMES:
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


def sign_manifest(root: Path, key: Path) -> None:
    manifest = root / MANIFEST_NAME
    signature = root / SIGNATURE_NAME
    if signature.exists():
        signature.unlink()
    proc = subprocess.run(
        ['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin',
         '-in', str(manifest), '-out', str(signature)],
        capture_output=True, timeout=60)
    if proc.returncode:
        raise SystemExit('签名失败：%s' % proc.stderr.decode('utf-8', 'replace')[:300])
    signature.chmod(0o644)


def verify_manifest(root: Path, trust: Path = TRUST) -> tuple[bool, str]:
    manifest = root / MANIFEST_NAME
    signature = root / SIGNATURE_NAME
    if not manifest.is_file():
        return False, '清单缺失'
    if not signature.is_file():
        return False, '签名缺失'
    if not trust.is_file():
        return False, '信任锚缺失'
    proc = subprocess.run(
        ['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(trust),
         '-rawin', '-in', str(manifest), '-sigfile', str(signature)],
        capture_output=True, timeout=30)
    if proc.returncode:
        return False, '签名校验失败'
    listed = 0
    bad = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        parts = line.split('  ')
        if len(parts) != 3:
            bad.append('格式错误: %r' % line[:80])
            continue
        digest, size, rel = parts
        path = root / rel
        if not path.is_file():
            bad.append('缺失: %s' % rel)
            continue
        if path.stat().st_size != int(size) or sha256_file(path) != digest:
            bad.append('哈希不符: %s' % rel)
        listed += 1
    on_disk = sum(1 for _ in iter_assets(root))
    if on_disk != listed:
        bad.append('未登记文件 %d 个（磁盘 %d / 清单 %d）' % (on_disk - listed, on_disk, listed))
    if bad:
        return False, '; '.join(bad[:5])
    return True, '%d 个文件通过' % listed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--pool', default=str(DEFAULT_POOL))
    parser.add_argument('--build', default=None)
    parser.add_argument('--key', default=None)
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
            ok, detail = verify_manifest(root)
            print('%-28s %s  %s' % (name, 'OK' if ok else 'FAIL', detail))
        return 0

    targets = [args.build] if args.build else builds
    if args.verify:
        failed = 0
        for name in targets:
            root = build_dir(pool, name)
            if not root.is_dir():
                print('%-28s 目录不存在' % name)
                failed += 1
                continue
            ok, detail = verify_manifest(root)
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
        count = write_manifest(root)
        sign_manifest(root, key)
        print('%-28s 已生成并签名（%d 文件）' % (name, count))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
