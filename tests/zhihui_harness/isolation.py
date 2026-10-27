"""隔离测试环境：临时根、环境变量、受保护路径守卫、只读测试视图。

设计约束（来自审计包 02 §4 T02 与 04 §7）：

1. 每次运行创建**全新临时根**，DB / users / auths / config / logs / locks /
   TEMP / HOME 全部落在该根下，运行结束可保留诊断或整树删除；
2. 默认把 ``TEMP`` / ``HOME`` / ``XDG_*`` 一并重定向，避免测试写用户目录；
3. 业务源码**只读**：需要补资产（web/dev/docs 等）时把源码复制进临时视图，
   绝不在 ``manager/``、``qtg/`` 里落任何字节；
4. 受保护路径（``/opt``、``/var``、``/etc``、``/root``、``/tmp``、``/home``、
   生产库与仓库内 ``manager/data``）一律拒绝写入，任何命中都算测试失败；
5. 不引入第三方依赖，只用标准库。

本模块只被 ``run_tests.py`` 与 pytest 插件使用，不进入业务代码的 import 链。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import uuid
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 受保护路径：这些位置一旦被写，立即判失败（T02 第 5 条）
# Windows 下 ``/opt/x`` 会被解析成当前盘的 ``\opt\x``，所以按“第一级目录名”判断
# ---------------------------------------------------------------------------
_SENSITIVE_TOP_DIRS = {
    "opt", "var", "etc", "root", "tmp", "home", "srv", "usr", "bin", "sbin",
    "boot", "proc", "sys", "dev", "mnt", "media", "run", "snap",
}
# 路径里出现即视为生产资产（无论盘符）
_PROD_MARKERS = ("workbuddy-manager", "workbuddy2api", "zhihui-prod", "workbuddy-web")

# 需要从测试视图里排除的目录/文件
_EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".next",
                 ".venv", "venv", ".mypy_cache", ".ruff_cache", ".tox"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")
# dev/ 里只是历史截图与状态快照，与“工具链代码”无关，复制时排除以控制体积
_DEV_EXCLUDE_PREFIXES = (".shots-", ".dash-", ".state-", ".palette", ".update-clear")
# manager 部署副本里不该进视图的数据目录
_MANAGER_EXCLUDE_NAMES = {"data", ".pytest_cache"}


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def protected_reason(path: Path | str, iso_root: Path | str | None = None) -> str | None:
    """返回该路径“为什么受保护”，None 表示可写。

    ``iso_root`` 非空时，落在隔离根内的路径一律放行：测试会在临时目录里**模拟**
    部署包（如 ``workbuddy-manager-v9.9.9/server/main.py``），那是合法测试数据，
    不能因为名字里带生产标记就拦——真正要防的是写到隔离根之外的 /opt、仓库
    manager/data 等真实位置。
    """
    raw = str(path)
    p = Path(raw)
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        resolved = p
    if iso_root is not None and _under(resolved, Path(iso_root)):
        return None
    if resolved == Path(os.devnull).resolve():
        # pytest/logging/subprocess use this exact sink; other /dev paths stay protected.
        return None
    # 隔离 VM 的专用工作区：`/home/gateway-lab/lab` 整个子树都是**合成数据**
    # （快照、identity-run、venv、artifact），不是生产。旧逻辑把 `/home` 整体
    # 列为系统目录，导致 Manager 在启动期写 `snapshots/<bid>/workspace/manager/data/
    # .budget-owners/*` 被拦（budget.recover → budgetowner.identity），fixture 起不来。
    # 只放行这一个具体前缀，其它 /home 路径仍然受保护。
    if _under(resolved, Path('/home/gateway-lab/lab')):
        return None
    low = resolved.as_posix().lower()

    for marker in _PROD_MARKERS:
        if marker in low:
            return f"命中生产资产标记 {marker}"
    parts = [seg for seg in low.split("/") if seg]
    # Windows has a drive component; POSIX starts with the system directory.
    top = parts[1] if len(parts) >= 2 and parts[0].endswith(':') else (parts[0] if parts else '')
    if top in _SENSITIVE_TOP_DIRS:
        return f"位于系统目录 /{top}"
    return None


def is_loopback(host: str) -> bool:
    """判断主机名/IP 是否本机回环（假 provider 只能跑在回环上）。"""
    if not host:
        return False
    host = str(host).strip("[]").lower()
    if host in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    if host in {"0.0.0.0", "::"}:
        # 这是绑定地址；作为“连接目标”时并非回环，但本地服务常用，放行并记录
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


# ---------------------------------------------------------------------------
# 隔离环境
# ---------------------------------------------------------------------------
@dataclass
class IsolatedEnv:
    root: Path
    repo: Path
    keep: bool = False
    created_at: float = field(default_factory=time.time)
    run_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    manifest: dict[str, Any] = field(default_factory=dict)

    # -- 目录 ------------------------------------------------------------
    def d(self, name: str) -> Path:
        return self.root / name

    def ensure_dirs(self) -> None:
        for name in ("db", "data", "auths", "config", "logs", "locks", "tmp",
                     "home", "view", "artifacts", "cache"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    # -- 环境变量 --------------------------------------------------------
    def build_env(self, fake_provider: Any = None, extra: dict[str, str] | None = None) -> dict[str, str]:
        """构造子进程/当前进程使用的完整环境变量表。"""
        root = str(self.root)
        env: dict[str, str] = dict(os.environ)  # 继承 PATH / SystemRoot / COMSPEC 等
        # -- 业务路径：全部落到临时根 --
        env.update({
            "WB_DATA_DIR": str(self.d("data")),
              "WB_BACKUP_STATUS_FILE": str(self.d("data") / "backup-status.json"),
            "WB_DB": str(self.d("db") / "manager.db"),
            "WB_USERS_FILE": str(self.d("config") / "users.json"),
            "WB_AUTH_DIR": str(self.d("auths")),
            "WB_UPSTREAM_CONFIG": str(self.d("config") / "upstream.json"),
            "WB_UPSTREAM_DIR": str(self.d("config")),
            "WB_STATIC_DIR": str(self.d("view") / "manager" / "web" / "out"),
            "WB_HTTP_PROXY": "",
            "WB_DEFAULT_ACCOUNT_PROXY": "",
            "WB_PROXY_PROBE_URL": "http://127.0.0.1:1/__health",
            "WB_TRUST_PROXY": "0",
            "WB_ENABLE_DOCS": "0",
            "WB_LOCAL_IMPORT": "0",
            "WB_SESSION_DAYS": "1",
            "WB_BASE_PATH": "",
            "WB_MANAGER_PORT": "0",
            "WB_MANAGER_HOST": "127.0.0.1",
        })
        # -- 临时目录 / 用户目录重定向 --
        env.update({
            "TMP": str(self.d("tmp")),
            "TEMP": str(self.d("tmp")),
            "TMPDIR": str(self.d("tmp")),
            "HOME": str(self.d("home")),
            "USERPROFILE": str(self.d("home")),
            "XDG_CACHE_HOME": str(self.d("cache")),
            "XDG_CONFIG_HOME": str(self.d("home") / ".config"),
            "XDG_DATA_HOME": str(self.d("home") / ".local" / "share"),
        })
        # -- 代理：显式清空，禁止任何外网代理出站 --
        for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[k] = ""
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
        # -- Python 行为 --
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONHASHSEED": "0",
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        })
        # -- 隔离标记：pytest 插件据此判断“确实在隔离器下运行” --
        env.update({
            "ZH_ISO_ROOT": root,
            "ZH_ISO_RUN_ID": self.run_id,
            "ZH_ISO_REPORT": str(self.d("artifacts") / "isolation-report.json"),
            "ZH_NET_POLICY": os.environ.get("ZH_NET_POLICY", "block"),
            "ZH_FAKE_PROVIDER_BASE": "",
            "ZH_FAKE_PROVIDER_PORT": "",
            "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
            "OPENAI_API_KEY": "sk-fake-for-isolated-tests-only",  # 假 key，非可用凭据
        })
        if fake_provider is not None:
            port = getattr(fake_provider, "port", 0)
            env["ZH_FAKE_PROVIDER_BASE"] = getattr(fake_provider, "base_url", "")
            env["ZH_FAKE_PROVIDER_PORT"] = str(port)
            env["OPENAI_BASE_URL"] = getattr(fake_provider, "openai_base_url", "")
            env["WB_PROXY_PROBE_URL"] = f"http://127.0.0.1:{port}/__health"
        env.update(extra or {})
        return env

    # -- 校验 ------------------------------------------------------------
    def validate(self, env: dict[str, str]) -> list[str]:
        """校验隔离配置；返回问题列表（空 = 合格）。"""
        problems: list[str] = []
        must_be_inside = (
            "WB_DATA_DIR", "WB_DB", "WB_USERS_FILE", "WB_AUTH_DIR",
            "WB_UPSTREAM_CONFIG", "WB_UPSTREAM_DIR", "TMP", "TEMP",
            "HOME", "USERPROFILE",
        )
        for key in must_be_inside:
            value = env.get(key, "")
            if not value:
                problems.append(f"{key} 未设置")
                continue
            if not _under(Path(value), self.root):
                problems.append(f"{key}={value} 不在隔离根 {self.root} 内")
            reason = protected_reason(value, iso_root=self.root)
            if reason:
                problems.append(f"{key}={value} 命中受保护路径（{reason}）")
        for key in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            value = env.get(key, "")
            if value and not _under(Path(value), self.root):
                problems.append(f"{key}={value} 不在隔离根内")
        return problems

    # -- 视图 ------------------------------------------------------------
    def build_view(self, name: str, source: Path, extra_copies: dict[str, Path] | None = None,
                   exclude_extra: set[str] | None = None) -> Path:
        """把源码树复制成临时视图（只读源，写只落临时根）。"""
        target = self.d("view") / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        skip = set(_EXCLUDE_DIRS) | set(exclude_extra or ())

        def _ignore(directory: str, names: list[str]) -> list[str]:
            out: list[str] = []
            base = Path(directory).name
            for n in names:
                if n in skip:
                    out.append(n)
                elif n.endswith(_EXCLUDE_SUFFIXES):
                    out.append(n)
                elif base == "dev" and any(n.startswith(p) for p in _DEV_EXCLUDE_PREFIXES):
                    out.append(n)
            return out

        shutil.copytree(source, target, ignore=_ignore, symlinks=False,
                        ignore_dangling_symlinks=True)
        for rel, src in (extra_copies or {}).items():
            dest = target / rel
            if dest.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, dest, ignore=_ignore, symlinks=False,
                                ignore_dangling_symlinks=True)
            elif src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        return target

    # -- 报告 ------------------------------------------------------------
    def write_manifest(self, target: str, pytest_exit: int, log_path: str,
                       report_path: str | None = None) -> Path:
        payload = {
            "run_id": self.run_id,
            "target": target,
            "iso_root": str(self.root),
            "repo": str(self.repo),
            "created_at": self.created_at,
            "finished_at": time.time(),
            "pytest_exit": pytest_exit,
            "log_path": log_path,
            "isolation_report": report_path,
            "kept": self.keep,
            "python": sys.version,
            "platform": sys.platform,
        }
        self.manifest = payload
        out = self.d("artifacts") / "run-manifest.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def cleanup(self) -> None:
        if self.keep:
            return
        # Windows 上可能有未释放句柄，重试若干次
        for _ in range(3):
            shutil.rmtree(self.root, ignore_errors=True)
            if not self.root.exists():
                return
            time.sleep(0.3)

    @classmethod
    def create(cls, repo: Path, keep: bool = False, base_dir: str | None = None) -> "IsolatedEnv":
        base = Path(base_dir) if base_dir else Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = Path(tempfile.mkdtemp(prefix=f"zhihui-iso-{stamp}-", dir=str(base)))
        iso = cls(root=root, repo=repo, keep=keep)
        iso.ensure_dirs()
        return iso


# ---------------------------------------------------------------------------
# 资产清单：把完整源码树中“部署副本缺、测试却要读”的目录补进 manager 视图
# ---------------------------------------------------------------------------
MANAGER_ASSET_DIRS = ("web", "dev", "docs")
MANAGER_ASSET_ROOT_FILES = (
    "Dockerfile", "docker-compose.yml", ".dockerignore", ".env.example",
    "README.md", "README.en.md", "CHANGELOG.md", "LICENSE",
    # 测试会读的仓库治理文件（部署副本里同样被裁剪掉了）
    ".gitignore", ".gitattributes",
)
MANAGER_ASSET_ROOT_DIRS = ("windows", ".github")


def manager_asset_copies(src_root: Path) -> dict[str, Path]:
    """返回 {视图内相对路径: 源路径}，缺失的自动跳过。"""
    out: dict[str, Path] = {}
    for name in MANAGER_ASSET_DIRS:
        p = src_root / name
        if p.is_dir():
            out[name] = p
    for name in MANAGER_ASSET_ROOT_FILES:
        p = src_root / name
        if p.is_file():
            out[name] = p
    for name in MANAGER_ASSET_ROOT_DIRS:
        p = src_root / name
        if p.is_dir():
            out[name] = p
    return out
