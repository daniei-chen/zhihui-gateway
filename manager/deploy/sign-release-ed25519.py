#!/opt/workbuddy-manager/venv/bin/python
"""清单签名工具（Ed25519 通道）——后端 JSON 清单与前端 manifest.sha256 共用。

## 为什么需要这个文件

生产启动门禁（`deploy/source_preflight.py`）验的是 **Ed25519 detached signature**，
用 `openssl pkeyutl -verify -rawin`。但仓库里只有 `deploy/sign-release.sh`，它走的是
**旧 RSA 通道**（`openssl dgst -sha256 -sign` + `release-signing.key` + `release-trust.pem`）。

于是 2026-10-26 之前，319 文件清单的签名是一次性在 `/tmp` 里手敲出来的命令——也就是说：

  * 验签能重现，**签名不能重现**；
  * 换个人接手时，唯一能签出有效清单的办法是去翻历史命令；
  * 而「签不出来」会逼人降级回 RSA 通道（把 `signature_algorithm` 改掉），
    正好是门禁注释里说「不允许被降级」的那个方向。

这个脚本把签名流程固化下来，并让后端清单与前端产物 manifest 走同一把密钥、
同一套校验，避免两条通道各写一遍。

## 用法

    # 后端清单：重算逐文件 sha/mode 后签名（替代 sign-release.sh 的 RSA 版）
    sudo WB_SIGN_KEY=/opt/zhihui-release/keys/release-priv.pem \\
        deploy/sign-release-ed25519.py --rebuild --manifest deploy/managed-release.json

    # 前端产物 manifest：签名（release-web-v2.sh 内部调用）
    deploy/sign-release-ed25519.py --sign-only --manifest out/manifest.sha256 \\
        --sig out/manifest.sig --key /opt/zhihui-release/keys/release-priv.pem

    # 只校验，不改文件（CI / 巡检可跑）
    deploy/sign-release-ed25519.py --verify --manifest deploy/managed-release.json

退出码：0 成功，2 缺少密钥/锚、3 校验失败、4 清单与磁盘不一致。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

#: 默认信任锚（与 source_preflight.py 用的是同一把公钥）
DEFAULT_TRUST = '/opt/workbuddy-manager/release-trust-ed25519.pem'
#: 清单里允许的文件权限（与 source_preflight.py 的 SAFE_MODES 保持一致）
SAFE_MODES = {0o400, 0o440, 0o444, 0o600, 0o640, 0o644, 0o700, 0o750, 0o755}
#: 必须可执行的文件
ENTRYPOINTS = {'deploy/source_preflight.py', 'server/backup_application.py',
               'deploy/web_preflight.py', 'deploy/sign-release-ed25519.py'}
ALGORITHM = 'ed25519'


def die(code: int, message: str) -> 'NoReturn':  # noqa: F821 - 仅用于类型提示可读性
    sys.stderr.write('ABORT: %s\n' % message)
    raise SystemExit(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_key(cli_key: str | None) -> Path:
    """密钥来源：--key 优先，其次 WB_SIGN_KEY。**不接受空**：缺密钥就退出，不降级。"""
    raw = cli_key or os.environ.get('WB_SIGN_KEY') or ''
    if not raw:
        die(2, '缺少签名私钥。用 --key 或 WB_SIGN_KEY 指定（私钥应离线保管，'
               '签名时临时拷入并立即删除，见 /opt/workbuddy-manager/SIGNING.md）')
    key = Path(raw)
    if not key.is_file():
        die(2, '签名私钥不存在：%s' % key)
    return key


def sign_file(key: Path, target: Path, signature: Path) -> None:
    """Ed25519 detached signature，**raw over 目标文件字节**（与验签侧一致）。"""
    if signature.exists():
        signature.unlink()
    proc = subprocess.run(
        ['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin',
         '-in', str(target), '-out', str(signature)],
        capture_output=True, timeout=30)
    if proc.returncode:
        die(3, '签名失败：%s' % proc.stderr.decode('utf-8', 'replace').strip()[:400])


def verify_file(trust: Path, target: Path, signature: Path) -> None:
    if not trust.is_file():
        die(2, '信任锚不存在：%s' % trust)
    if not signature.is_file():
        die(3, '签名文件不存在：%s' % signature)
    proc = subprocess.run(
        ['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(trust), '-rawin',
         '-in', str(target), '-sigfile', str(signature)],
        capture_output=True, timeout=30)
    if proc.returncode:
        die(3, '签名校验失败：%s' % target)


def rebuild_json_manifest(root: Path, manifest: Path) -> dict:
    """重算清单里每个文件的 sha256 / mode，并物理检查它们。

    与 `sign-release.sh`（RSA 版）的检查等价：清单引用的文件必须存在、不是软链、
    mode 在安全白名单内、entrypoint 必须可执行。清单的**键集合仍是人工维护**——
    本脚本只重算哈希，不增删条目，避免「自动扫描把不该发布的文件签进来」。
    """
    release = json.loads(manifest.read_text(encoding='utf-8'))
    if release.get('signature_algorithm') != ALGORITHM:
        # 允许首次从旧清单升级：显式写入算法，而不是猜
        release['signature_algorithm'] = ALGORITHM
    if release.get('signature_required') is not True:
        release['signature_required'] = True

    problems: list[str] = []
    if 'deploy/managed-release.json' in release.get('files', {}):
        die(4, '清单不得包含自身（deploy/managed-release.json）')

    changed = 0
    for relative, item in release['files'].items():
        if relative.startswith('/') or '..' in relative.split('/') or '\\' in relative:
            problems.append('不安全路径: %r' % relative)
            continue
        path = root / relative
        if path.is_symlink() or not path.is_file():
            problems.append('缺失或为软链: %s' % relative)
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode not in SAFE_MODES:
            problems.append('权限不在白名单: %s=%o' % (relative, mode))
            continue
        if relative in ENTRYPOINTS and not mode & 0o111:
            problems.append('entrypoint 不可执行: %s' % relative)
            continue
        digest = sha256_file(path)
        if item.get('candidate_sha256') != digest or item.get('mode') != mode:
            changed += 1
            item['candidate_sha256'] = digest
            item['mode'] = mode

    if problems:
        for problem in problems[:20]:
            sys.stderr.write('  %s\n' % problem)
        die(4, '清单与磁盘不一致（%d 项）' % len(problems))

    manifest.write_text(json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                        encoding='utf-8')
    print('rebuilt %s: files=%d changed=%d' % (manifest, len(release['files']), changed))
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Ed25519 release manifest signer/verifier')
    parser.add_argument('--manifest', required=True,
                        help='后端 JSON 清单，或前端 manifest.sha256')
    parser.add_argument('--sig', default=None,
                        help='签名输出/输入（默认：清单同目录 manifest.sig / managed-release.sig）')
    parser.add_argument('--key', default=None, help='签名私钥（或 WB_SIGN_KEY）')
    parser.add_argument('--trust', default=DEFAULT_TRUST, help='Ed25519 信任锚')
    parser.add_argument('--root', default=None,
                        help='JSON 清单里的相对路径以哪个目录为根（默认为清单上两级）')
    parser.add_argument('--rebuild', action='store_true', help='先重算 JSON 清单的逐文件哈希')
    parser.add_argument('--add', action='append', default=None,
                        help='把文件加入 JSON 清单（可重复；随后 --rebuild 会重算全部哈希）。'
                             '只接受 SAFE_MODES 内的权限，拒绝软链。')
    parser.add_argument('--sign-only', action='store_true', help='只签，不重算（前端 manifest）')
    parser.add_argument('--verify', action='store_true', help='只校验签名，不写任何文件')
    parser.add_argument('--dry-run', action='store_true', help='重算并报告，但不写/不签')
    args = parser.parse_args(argv)

    manifest = Path(args.manifest).resolve()
    if not manifest.is_file():
        die(2, '清单不存在：%s' % manifest)
    is_json = manifest.name.endswith('.json')
    signature = Path(args.sig) if args.sig else manifest.with_name(
        'managed-release.sig' if is_json else 'manifest.sig')

    if args.verify:
        verify_file(Path(args.trust), manifest, signature)
        print('verify ok: %s' % manifest)
        return 0

    if not args.sign_only:
        if not is_json:
            die(2, '--rebuild 只适用于 JSON 清单；前端 manifest 请用 --sign-only')
        root = Path(args.root).resolve() if args.root else manifest.parent.parent
        if args.add:
            # 清单的键集合本来是人工维护的（这正是它抗「自动扫描把不该发布的文件
            # 签进来」的原因）。但新增一个**门禁脚本自身**进清单时，手工改 JSON 太
            # 容易漏字段，所以把「加键」也做成显式、可审计、可复跑的一步。
            release = json.loads(manifest.read_text(encoding='utf-8'))
            files = release.setdefault('files', {})
            for relative in args.add:
                path = root / relative
                if path.is_symlink() or not path.is_file():
                    die(4, '--add 的文件不存在或为软链：%s' % relative)
                mode = stat.S_IMODE(path.stat().st_mode)
                if mode not in SAFE_MODES:
                    die(4, '--add 的文件权限不在白名单：%s=%o' % (relative, mode))
                files[relative] = {'candidate_sha256': sha256_file(path), 'mode': mode}
                print('added %s (mode %o)' % (relative, mode))
            manifest.write_text(
                json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                encoding='utf-8')
        if args.dry_run:
            release = json.loads(manifest.read_text(encoding='utf-8'))
            changed = sum(1 for rel, item in release['files'].items()
                          if (root / rel).is_file()
                          and sha256_file(root / rel) != item.get('candidate_sha256'))
            print('dry-run: %d 个文件的哈希与会不符（未写入）' % changed)
            return 0
        rebuild_json_manifest(root, manifest)

    key = resolve_key(args.key)
    sign_file(key, manifest, signature)
    # 立刻用信任锚自验一次：签出来的东西必须当场可验，否则等于没签
    verify_file(Path(args.trust), manifest, signature)
    print('signed %s -> %s (%d bytes)' % (manifest, signature, signature.stat().st_size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
