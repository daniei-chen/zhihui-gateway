#!/usr/bin/env python3
"""统一的发布流水线入口：stage → seal → verify → activate → notes。

## 为什么要有这个文件

2026-11-23 连发三版（1.5.29 / 1.5.30 / 1.5.31），每次都在同样的地方踩坑：

  · manifest 的 `version` 取的是**挂载点目录名**（`/mnt/v1529` → `v1529`），
    启动门禁要求它等于 release 目录名，于是服务被拒启动；
  · 换了前端产物却没重建 `web/out/manifest.sha256`，签名校验失败；
  · `writer-capabilities.json` 的 `version` 被当成产品版本改成字符串，
    而校验要求整数 `1`；
  · 改了受保护算法却没同步 `docs/protected-customizations.json`；
  · `release-provenance.json` 的哈希在 `git add` **之前**取，记成旧值；
  · 切换指针直接 `ln -sfn` 后重启，**没有跑 `preflight-gate.py`**，
    结果线上 15 分钟不可用；
  · 发布说明里的 sha256 手工抄。

其中前六项全部是**可判定的**——也就是说它们本该在切换指针之前被机械地拦住。
这个脚本把「判定」集中到一处，让「照仓库内容发布」变成一条命令。

## 为什么在容器里跑

宿主机的 python 被 guard 拦（`interpreter-inline-code`），而发布镜像里已有
`cryptography`。Ed25519 是确定性签名，自己签与 `openssl pkeyutl -sign -rawin`
产生**相同字节**，所以不必再依赖宿主 openssl（这也顺手解决了「容器里没有
openssl」那个坑）。

## 用法

    release.py stage    v1.5.32 --archive /tmp/v1532.tar.gz --web /tmp/webout-1532.tar.gz
    release.py seal     v1.5.32
    release.py verify   v1.5.32
    release.py activate v1.5.32
    release.py rollback
    release.py notes    v1.5.32 --asset workbuddy-manager-v1.5.32.tar.gz=sha256:... --out notes.md

`activate` 只做文件系统操作（原子切换指针 + 记录上一版），**不重启服务** ——
重启由调用方在宿主执行，因为容器里没有 systemd。切换后若服务起不来，
`rollback` 会把指针切回去。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile

DEFAULT_ROOT = Path("/opt/workbuddy-manager/releases")
DEFAULT_KEY = Path("/opt/zhihui-release/keys/release-priv.pem")
DEFAULT_TRUST = Path("/opt/workbuddy-manager/release-trust-ed25519.pem")

LABEL = re.compile(r"v\d+\.\d+\.\d+")
#: 由 `seal` 自己重写的两个文件：复制上一版时必须排除，否则会把旧值带过来。
SEALED_FILES = {"deploy/managed-release.json", "deploy/managed-release.sig"}
WEB_SEALED = {"manifest.sha256", "manifest.sig"}
#: 归档里允许落地的顶层项。多出来的（例如源码目录）一律忽略，避免把仓库
#: 里的 `web/lib` 之类混进发布树 —— 发布树只需要可运行的那部分。
STAGE_ROOTS = ("server", "deploy", "CHANGELOG.md", "VERSION")
SAFE_MODES = {0o644, 0o755}
#: 必须保持 CRLF 的扩展名。Windows 批处理在 cmd.exe 的 if 块 / goto 标签边界上
#: 有已知的 LF 解析风险，仓库的 `.gitattributes` 也明确写着 `*.cmd text eol=crlf`。
#: 它们不会被 Linux 的 execve 执行，行尾归一只有坏处 —— 2026-11-26 的第一次
#: v1.6.1 stage 就把这两个文件误转成了 LF，靠读 stage 输出才发现。
CRLF_KEEP = {".cmd", ".bat"}


class ReleaseError(RuntimeError):
    """发布流程被拒。消息会直接给运维看，要能照着修。"""


# ── 基础工具 ──────────────────────────────────────────────────────────────

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


def _member_name(member: tarfile.TarInfo) -> str:
    name = member.name
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseError("归档里有越界路径：" + name)
    return name


def extract(tar_path: Path, dest: Path, *, roots=(), strip: int = 0) -> list[str]:
    """安全解包，返回解出来的相对路径（用于决定哪些文件要置为可执行）。

    不用 `tarfile.extractall`：它会照单全收归档里的路径与权限，而发布树的
    权限只有两种合法值（644 / 755），必须由我们自己定。
    """
    written: list[str] = []
    with tarfile.open(tar_path, "r:*") as archive:
        for member in archive.getmembers():
            name = _member_name(member)
            parts = Path(name).parts
            if strip:
                if len(parts) <= strip:
                    continue
                parts = parts[strip:]
                name = Path(*parts).as_posix()
            if roots:
                top = parts[0] if parts else ""
                if top not in roots:
                    continue
            target = dest / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                # 符号链接与设备文件一律不落地：发布树里没有它们的合法用途，
                # 而符号链接正是「指向发布树之外」的常见手法。
                raise ReleaseError("归档里有非普通文件：" + member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseError("无法读取归档成员：" + member.name)
            with target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            written.append(name)
            if member.mode & 0o111:
                os.chmod(target, 0o755)
    return written


def normalize_eol(root: Path, roots: tuple[str, ...] = ("server", "deploy")) -> list[str]:
    """把落地的 `server/` 与 `deploy/` 文本文件行尾归一成 LF，返回被改动的路径。

    ## 为什么 stage 必须做这件事

    2026-11-26：线上 v1.6.0 发布树里有 **525/713 个 `server/*.py` 和 39 个
    `deploy` 脚本是 CRLF**（基线 v1.5.94 是 0 个）。Python 解释器读文件容错，
    所以网关照常服务、巡检也全绿；但**内核直接执行的脚本不容错**：

        #!/opt/workbuddy-manager/venv/bin/python\\r
                                                  ↑ 这个 \\r 让 execve 返回 ENOENT

    `server/backup_application.py` 正是被 recovery 直接执行的入口。于是每天
    04:15 的完整加密快照连续失败，报的却是
    `FileNotFoundError: ... /backup_application.py` —— 看日志像文件丢了，实际
    文件在、只是解释器路径多了一个 \\r。这就是「能跑」与「能执行」的差别。

    ## 为什么归一而不是拒绝

    归档来自 `git archive`，仓库里存的本来就是 LF（`.gitattributes` 里
    `* text=auto eol=lf`）；CRLF 是打包那台机器带进来的。拒绝会让正确的字节
    因为上游的打包方式而不可用，而这台机器没办法让上游重打一次。归一之后
    `seal` 签的是**归一后的字节**，签名、哈希、实际内容三者仍然自洽。

    只动可解码的文本：含 NUL 或不是合法 UTF-8 的一律按二进制原样保留 ——
    把二进制改坏比留一处行尾问题严重得多。Windows 批处理（`.cmd` / `.bat`）
    也跳过：仓库里就要求它们是 CRLF，且不会被 Linux 的 execve 执行。
    """
    changed: list[str] = []
    for name in roots:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() in CRLF_KEEP:
                continue
            raw = path.read_bytes()
            if b"\r\n" not in raw or b"\x00" in raw:
                continue
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            path.write_bytes(raw.replace(b"\r\n", b"\n"))
            changed.append(path.relative_to(root).as_posix())
    return changed


def closure(root: Path) -> dict[str, dict]:
    """`server/` 与 `deploy/` 下的全部文件（排除自己重写的两个清单）。"""
    files: dict[str, dict] = {}
    for name in ("server", "deploy"):
        base = root / name
        if not base.is_dir():
            raise ReleaseError(f"缺目录：{name}")
        for path in sorted(base.rglob("*")):
            if path.is_dir():
                if path.name == "__pycache__":
                    raise ReleaseError("发布树里有 __pycache__：" + str(path))
                continue
            if path.suffix in (".pyc", ".pyo"):
                raise ReleaseError("发布树里有字节码：" + str(path))
            relative = path.relative_to(root).as_posix()
            if relative in SEALED_FILES:
                continue
            files[relative] = {
                "candidate_sha256": digest(path),
                "mode": stat.S_IMODE(path.lstat().st_mode),
            }
    return files


def web_closure(root: Path) -> list[tuple[str, int, str]]:
    """前端闭包：`(相对路径, 字节数, sha256)`，按路径排序。"""
    base = root / "web/out"
    if not base.is_dir():
        raise ReleaseError("缺 web/out")
    entries = []
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(base).as_posix()
        if relative in WEB_SEALED:
            continue
        if path.is_symlink():
            raise ReleaseError("web/out 里有符号链接：" + relative)
        entries.append((relative, path.stat().st_size, digest(path)))
    return entries


# ── 签名 ──────────────────────────────────────────────────────────────────

def _private_key(path: Path):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(path.read_bytes(), password=None)


def _public_key(path: Path):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    return load_pem_public_key(path.read_bytes())


def sign_bytes(data: bytes, key_path: Path) -> bytes:
    """Ed25519 裸签名（64 字节）。

    **不要**改成先 hash 再签：`openssl pkeyutl -sign -rawin` 与这里的
    `private_key.sign(data)` 都是「对原始字节做 Ed25519」，两边必须逐字节一致，
    否则宿主工具验不过我们签的东西。
    """
    return _private_key(key_path).sign(data)


def verify_bytes(data: bytes, signature: bytes, trust_path: Path) -> None:
    from cryptography.exceptions import InvalidSignature
    try:
        _public_key(trust_path).verify(signature, data)
    except InvalidSignature:
        raise ReleaseError("签名校验失败：" + trust_path.as_posix()) from None


# ── 发布元数据自洽 ────────────────────────────────────────────────────────

def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReleaseError("缺文件：" + path.as_posix()) from None
    except ValueError as exc:
        raise ReleaseError(f"{path.as_posix()} 不是合法 JSON：{exc}") from None
    if not isinstance(value, dict):
        raise ReleaseError(path.as_posix() + " 必须是 JSON 对象")
    return value


def check_metadata(root: Path) -> list[str]:
    """发布元数据必须在**六个地方**说同一个版本。

    这六处曾各自漂移过（见模块开头），所以一次性全查、一次全报，
    而不是撞到一个算一个。
    """
    version = root.name
    if not LABEL.fullmatch(version):
        raise ReleaseError(f"目录名必须是 vX.Y.Z 形态：{version!r}")
    product_version = version[1:]
    problems: list[str] = []

    stamp = root / ".version"
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() != version:
        problems.append(f".version={stamp.read_text(encoding='utf-8').strip()!r} != {version!r}")

    release = read_json(root / "server/product-release.json")
    if release.get("version") != product_version:
        problems.append(f"product-release.version={release.get('version')!r} != {product_version!r}")

    writer = read_json(root / "server/writer-capabilities.json")
    if writer.get("release_id") != version:
        problems.append(f"writer-capabilities.release_id={writer.get('release_id')!r} != {version!r}")
    # writer 描述的 `version` 是**描述格式版本**（整数 1），不是产品版本。
    # 被写成 "1.5.27" 这类字符串时启动门禁会拒，且日志只说「unsupported
    # writer descriptor」——这里提前点名。
    if writer.get("version") != 1 or not isinstance(writer.get("version"), int):
        problems.append(f"writer-capabilities.version={writer.get('version')!r}，必须是整数 1")

    provenance = read_json(root / "server/release-provenance.json")
    if provenance.get("version") != product_version:
        problems.append(f"release-provenance.version={provenance.get('version')!r} != {product_version!r}")
    if provenance.get("release_id") != version:
        problems.append(f"release-provenance.release_id={provenance.get('release_id')!r} != {version!r}")

    changelog = root / "CHANGELOG.md"
    if not changelog.is_file():
        problems.append("缺 CHANGELOG.md")
    elif not re.search(rf"(?m)^## \[{re.escape(product_version)}\]", changelog.read_text(encoding="utf-8")):
        problems.append(f"CHANGELOG.md 里没有 ## [{product_version}] 段落")

    if problems:
        raise ReleaseError("发布元数据不自洽：\n  - " + "\n  - ".join(problems))
    return [
        f"version={version}",
        f"product={product_version}",
        f"writer.version={writer['version']}（整数）",
        "changelog 段落存在",
    ]


# ── 子命令 ────────────────────────────────────────────────────────────────

def _previous(root: Path) -> str | None:
    current = root / "current"
    if not current.is_symlink():
        return None
    target = Path(os.readlink(current)).name
    return target if LABEL.fullmatch(target) else None


def cmd_stage(args) -> int:
    root = Path(args.root).resolve()
    target = root / args.version
    if target.exists() or target.is_symlink():
        raise ReleaseError(f"{target} 已存在 —— 换一个版本号，或先删除它")

    base_version = args.base or _previous(root)
    if not base_version:
        raise ReleaseError("找不到上一版：请用 --base 指定（releases/current 不是有效指针）")
    base = root / base_version
    if not base.is_dir():
        raise ReleaseError(f"上一版目录不存在：{base}")

    print(f"== stage {args.version}（基线 {base_version}）==")
    shutil.copytree(
        base, target, symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    # 复制过来的旧清单必须删掉：它们描述的是**上一版**的字节，留着会被
    # `seal` 误认为「已经签好了」，也会让 `verify` 拿旧哈希去核对新内容。
    for name in SEALED_FILES:
        (target / name).unlink(missing_ok=True)
    for name in WEB_SEALED:
        (target / "web/out" / name).unlink(missing_ok=True)

    executable = extract(Path(args.archive), target, roots=STAGE_ROOTS)
    print(f"   server+deploy 落地 {len(executable)} 个文件")

    # 行尾必须在 seal 之前归一：签名与清单哈希都对**这一份字节**负责。
    # 放在这里而不是「假设归档是 LF」，是因为 2026-11-26 的 v1.6.0 就是从
    # Web 工作区打的 CRLF 包，恢复快照因此停摆两天（详见 normalize_eol 文档）。
    normalised = normalize_eol(target)
    if normalised:
        preview = "、".join(normalised[:5]) + ("…" if len(normalised) > 5 else "")
        print(f"   行尾归一 {len(normalised)} 个文件（CRLF → LF）：{preview}")
        print("   来源归档带 CRLF，通常是在 Windows 工作区打的包。仓库里存的是 LF"
              "（.gitattributes 的 eol=lf），打包请用 git archive，"
              "或先确认 git config core.autocrlf。")

    # 前端整目录替换（不是叠加）：混合两代产物会出现「页面引用了不存在的
    # chunk」，而那种错误只在新旧标签页并存时才显形，极难归因。
    web_out = target / "web/out"
    if web_out.exists():
        shutil.rmtree(web_out)
    web_out.mkdir(parents=True)
    if args.web:
        extract(Path(args.web), web_out, strip=1)
        landed = web_closure(target)
        # 前端包是空的 / 不完整 —— 必须在 stage 就拦，不能等到 seal。
        #
        # `seal` 确实会检查「缺 index.html 或 login/index.html」，但那时
        # 归档已经打好了，中间还隔着一个可能被 `activate` 或被人当成正常
        # 的窗口。更要命的是 stage 只打印一行「落地 0 个文件」，没人盯着
        # 输出就漏过去了。
        #
        # 这也不是假想的：本机 `web/out` 现在就是个 94 个目录、0 个文件的
        # 空骨架（`.gitignore` 忽略了 out/，所以不会体现在 git status 里）。
        # 拿它打包必然得到一个没有界面却「发布成功」的版本。
        if not landed:
            raise ReleaseError(
                f"--web {args.web} 解压后 web/out 里一个文件都没有。\n"
                f"最常见的原因是忘了先跑 `npm run build:export`，"
                f"或者 tar 包的第一层不是 out/。\n"
                f"先确认 `web/out/index.html` 真的存在，再重新打包。"
            )
        if not (web_out / "index.html").is_file() or not (web_out / "login" / "index.html").is_file():
            raise ReleaseError(
                f"--web {args.web} 解压出 {len(landed)} 个文件，但缺 "
                f"index.html 或 login/index.html —— 这不是一套完整的 Next 静态导出。"
            )
        print(f"   web/out 落地 {len(landed)} 个文件")

    (target / ".version").write_text(args.version + "\n", encoding="utf-8")

    # 权限只有两种合法值。归档来源（Windows 构建、git archive）带来的 664/777
    # 会被启动门禁拒绝，且报错只说「unsafe release member」。
    fixed = 0
    for base_dir in ("server", "deploy", "web/out"):
        directory = target / base_dir
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_dir():
                mode = 0o755
            else:
                mode = 0o755 if path.lstat().st_mode & 0o111 else 0o644
            if stat.S_IMODE(path.lstat().st_mode) != mode:
                os.chmod(path, mode)
                fixed += 1
    print(f"   权限规范化 {fixed} 项")

    print("   " + " | ".join(check_metadata(target)))
    print("   OK：下一步 release.py seal " + args.version)
    return 0


def cmd_seal(args) -> int:
    root = Path(args.root).resolve()
    target = root / args.version
    if not target.is_dir():
        raise ReleaseError(f"目标不存在：{target}")
    key = Path(args.key)
    trust = Path(args.trust)

    print(f"== seal {args.version} ==")
    print("   " + " | ".join(check_metadata(target)))

    # 前端清单：三段格式 `sha256  字节数  相对路径`，与既有
    # `create-web-manifest.py` 逐字一致（门禁按两段空格切分解析）。
    entries = web_closure(target)
    if not {"index.html", "login/index.html"} <= {name for name, _, _ in entries}:
        raise ReleaseError("web/out 缺 index.html 或 login/index.html")
    content = "".join(f"{value}  {size}  {name}\n" for name, size, value in entries).encode()
    web_manifest = target / "web/out/manifest.sha256"
    web_manifest.write_bytes(content)
    (target / "web/out/manifest.sig").write_bytes(sign_bytes(content, key))
    verify_bytes(content, (target / "web/out/manifest.sig").read_bytes(), trust)
    print(f"   前端清单 {len(entries)} 项，签名已验证")

    files = closure(target)
    manifest = {
        # `version` / `release_id` 必须是**目录名**。用挂载点或临时目录名生成
        # 过一次（`v1529`），服务直接拒绝启动。
        "version": args.version,
        "release_id": args.version,
        "signature_required": True,
        "signature_algorithm": "ed25519",
        "at": "host-staged",
        "database_rollback": False,
        "files": files,
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    manifest_path = target / "deploy/managed-release.json"
    manifest_path.write_bytes(raw)
    (target / "deploy/managed-release.sig").write_bytes(sign_bytes(raw, key))
    verify_bytes(raw, (target / "deploy/managed-release.sig").read_bytes(), trust)
    print(f"   运行清单 {len(files)} 项，签名已验证")

    for path in (web_manifest, target / "web/out/manifest.sig", manifest_path,
                 target / "deploy/managed-release.sig"):
        os.chmod(path, 0o644)
    print("   OK：下一步 release.py verify " + args.version)
    return 0


def check_nginx(site: Path) -> list[str]:
    """站点配置里是否还有余额协议的两段放行。

    这两段是**被 include 进来的**，不在主文件里；从旧备份恢复主文件后它们会静默
    消失，而页面一切正常、只有余额接口回到「未登录」—— 这种「只有第三方调用方
    受害」的失效最难被发现，所以给它一个可执行的检查。
    """
    if not site.is_file():
        raise ReleaseError("找不到站点配置：" + site.as_posix())
    text = site.read_text(encoding="utf-8", errors="replace")
    missing = [f"location = {path}" for path in ("/api/usage/token", "/api/usage/token/")
               if f"location = {path} " not in text and f"location = {path}{{" not in text]
    if missing:
        raise ReleaseError(
            "站点配置缺少余额协议放行（恢复备份后需重新插入）：" + "; ".join(missing))
    return ["余额协议放行已就位"]


def cmd_verify(args) -> int:
    root = Path(args.root).resolve()
    target = root / args.version
    if not target.is_dir():
        raise ReleaseError(f"目标不存在：{target}")

    print(f"== verify {args.version} ==")
    print("   " + " | ".join(check_metadata(target)))

    if getattr(args, "nginx", None):
        print("   " + " | ".join(check_nginx(Path(args.nginx))))

    # 优先用**与本脚本同目录**的校验器，其次才用发布树自带的那份。
    #
    # 理由是顺序性：`preflight-gate.py` 自己也会演进（例如从「只调宿主 openssl」
    # 改成「cryptography 优先」）。若固定用目标树里的旧版，那么「改了校验器」
    # 这一版就永远无法用自己的新逻辑验证自己 —— 演练时正是这么撞上的
    # （旧版硬编码 /usr/bin/openssl，而容器里没有）。
    tool_gate = Path(__file__).resolve().parent / "preflight-gate.py"
    gate = tool_gate if tool_gate.is_file() else target / "deploy/preflight-gate.py"
    if not gate.is_file():
        raise ReleaseError("找不到 deploy/preflight-gate.py，无法复现启动门禁")
    result = subprocess.run([sys.executable, str(gate), str(target)],
                            capture_output=True, text=True,
                            env={**os.environ, "WB_RELEASE_TRUST": args.trust})
    print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.rstrip(), file=sys.stderr)
        raise ReleaseError("启动门禁未通过 —— 不要切换发布指针")
    if "全部通过" not in result.stdout:
        raise ReleaseError("门禁输出缺少「全部通过」，拒绝放行")
    print("   OK：下一步 release.py activate " + args.version)
    return 0


def _switch(pointer: Path, version: str) -> str | None:
    """原子切换指针，返回切换前的目标名（用于回滚）。

    两条都试过才定下来的：

    1. **必须原子**：`ln -sfn` 不是原子的，中间存在「指针不存在」的瞬间，
       那一刻正好有请求进来就会 502。这里先在同一目录建临时链接再 `os.replace`。
    2. **目标必须写相对名**：本工具在容器里跑，`--root` 是容器内的挂载路径
       （`/rel`），而链接是在**宿主**上被解析的。写绝对路径会把
       `/rel/v1.5.32` 烙进符号链接 —— 宿主上没有这个路径，服务立刻起不来。
       相对名（`v1.5.32`）在两侧解析结果一致。这个坑真实踩过。
    """
    previous = _previous(pointer.parent)
    stage = pointer.with_name(f".{pointer.name}.{os.getpid()}.next")
    if stage.exists() or stage.is_symlink():
        stage.unlink()
    stage.symlink_to(version, target_is_directory=True)
    os.replace(stage, pointer)
    return previous


def cmd_activate(args) -> int:
    root = Path(args.root).resolve()
    version = args.version
    target = root / version
    if not target.is_dir():
        raise ReleaseError(f"目标不存在：{target}")
    target_root = target.resolve()

    cmd_verify(argparse.Namespace(root=args.root, version=version, trust=args.trust))

    print(f"== activate {version} ==")
    previous = _switch(root / "current", version)
    # 切换后立刻复核指针：这是唯一能发现「链接写到别处」的时刻。
    target = (root / "current").resolve(strict=True)
    if target != target_root:
        _switch(root / "current", previous or version)
        raise ReleaseError(f"指针复核失败：current 指向 {target}，应为 {target_root}；已切回")
    (root / ".previous").write_text((previous or "") + "\n", encoding="utf-8")
    print(f"   指针 {previous or '(无)'} → {version}（已复核）")
    print("   **下一步必须在宿主执行**（容器里没有 systemd）：")
    print("     sudo systemctl restart workbuddy-web")
    print("     curl -s http://127.0.0.1:7866/api/release")
    print(f"   服务未起或版本不是 {version} 时回滚：release.py rollback")
    return 0


def cmd_rollback(args) -> int:
    root = Path(args.root).resolve()
    stamp = root / ".previous"
    previous = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
    if not previous:
        raise ReleaseError("没有可回滚的目标（releases/.previous 为空）")
    if not (root / previous).is_dir():
        raise ReleaseError(f"回滚目标不存在：{previous}")
    current = _previous(root)
    _switch(root / "current", previous)
    stamp.write_text((current or "") + "\n", encoding="utf-8")

    # 回滚只切指针，**不重启服务**。
    #
    # 这不是疏漏，是刻意的：重启要 sudo、要配合中继重试，而 release.py 只负责
    # 文件与指针，不碰进程。但「只切指针」有一个很危险的副作用 ——
    # 命令返回 0（成功），而**服务还在跑出故障的那个版本**。
    #
    # 2026-11-25 实测：执行 rollback 后指针已指向 v1.5.76，
    # 而 /api/release 仍在报 v1.5.77。如果有谁看到「成功」就去忙别的，
    # 故障会一直继续，而日志里唯一线索只是一行容易被跳过的提示。
    #
    # 所以这里把「还没完」说得足够重，并给出可直接粘贴的下一步与验证方法。
    print(f"== rollback：{current or '(无)'} → {previous} ==")
    print("")
    print("  ** 注意：只切了指针，服务还在跑旧代码。回滚尚未生效。 **")
    print("")
    print("  必须立刻执行下面这条，否则故障会继续：")
    print(f"      cd {root.parent / 'deploy'} && sudo ./rollout-rolling.sh {previous}")
    print("")
    print("  然后验证版本真的变了：")
    print("      curl -s http://127.0.0.1:7866/api/release")
    print(f"  返回的 version 应当是 {previous[1:] if previous.startswith('v') else previous}。")
    print("  如果仍是回滚前的版本，说明服务还没重启。")
    return 0


def cmd_prune(args) -> int:
    """删掉不会再被切回去的旧发布目录。

    为什么需要：`releases/` 每发一版就多一份完整产物（约 41 MB）。2026-11-26
    实测 65 个版本占 2.6 GB，磁盘 74%，而每轮迭代都要发一版 —— 其中绝大多数
    永远不会再被 activate。

    刻意保守，三条都要满足才会删：
      * 不在 `current` 与 `releases/.previous` 里（回滚与 stage 的基线靠它们）；
      * 不在最近 `--keep` 个里（默认 5，按版本号排序）；
      * 名字是 `vN.N.N` 的**实体目录**（符号链接、`.previous`、其他文件一概不碰）。

    默认只预演。真正删除必须显式加 `--apply`。
    """
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise ReleaseError(f"releases 根目录不存在：{root}")
    keep_recent = max(1, int(args.keep))

    def _version_key(path: Path) -> tuple:
        return tuple(int(part) for part in path.name.lstrip("v").split("."))

    current = _previous(root)
    stamp = root / ".previous"
    previous = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""

    candidates = [p for p in root.iterdir()
                  if not p.is_symlink() and p.is_dir() and LABEL.fullmatch(p.name)]
    candidates.sort(key=_version_key)
    protected = {name for name in (current, previous) if name}
    keep = {p.name for p in candidates[-keep_recent:]} | protected
    doomed = [p for p in candidates if p.name not in keep]

    print(f"== prune {root} ==")
    print(f"   共 {len(candidates)} 个版本，保留 {len(keep)} 个"
          f"（最近 {keep_recent} 个 + current + .previous）")
    if not doomed:
        print("   没有可清理的目录")
        return 0

    freed = 0
    for path in doomed:
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        freed += size
        print(f"   {'删除' if args.apply else '可删'}  {path.name}  {size / 1048576:.0f} MB")
    print(f"   合计 {freed / 1048576:.0f} MB")
    if not args.apply:
        print("   这是预演，没有删任何东西。确认无误后加 --apply 执行。")
        return 0
    for path in doomed:
        shutil.rmtree(path)
    print(f"   已删除 {len(doomed)} 个目录，释放约 {freed / 1048576:.0f} MB")
    return 0


def cmd_notes(args) -> int:
    """从 CHANGELOG 生成发布说明。

    只做「搬运 + 拼装」，不重写措辞：措辞维护在 CHANGELOG（那是唯一事实来源），
    发布说明与它不一致的代价是「同一个版本两种说法」。
    """
    root = Path(args.root).resolve()
    version = args.version
    product_version = version[1:]
    changelog = Path(args.changelog) if args.changelog else root / version / "CHANGELOG.md"
    if not changelog.is_file():
        raise ReleaseError("找不到更新日志：" + changelog.as_posix())
    text = changelog.read_text(encoding="utf-8")

    start = re.search(rf"(?m)^## \[{re.escape(product_version)}\].*$", text)
    if not start:
        raise ReleaseError(f"CHANGELOG 里没有 ## [{product_version}]")
    rest = text[start.end():]
    following = re.search(r"(?m)^## \[", rest)
    section = (rest[:following.start()] if following else rest).strip("\n")

    # CHANGELOG 用 `### 新增`，发布说明用 `## 新增`。
    body = re.sub(r"(?m)^### ", "## ", section)
    parts = [f"# 智汇网关 {version}", "", body]

    if args.asset:
        lines = []
        for item in args.asset:
            name, _, value = item.partition("=")
            value = value.removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ReleaseError(f"资产 {name} 的摘要不是 64 位十六进制：{value!r}")
            lines.append(f"{value}  {name}")
        parts += ["", "## 产物校验（sha256）", "", "```", *lines, "```"]

    out = "\n".join(parts) + "\n"
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"已写入 {args.out}（{len(out)} 字节）")
    else:
        print(out)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="releases 根目录")
    parser.add_argument("--key", default=str(DEFAULT_KEY), help="Ed25519 私钥")
    parser.add_argument("--trust", default=str(DEFAULT_TRUST), help="Ed25519 公钥")
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage", help="从上一版复制并覆盖新产物")
    stage.add_argument("version")
    stage.add_argument("--archive", required=True, help="git archive 产物（含 server/deploy）")
    stage.add_argument("--web", help="前端静态导出产物（顶层为 out/）")
    stage.add_argument("--base", help="基线版本（默认取 releases/current）")
    stage.set_defaults(func=cmd_stage)

    seal = sub.add_parser("seal", help="生成并签名运行清单与前端清单")
    seal.add_argument("version")
    seal.set_defaults(func=cmd_seal)

    verify = sub.add_parser("verify", help="复现启动门禁（切换前必跑）")
    verify.add_argument("version")
    verify.add_argument("--nginx", help="站点配置文件（可选，检查余额协议放行是否还在）")
    verify.set_defaults(func=cmd_verify)

    activate = sub.add_parser("activate", help="原子切换发布指针")
    activate.add_argument("version")
    activate.set_defaults(func=cmd_activate)

    rollback = sub.add_parser("rollback", help="切回上一版指针")
    rollback.set_defaults(func=cmd_rollback)

    prune = sub.add_parser("prune", help="清理旧发布目录（默认只预演）")
    prune.add_argument("--keep", type=int, default=5, help="保留最近几个版本（默认 5）")
    prune.add_argument("--apply", action="store_true", help="真的删除（默认只预演）")
    prune.set_defaults(func=cmd_prune)

    notes = sub.add_parser("notes", help="从 CHANGELOG 生成发布说明")
    notes.add_argument("version")
    notes.add_argument("--out", help="输出文件（缺省打印到 stdout）")
    notes.add_argument("--changelog", help="CHANGELOG 路径")
    notes.add_argument("--asset", action="append",
                       help="产物校验行，形如 name=sha256:...（可重复）")
    notes.set_defaults(func=cmd_notes)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as exc:
        print("REJECTED: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
