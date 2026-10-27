#!/opt/workbuddy-manager/venv/bin/python
"""已发布前端产物的只读门禁（Ed25519 通道）。

## 为什么需要它

后端源码从 2026-10 起受 `deploy/source_preflight.py` 保护：清单 + Ed25519 签名，
被改动就拒绝启动。但**前端产物一直在这道门禁之外**：

  * `deploy/managed-release.json` 里没有任何 `web/` 条目；
  * nginx 直接读 `/var/www/zhihui-console-current`，Manager 读 `web/out`；
  * 也就是说，任何能写这两个目录的人都可以替换 `security/page-*.js` 或 `layout-*.js`，
    注入读取 cookie / 改写请求的脚本，而**系统不会有任何反应**。

同一个攻击面，后端有签名、前端没有——这个不对称本身就是缺口。本脚本把同一套通道
延伸到前端产物：

  * manifest：`<web-root>/manifest.sha256`（`release/release-web-v2.sh` 安装时生成，
    逐行 `<sha256>  <size>  <path>`）；
  * 签名：`<web-root>/manifest.sig`（detached，raw over manifest.sha256 的字节）；
  * 信任锚：`/opt/workbuddy-manager/release-trust-ed25519.pem`（与后端**同一把**公钥，
    同一套 `deploy/sign-release-ed25519.py` 签）。

## 为什么「没有 manifest 就跳过」而不是一律拒绝

部署顺序是「先装本脚本与 ExecStartPre，再发布带签名的产物」。如果无 manifest 即拒绝，
那在半装状态下 Manager 根本起不来（连 API 一起挂）。所以语义定成：

  * **没有** `manifest.sha256` → 旧安装或未启用门禁：打印 WARN，**放行**；
  * **有** manifest → 必须签名有效 + 逐文件哈希一致，任何不符**拒绝启动**。

最后一条是这条门禁的全部价值：`manifest 存在` 就是产物在声明「我受门禁保护」，
此后任何改动都必须重新签名。攻击者若删掉 manifest 来绕过，则 nginx 服务的仍是
被改过的文件，但 `manifest 缺失` 会在启动日志里留 WARN——运维侧可见。

## 用法

    python deploy/web_preflight.py                 # ExecStartPre 调用，失败即非零退出
    python deploy/web_preflight.py --root DIR      # 对任意目录演练（发布流程可先用它预检）
    python deploy/web_preflight.py --quiet         # 只在出错时输出

退出码：0 通过/跳过，2 信任锚缺失，3 签名失败，4 manifest 格式或哈希不符。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

DEFAULT_ROOT = '/opt/workbuddy-manager/web/out'
TRUST = Path('/opt/workbuddy-manager/release-trust-ed25519.pem')
MANIFEST_NAME = 'manifest.sha256'
SIGNATURE_NAME = 'manifest.sig'
SHA_RE = re.compile(r'^[0-9a-f]{64}$')
#: Next 静态导出会在 index.html 里嵌入 `<!--<buildId>-->`；用于发现「文件混装」
BUILD_ID_COMMENT = re.compile(rb'<!--([A-Za-z0-9_-]{8,64})-->')


def fail(code: int, message: str) -> 'NoReturn':  # noqa: F821
    sys.stderr.write('ABORT: %s\n' % message)
    raise SystemExit(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_signature(manifest: Path, signature: Path) -> None:
    if not TRUST.is_file() or TRUST.is_symlink():
        fail(2, 'ed25519 信任锚缺失或为软链：%s' % TRUST)
    if signature.is_symlink() or not signature.is_file():
        fail(3, '签名文件缺失或为软链：%s（manifest 存在即表示产物受门禁保护）' % signature)
    proc = subprocess.run(
        ['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(TRUST), '-rawin',
         '-in', str(manifest), '-sigfile', str(signature)],
        capture_output=True, timeout=30)
    if proc.returncode:
        fail(3, '前端产物签名校验失败：%s（产物被改动过，或签名与 manifest 不配套）' % manifest)


def parse_manifest(text: str) -> list[tuple[str, int, str]]:
    """解析 `release-web-v2.sh` 的 make_manifest 输出：`<sha256>  <size>  <path>`。"""
    entries: list[tuple[str, int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split('  ')
        if len(parts) != 3:
            fail(4, '%s 第 %d 行不是三段式（sha  size  path）：%r' % (MANIFEST_NAME, lineno, raw[:120]))
        digest, size, relative = parts
        if not SHA_RE.match(digest):
            fail(4, '%s 第 %d 行的 sha256 非法：%r' % (MANIFEST_NAME, lineno, digest[:80]))
        try:
            bytes_ = int(size)
        except ValueError:
            fail(4, '%s 第 %d 行的 size 非法：%r' % (MANIFEST_NAME, lineno, size[:40]))
        entries.append((digest, bytes_, relative))
    if not entries:
        fail(4, '%s 没有任何条目（空 manifest 不能作为「已校验」的依据）' % MANIFEST_NAME)
    return entries


def check_entries(root: Path, entries: list[tuple[str, int, str]]) -> int:
    resolved_root = root.resolve()
    checked = 0
    for digest, size, relative in entries:
        parts = PurePosixPath(relative)
        if parts.is_absolute() or '..' in parts.parts or '\\' in relative:
            fail(4, 'manifest 里的路径不安全：%r' % relative)
        path = root / relative
        # 软链必须拒绝：release-web-v2.sh 的旧版本静态兼容软链**不进 manifest**
        # （make_manifest 用 `find -type f`），所以这里遇到软链就是异常。
        if path.is_symlink():
            fail(4, '产物文件是软链（manifest 只应包含常规文件）：%s' % relative)
        if not path.is_file():
            fail(4, '产物文件缺失：%s' % relative)
        if not path.resolve().is_relative_to(resolved_root):
            fail(4, '产物文件逃出 web 根目录：%s' % relative)
        actual_size = path.stat().st_size
        if actual_size != size:
            fail(4, '产物文件大小不符：%s（manifest %d / 实际 %d）' % (relative, size, actual_size))
        if sha256_file(path) != digest:
            fail(4, '产物文件哈希不符：%s（内容被替换）' % relative)
        checked += 1
    return checked


def check_build_id(root: Path) -> str:
    """buildId 自洽的**软检查**：index.html 里嵌入的 buildId 应与 `_next/` 下那个目录一致。

    不一致意味着「HTML 与资源来自两次不同构建」——页面会 404 或行为错乱。
    只做软检查（找不到注释就跳过）：注释是 Next 当前版本的行为，不是协议。
    """
    index = root / 'index.html'
    if not index.is_file():
        fail(4, 'web 根目录下没有 index.html：%s' % root)
    next_dir = root / '_next'
    if not next_dir.is_dir():
        fail(4, 'web 根目录下没有 _next/：%s' % root)
    candidates = [d.name for d in next_dir.iterdir()
                  if d.is_dir() and d.name != 'static' and re.match(r'^[A-Za-z0-9_-]{8,64}$', d.name)]
    if len(candidates) != 1:
        fail(4, '_next/ 下的 buildId 目录不唯一（找到 %d 个：%s）'
             % (len(candidates), ','.join(sorted(candidates)) or 'none'))
    build_id = candidates[0]
    found = BUILD_ID_COMMENT.search(index.read_bytes()[:4096])
    if found:
        embedded = found.group(1).decode()
        # Next 在 HTML 注释里把 buildId 的 `-` 写成 `_`（base64url 与 base64 的字母表
        # 差异）。2026-10-27 首次带 `-` 的构建（eJGJ-S0B…）就是被这一条挡住切链的：
        # 门禁的拦截行为是对的（宁可不切），但判据过严——`-`/`_` 之差是编码差异，
        # 不是「不同构建」。归一化后再比，真正的混装（buildId 完全不同）仍会被抓。
        if embedded.replace('-', '_') != build_id.replace('-', '_'):
            fail(4, 'index.html 嵌入的 buildId(%s) 与 _next/%s 不一致——HTML 与资源来自不同构建'
                 % (embedded, build_id))
    return build_id


def check(root: Path, quiet: bool = False) -> int:
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        if not quiet:
            print('WARN: 前端产物没有 %s（未启用前端门禁，或为旧安装）：%s'
                  % (MANIFEST_NAME, root))
        return 0
    verify_signature(manifest, root / SIGNATURE_NAME)
    entries = parse_manifest(manifest.read_text(encoding='utf-8'))
    checked = check_entries(root, entries)
    build_id = check_build_id(root)
    if not quiet:
        print('web asset preflight passed: %d files; buildId=%s; ed25519 channel' % (checked, build_id))
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='已发布前端产物的 Ed25519 门禁')
    parser.add_argument('--root', default=DEFAULT_ROOT)
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        fail(4, 'web 根目录不存在：%s' % root)
    check(root, quiet=args.quiet)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
