"""把上游 workbuddy2api 的源码打成「随 Release 分发」的压缩包。

## 为什么有这个脚本

上游源码不再有公开的 git 地址可拉。本项目的公开仓库
**不放上游代码**（它不属于我们），但用户装的时候需要它——所以做法是：

    dev/ 里的这个脚本  →  打成 workbuddy2api-src.tar.gz
                       →  上传到**本仓**固定 tag 的 Release（`upstream-src`）
                       →  同一次运行里写 dev/upstream-source.lock（sha256 + bytes）
                       →  每次面板发版时，CI 从那里取回来，塞进 Release 包里的 upstream/
                       →  install.sh 见到 upstream/ 就直接用（离线可装）
                       →  update.py 同步进 /opt/workbuddy2api（保留 config/auths/data）

于是「上游源码更新」与「面板发版」是一条通道：维护者把新代码放进源码目录、
重跑本脚本（带 `--upload`）、覆盖那个 Release 附件，下一次面板发版就会把新代码
带给所有用户。

**取源只有一处**：`dev/upstream-source.lock`。面板发版（`.github/workflows/
release.yml`）与 Windows 安装器构建（`windows/launcher/build.ps1`）都只读这份
lock，并按它校验 sha256 与字节数。所以「上传新包」与「改 lock」必须是同一次
运行 —— 分成两步就必然出现「资产换了、lock 没换」的窗口，而那个窗口里的发版
会拿旧摘要去校验一个新包，直接红在构建阶段（2026-11-28 的第三次发版就是这么
失败的：安装器有自己的第二份 pin，而第三方载体上的同名文件被换掉了）。

## 用法

    python dev/pack_upstream_src.py <源码目录> [-o 输出目录] [--overlay 补丁目录]
                                   [--stamp 说明文字] [--upload [--repo OWNER/REPO]]

    # 例：把归档里的快照打包，并叠加一份新的签到脚本（只打包，不上传）
    python dev/pack_upstream_src.py "D:/Leo/桌面/workbuddy2api-archive/snapshot" \
        --overlay ./new-scripts --stamp "更新签到脚本：<一句话>"

    # 发布：打包 + 上传到本仓 upstream-src + 同一次运行写好 lock
    python dev/pack_upstream_src.py <源码目录> --stamp "…" --upload

无论传不传 `--upload`，本脚本都会把本次产物的 `sha256=` / `bytes=` 写进
`dev/upstream-source.lock`（其它行原样保留）；`--upload` 只是**额外**把它传到
本仓的 `upstream-src` Release（覆盖同名附件）。手工上传是唯一会绕过 lock 的
路径：真要手工传，传完必须再跑一次本脚本。

输出 `workbuddy2api-src.tar.gz`，内含顶层目录 `workbuddy2api/`（解压时配
`--strip-components=1` 即可，与 install.sh 的约定一致）。

## 打包时做的两件事

1. **排除 `.git`**：源码目录若是 git 仓库（比如归档里那份），不要把版本库带进去
   ——用户不需要，而它会让包大一截、还会让人误以为能从它 `git pull` 更新。
2. **归一化时间戳与顺序**：同名同内容的两次打包产出**逐字节相同**，便于比对
   「这次到底换了什么」。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

TOP = 'workbuddy2api'
# 上游源码包的发布位置：**我们自己的仓库**。第三方载体是 2026-11-28 那次安装器
# 构建失败的根因 —— 那边的同名文件被换成了另一份字节，而我们没有控制权。
DEFAULT_REPO = 'daniei-chen/zhihui-gateway'
RELEASE_TAG = 'upstream-src'
ASSET_NAME = 'workbuddy2api-src.tar.gz'
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / 'dev' / 'upstream-source.lock'

# 打包进公开 Release 的东西必须**只含源码**：部署树的 .env、数据库、日志、Python
# 虚拟环境里全是凭据与运行时垃圾（2026-10-18 审计：旧清单只排除了 config.json /
# auths / data，`.env`、`*.db`、`logs/` 会被原样打进公开资产）。
EXCLUDE_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', '.runtime',
                'logs', 'tmp', 'data', 'auths'}
EXCLUDE_DIR_PREFIXES = ('auths.backup', 'auths.bak', 'backup', 'backups',
                        '.local-config')
EXCLUDE_SUFFIX = {'.pyc', '.pyo', '.log', '.db', '.db-wal', '.db-shm',
                  '.sqlite', '.sqlite3'}
EXCLUDE_NAMES = {'config.json', 'auths', 'data', 'users.json',
                 'update-status.json', 'version-check.json', 'upstream-ref.txt'}


def _lf_text(raw: bytes) -> bytes:
    """文本内容 CRLF → LF；二进制原样返回（issue #202）。

    判据：**不含 NUL 且能按 UTF-8 解码**的当文本，其余（图片、压缩包等）一个字节都
    不动。猜错方向的代价不对称 —— 把一个二进制改坏比留一处行尾问题严重得多。
    与 `deploy/update.py::_normalize_eol` 同一口径（那边是同步时的兜底；这边治源头，
    让下出去的资产本身就是干净的）。
    """
    if b'\x00' in raw:
        return raw
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw
    return raw.replace(b'\r\n', b'\n')


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob('*')):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS or part.startswith(EXCLUDE_DIR_PREFIXES)
               for part in rel.parts):
            continue
        name = rel.name
        if (name.startswith('.env')
                or name.startswith('config.json.bak')
                or name.startswith('config.json.pre')
                or name.startswith('auths.backup')
                or name.startswith('auths.bak')
                or name.endswith(('.bak', '.backup', '.old', '.orig', '~'))
                or any(marker in name for marker in ('.bak-', '.backup-',
                                                      '.old-', '.orig-'))):
            continue
        if p.suffix in EXCLUDE_SUFFIX:
            continue
        # 只排除**顶层**的运行时文件/目录：源码树里若有 scripts/config.json 之类的
        # 示例文件，那是要保留的。`.env*` 一律排除（含 .env.local 这类备份）。
        top_name = rel.parts[0]
        if len(rel.parts) == 1 and (
                top_name in EXCLUDE_NAMES
                or top_name.startswith('.env')
                or top_name.startswith('config.json.')
                or top_name.startswith('config.json-')
                or top_name.startswith('auths.backup')
                or top_name.startswith('auths.bak')
                or top_name.endswith(('.bak', '.backup', '.old', '.orig', '~'))
                or any(marker in top_name for marker in ('.bak-', '.backup-',
                                                          '.old-', '.orig-'))):
            continue
        out.append(p)
    return out


def tree_id(root: Path) -> str:
    """源码树的稳定标识：路径 + 内容哈希（排除了会随时间变化的说明文件）。

    写进包内 `.upstream-id`，面板用它判断「这次 Release 带的上游与本机是否
    同一份」——没有它就只能比较版本号，而版本号在纯上游修复时不会变。
    """
    h = hashlib.sha256()
    for f in _iter_files(root):
        rel = f.relative_to(root).as_posix()
        if rel in ('UPSTREAM-SRC.txt', '.upstream-id'):
            continue
        h.update(rel.encode('utf-8')); h.update(b'\0')
        h.update(f.read_bytes()); h.update(b'\0')
    return h.hexdigest()


def write_lock(path: Path, digest: str, size: int) -> None:
    """把本次产物的 `sha256=` / `bytes=` 写进 lock，其余行原样保留。

    lock 是上游源码的唯一真源（release.yml 与 build.ps1 都只认它），所以「打包」
    与「改 lock」必须在同一次运行里完成 —— 否则就留出了一个「资产换了、lock 还是
    旧值」的窗口，而那个窗口里的发版会拿旧摘要校验新包。
    """
    if path.is_file():
        lines = path.read_text(encoding='utf-8').splitlines()
    else:
        lines = [
            '# The upstream-src asset is a release input, not a floating download.',
            '# Update this file only after reviewing the new snapshot and its patch set.',
            f'release={RELEASE_TAG}',
            f'asset={ASSET_NAME}',
        ]
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split('=', 1)[0].strip()
        if key == 'sha256':
            out.append(f'sha256={digest}')
            seen.add('sha256')
        elif key == 'bytes':
            out.append(f'bytes={size}')
            seen.add('bytes')
        else:
            out.append(line)
    if 'sha256' not in seen:
        out.append(f'sha256={digest}')
    if 'bytes' not in seen:
        out.append(f'bytes={size}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(out) + '\n', encoding='utf-8')


def upload_command(repo: str, path: Path) -> list[str]:
    """真正要执行的上传命令 —— `--dry-run` 打印的就是它，只此一处，不许平行实现。"""
    return ['gh', 'release', 'upload', repo, RELEASE_TAG, str(path), '--clobber']


def upload_asset(repo: str, path: Path) -> int:
    """把包传到**本仓** `upstream-src` Release 的固定附件名上（覆盖）。"""
    cmd = upload_command(repo, path)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
    except FileNotFoundError:
        print('[✗] 找不到 gh（GitHub CLI）：装好并 `gh auth login` 之后再带 --upload。',
              file=sys.stderr)
        return 3
    if done.returncode != 0:
        print(f'[✗] 上传失败（{" ".join(cmd)}）：{done.stderr.strip()}', file=sys.stderr)
        print('    lock 未改动：远端还是旧包，两者仍然一致。修好网络/权限后重跑。',
              file=sys.stderr)
        return 3
    return 0


def publish(args, out_path: Path, digest: str, size: int) -> int:
    """上传（可选）与写 lock 的**唯一**入口。

    两个动作放在同一个函数体里，是为了让「上传了但 lock 没改」这种组合在结构上
    不存在：任何一条失败都在 `write_lock()` 之前 return，lock 保持原样（远端也
    还是旧包，两者仍然一致）。`--dry-run` 走同一段逻辑，只打印不执行、不写文件。
    """
    if args.dry_run:
        if args.upload:
            print('将执行：' + ' '.join(upload_command(args.repo, out_path)))
            if shutil.which('gh') is None:
                print('  注意：本机 PATH 里没有 gh，实跑会在这一步失败（--dry-run 不调用它）。')
        print(f'将写入 {args.lock}：')
        print(f'  sha256={digest}')
        print(f'  bytes={size}')
        print('--dry-run：没有上传、没有改 lock。')
        return 0

    if args.upload:
        rc = upload_asset(args.repo, out_path)
        if rc != 0:
            return rc
        print(f'已上传 {ASSET_NAME} 到 {args.repo} 的 {RELEASE_TAG} Release。')
    else:
        print('未上传（没有 --upload）。发布时用同一条命令加 --upload：')
        print('  上传与改 lock 必须是同一次运行；手工上传之后要再跑一遍本脚本。')
        print('  这个包是我们的源码快照，只发到本仓，不传到第三方仓库。')

    write_lock(args.lock, digest, size)
    print(f'已更新 {args.lock}：sha256={digest} bytes={size}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='打上游源码包（随 Release 分发）')
    ap.add_argument('src', type=Path, help='上游源码目录')
    ap.add_argument('-o', '--out', type=Path, default=Path('.'),
                    help='输出目录（默认当前目录）')
    ap.add_argument('--overlay', type=Path, action='append', default=[],
                    help='要先叠加进来的补丁目录（可多次）；同路径文件以补丁为准')
    ap.add_argument('--stamp', default='', help='写进包内 UPSTREAM-SRC.txt 的说明')
    ap.add_argument('--lock', type=Path, default=DEFAULT_LOCK,
                    help='要更新的锁文件（默认 dev/upstream-source.lock）')
    ap.add_argument('--upload', action='store_true',
                    help=f'打包后传到 {DEFAULT_REPO} 的 {RELEASE_TAG} Release（覆盖同名附件）')
    ap.add_argument('--repo', default=DEFAULT_REPO,
                    help=f'--upload 的目标仓库（默认 {DEFAULT_REPO}；只接受本仓）')
    ap.add_argument('--dry-run', action='store_true',
                    help='只打印将要执行的上传命令与将要写入 lock 的内容；不调用 gh、不改 lock')
    args = ap.parse_args()

    if args.repo != DEFAULT_REPO:
        print(f'[✗] 只允许发到本仓 {DEFAULT_REPO}，不接受 {args.repo}', file=sys.stderr)
        print('    这个包是我们的源码快照，传到第三方仓库等于把发布权交出去。',
              file=sys.stderr)
        return 2

    src: Path = args.src
    if not (src / 'docker-compose.yml').is_file():
        print(f'[✗] {src} 看起来不是上游源码根目录（没有 docker-compose.yml）',
              file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / TOP
        shutil.copytree(src, work, ignore=shutil.ignore_patterns(*EXCLUDE_DIRS))
        for rel in sorted(EXCLUDE_NAMES):
            p = work / rel
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink()
        overlaid: list[str] = []
        for ov in args.overlay:
            if not ov.is_dir():
                print(f'[✗] 补丁目录不存在：{ov}', file=sys.stderr)
                return 2
            for f in sorted(ov.rglob('*')):
                if not f.is_file():
                    continue
                rel = f.relative_to(ov)
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
                overlaid.append(str(rel))
        print(f'叠加补丁 {len(overlaid)} 个文件' if overlaid else '无补丁')

        (work / 'UPSTREAM-SRC.txt').write_text(
            f"""本目录是上游 workbuddy2api 的源码快照，随本项目的 Release 一同分发。

上游原仓库（github.com/Sliverkiss/workbuddy2api）已被其作者删除；源码由本项目
维护者留存并继续维护。许可为 MIT（见 LICENSE，版权归原作者）——再分发请保留
LICENSE 与版权声明。

打包说明：{args.stamp or '（未填写）'}
打包时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
""", encoding='utf-8')

        (work / '.upstream-id').write_text(tree_id(work) + '\n', encoding='utf-8')

        files = _iter_files(work)
        out_path: Path = args.out / 'workbuddy2api-src.tar.gz'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 归一化：固定 mtime / uid / gid / 排序 → 同内容两次打包逐字节相同
        with tarfile.open(out_path, 'w:gz', format=tarfile.GNU_FORMAT) as tf:
            for f in files:
                rel = f.relative_to(work.parent)
                info = tf.gettarinfo(str(f), arcname=str(rel))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mode = 0o755 if (f.stat().st_mode & 0o111) else 0o644
                # 行尾归一（issue #202）：源码目录可能是在 Windows 上检出的（git 的
                # `core.autocrlf` 会把文本文件写成 CRLF），而打包是字节拷贝 —— 包内
                # 那份 CRLF 会让容器里的脚本首行变成 `#!/usr/bin/env bash\r`，解释器
                # 路径对不上，一键登录 / 签到 / 积分这些依赖脚本的功能整体失效。
                # 归一放在**打包这一步**、而不是指望源码目录干净：谁的工作区干净
                # 不该决定发出去的资产长什么样。判据见 `_lf_text`。
                data = _lf_text(f.read_bytes())
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        raw = out_path.read_bytes()
        size = len(raw)
        full_digest = hashlib.sha256(raw).hexdigest()
        print(f'已打出 {out_path}（{len(files)} 个文件，{size / 1024:.0f} KB，'
              f'sha256:{full_digest[:16]}…）')

        rc = publish(args, out_path, full_digest, size)
        if rc != 0:
            return rc
        if not args.dry_run:
            print('下一步：把 lock 与本次改动一起提交 —— release.yml 与 '
                  'windows/launcher/build.ps1 都只认这份 lock。')
    return 0


if __name__ == '__main__':
    sys.exit(main())

