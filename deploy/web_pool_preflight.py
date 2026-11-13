#!/opt/workbuddy-manager/venv/bin/python
"""前端 serving 树的闭包校验（U12，2026-10-28）。

## 它补的是哪个洞

`deploy/web_preflight.py` 证明「这棵树是签过名的」，但**不证明「nginx 正在服务的
就是这棵树」**。指针链是三级软链：

    /opt/workbuddy-manager/web/out  ->  /var/www/zhihui-console-current
                                    ->  /var/www/zhihui-console-releases/<buildId>

只要有人把 `zhihui-console-current` 重指向 `console-releases/` 下另一棵**已签名**
的旧树，`web_preflight.py` 依旧全绿（那棵树确实签过），但用户拿到的是旧前端。
同理，`/_next/` 由 `/var/www/zhihui-next-pool/static/<buildId>/` 提供，池里的
chunk 原先没有任何清单（见 `deploy/web_pool_manifest.py`）。

本脚本把「期望值」变成可校验的事实：

  * `web/current.json`（root 0644，由发布流程写）记录**期望**的 buildId；
  * 解析指针链得到**实际** buildId，与期望值比对；
  * 校验池内该 buildId（以及池内每个已登记目录）的 Ed25519 清单；
  * 校验 `console-releases/<buildId>` 的清单（调用 web_preflight 的同一套逻辑）。

## 用法

    web_pool_preflight.py [--web-root DIR] [--pool DIR] [--releases DIR] [--allow-drift]

退出码：0 通过；3 签名失败；4 哈希/清单不符；6 指针链与期望值不一致；7 缺清单。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

TRUST = Path('/opt/workbuddy-manager/release-trust-ed25519.pem')
DEFAULT_WEB = Path('/opt/workbuddy-manager/web')
DEFAULT_POOL = Path('/var/www/zhihui-next-pool')
DEFAULT_RELEASES = Path('/var/www/zhihui-console-releases')
CURRENT_NAME = 'zhihui-console-current'
MANIFEST = 'manifest.sha256'
SIGNATURE = 'manifest.sig'


def verify_tree(root: Path, trust: Path = TRUST) -> tuple[bool, str]:
    """校验一个目录的 manifest.sha256 + manifest.sig + 逐文件哈希。"""
    manifest = root / MANIFEST
    signature = root / SIGNATURE
    if manifest.is_symlink() or not manifest.is_file():
        return False, '清单缺失'
    if signature.is_symlink() or not signature.is_file():
        return False, '签名缺失'
    if trust.is_symlink() or not trust.is_file():
        return False, '信任锚缺失'
    proc = subprocess.run(
        ['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(trust),
         '-rawin', '-in', str(manifest), '-sigfile', str(signature)],
        capture_output=True, timeout=30)
    if proc.returncode:
        return False, '签名校验失败'
    listed = set()
    for line in manifest.read_text(encoding='utf-8').splitlines():
        parts = line.split('  ')
        if len(parts) != 3:
            return False, '清单格式错误: %r' % line[:80]
        digest, size, rel = parts
        relative = PurePosixPath(rel)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in rel
                or not relative.parts or relative.as_posix() != rel
                or rel in listed or rel in {MANIFEST, SIGNATURE}
                or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
            return False, '不安全或重复清单项: %r' % rel
        try:
            size = int(size)
        except ValueError:
            return False, '无效大小: %r' % rel
        if size < 0:
            return False, '无效大小: %r' % rel
        path = root / rel
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(root.resolve())):
            return False, '缺失: %s' % rel
        if path.stat().st_size != size:
            return False, '大小不符: %s' % rel
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return False, '哈希不符: %s' % rel
        listed.add(rel)
    if not listed:
        return False, '清单为空'
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            return False, '不允许软链: %s' % path.relative_to(root)
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    metadata = {MANIFEST, SIGNATURE, 'BUILD_INFO.json'}
    if actual - metadata != listed - metadata:
        return False, '闭包不符: extra=%r missing=%r' % (
            sorted(actual - metadata - listed), sorted(listed - metadata - actual))
    return True, '%d 个文件通过' % len(listed)


def resolve_chain(web: Path) -> tuple[Path | None, str, list[str]]:
    """解析 web/out -> console-current -> releases/<buildId>，返回 (目标, buildId, 轨迹)。"""
    trail: list[str] = []
    link = web / 'out'
    if not link.exists():
        return None, '', trail + ['%s 不存在' % link]
    trail.append('%s -> %s' % (link, link.resolve()))
    target = link.resolve()
    build = target.name
    return target, build, trail


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--web-root', default=str(DEFAULT_WEB))
    parser.add_argument('--pool', default=str(DEFAULT_POOL))
    parser.add_argument('--releases', default=str(DEFAULT_RELEASES))
    parser.add_argument('--allow-drift', action='store_true',
                        help='允许指针链与 web/current.json 不一致（仅用于演练）')
    args = parser.parse_args(argv)

    web, pool, releases = Path(args.web_root), Path(args.pool), Path(args.releases)
    problems: list[str] = []

    # 1) 期望值
    current_file = web / 'current.json'
    expected = None
    if current_file.is_file():
        try:
            expected = json.loads(current_file.read_text(encoding='utf-8')).get('buildId')
        except (ValueError, OSError):
            problems.append('current.json 无法解析')
    else:
        print('FAIL 缺 web/current.json：无法证明正在服务期望构建')
        return 6
    if not isinstance(expected, str) or not expected:
        print('FAIL current.json 缺有效 buildId')
        return 6

    # 2) 指针链
    target, actual, trail = resolve_chain(web)
    for line in trail:
        print('chain  %s' % line)
    if target is None or not target.is_dir():
        print('FAIL  指针链无法解析到目录')
        return 6
    if not target.resolve().is_relative_to(releases.resolve()):
        print('FAIL  指针链未落在 %s 之内：%s' % (releases, target))
        return 6
    if expected and actual != expected:
        msg = '指针链实际 buildId=%s，期望=%s' % (actual, expected)
        if not args.allow_drift:
            print('FAIL  %s（重指向已签名旧树也会被这里拦住）' % msg)
            return 6
        print('WARN  %s（--allow-drift）' % msg)

    # 3) BUILD_INFO 一致性
    info_file = target / 'BUILD_INFO.json'
    if info_file.is_file():
        try:
            info = json.loads(info_file.read_text(encoding='utf-8'))
        except ValueError:
            info = {}
        if info.get('buildId') and info['buildId'] != actual:
            problems.append('BUILD_INFO.buildId=%s 与目录名 %s 不一致'
                            % (info['buildId'], actual))
        print('build  BUILD_INFO buildId=%s sourceCommit=%s'
              % (info.get('buildId'), (info.get('sourceCommit') or '')[:12]))

    # 4) serving 树签名（console-releases/<buildId>）
    ok, detail = verify_tree(target)
    print('served %-40s %s  %s' % (actual, 'OK' if ok else 'FAIL', detail))
    if not ok:
        return 4

    # 5) 池内清单：该 buildId 必须已登记；池内每个目录都要有有效清单
    pool_static = pool / 'static'
    if not pool_static.is_dir():
        problems.append('资源池缺失：%s' % pool_static)
    else:
        entries = sorted(p.name for p in pool_static.iterdir() if p.is_dir())
        if actual not in entries:
            problems.append('池内没有当前 buildId 的静态资源：%s' % actual)
        for name in entries:
            ok, detail = verify_tree(pool_static / name)
            print('pool   %-40s %s  %s' % (name, 'OK' if ok else 'FAIL', detail))
            if not ok:
                problems.append('池内 %s 校验失败：%s' % (name, detail))

    # 6) 已发布树：白名单内必须签名有效；白名单外只 WARN。
    #    白名单 = current.json 的 supportedBuilds（缺省为当前 buildId）。
    #    为什么不在白名单的树不 FAIL：`console-releases/` 里会沉淀历史 build，
    #    其中有些早于「前端签名通道」上线（例如 2026-10-23 的 t00Agw…），
    #    它们既没清单、也不在资源池里 —— 旧标签页早已拿不到它们的 chunk。
    #    对它们 FAIL 会让门禁永久红；但**指针链**一旦指向白名单外的树，
    #    第 2 步的 expected 比对就会拦住，所以「重指向未签名旧树」仍然被阻断。
    supported = set()
    if current_file.is_file():
        try:
            data = json.loads(current_file.read_text(encoding='utf-8'))
            supported = set(data.get('supportedBuilds') or [])
            if data.get('buildId'):
                supported.add(data['buildId'])
        except (ValueError, OSError):
            pass
    if not supported:
        supported = {actual}
    if releases.is_dir():
        for path in sorted(p for p in releases.iterdir() if p.is_dir()):
            ok, detail = verify_tree(path)
            if ok:
                print('release %-39s OK  %s' % (path.name, detail))
                continue
            if path.name in supported:
                print('release %-39s FAIL  %s' % (path.name, detail))
                problems.append('受支持树 %s 校验失败：%s' % (path.name, detail))
            else:
                print('release %-39s WARN  未列入 supportedBuilds（退役候选）：%s'
                      % (path.name, detail))

    if problems:
        for item in problems:
            print('FAIL  %s' % item)
        return 4
    print('OK    前端 serving 树闭包通过：buildId=%s（期望=%s）' % (actual, expected or '未设置'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
