"""一键隔离测试运行器（Python 入口）。

职责（对应审计包 T02 输出要求）：
  1. 创建全新临时根（DB/users/auths/config/logs/locks/TEMP/HOME 全在其中）；
  2. 启动进程内假 provider，把 ``OPENAI_BASE_URL`` 指向它；
  3. 把业务源码**只读复制**成临时视图，可选补齐 web/dev/docs 等资产；
  4. 用 pytest 插件强制隔离：无 ``ZH_ISO_ROOT`` 拒绝运行、拦外网、拦生产路径写；
  5. 落 junit XML（供分类脚本精确解析）与运行清单，退出码透传 pytest。

用法::

    python tests/zhihui_harness/run_tests.py --target manager
    python tests/zhihui_harness/run_tests.py --target manager-assets
    python tests/zhihui_harness/run_tests.py --target qtg --keep
    python tests/zhihui_harness/run_tests.py --target manager --collect-only

不要直接调用 pytest：插件会以退出码 3 拒绝。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
import platform
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if (TESTS_DIR / '.deps').is_dir():
    sys.path.insert(0, str(TESTS_DIR / '.deps'))

from zhihui_harness import pytest_plugin  # noqa: E402
from zhihui_harness.isolation import (  # noqa: E402
    IsolatedEnv,
    manager_asset_copies,
)
from fake_provider.server import FakeProvider  # noqa: E402

EVIDENCE = REPO / "evidence"

TARGETS: dict[str, dict[str, Any]] = {
    # manager 部署副本原样：无 web/dev/docs，用来复现部署态真实失败
    "manager": {
        "source": "manager",
        "pytest_path": "server/tests",
        "assets": False,
        "description": "Manager 部署副本（server/ 源码 + 测试，无前端资产）",
    },
    # 同一份 manager 源码 + 从完整源码树补 web/dev/docs，用来区分“缺资产”与真实失败
    "manager-assets": {
        "source": "manager",
        "pytest_path": "server/tests",
        "assets": True,
        "description": "Manager + 前端/dev/docs 资产（诊断视图，非部署树）",
    },
    # 完整源码树视角
    "src": {
        "source": "src/three-platform",
        "pytest_path": "server/tests",
        "assets": False,
        "description": "完整源码树 src/three-platform（含 web/dev/docs）",
    },
    "qtg": {
        "source": "qtg",
        "pytest_path": "tests",
        "assets": False,
        "extra_syspath": ["app"],
        "description": "QTG 控制面测试",
    },
}


# ---------------------------------------------------------------------------
# junit 统计
# ---------------------------------------------------------------------------
def parse_junit(path: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "tests": 0, "passed": 0, "failed": 0, "errors": 0,
        "skipped": 0, "xfailed": 0, "xpassed": 0,
        "failures": [], "error_items": [], "skips": [],
    }
    if not path.exists():
        stats["missing"] = True
        return stats
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        stats["parse_error"] = str(exc)
        return stats
    for tc in root.iter("testcase"):
        stats["tests"] += 1
        name = "::".join(x for x in (tc.get("classname", ""), tc.get("name", "")) if x)
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")
        if failure is not None:
            stats["failed"] += 1
            if len(stats["failures"]) < 5000:
                stats["failures"].append({
                    "id": name, "type": failure.get("type", ""),
                    "message": (failure.get("message") or "").strip()[:800],
                    "text": (failure.text or "").strip()[:2000],
                })
        elif error is not None:
            stats["errors"] += 1
            if len(stats["error_items"]) < 5000:
                stats["error_items"].append({
                    "id": name, "type": error.get("type", ""),
                    "message": (error.get("message") or "").strip()[:800],
                    "text": (error.text or "").strip()[:2000],
                })
        elif skipped is not None:
            stats["skipped"] += 1
            if len(stats["skips"]) < 2000:
                stats["skips"].append({"id": name,
                                       "reason": (skipped.get("message") or "").strip()[:300]})
        else:
            stats["passed"] += 1
    return stats


def summarize_stats(stats: dict[str, Any]) -> str:
    return (f"tests={stats['tests']} passed={stats['passed']} failed={stats['failed']} "
            f"errors={stats['errors']} skipped={stats['skipped']}")


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------
def prepare_view(iso: IsolatedEnv, target: str) -> Path:
    cfg = TARGETS[target]
    source = REPO / cfg["source"]
    if not source.is_dir():
        raise SystemExit(f"源码目录不存在: {source}")
    name = f"{target}-view"
    extra: dict[str, Path] = {}
    if cfg.get("assets"):
        src_root = REPO / "src" / "three-platform"
        if not src_root.is_dir():
            raise SystemExit(f"补资产需要完整源码树，但不存在: {src_root}")
        extra = manager_asset_copies(src_root)
    view = iso.build_view(name, source, extra_copies=extra)
    if cfg.get("assets"):
        iso.manifest.setdefault("asset_copies", sorted(extra))
    return view


def qtg_path_env(iso: IsolatedEnv, config_py: Path) -> dict[str, str]:
    """为 QTG 注入 Windows 绝对路径。

    QTG 的 ``_abs()`` 只接受绝对路径，而默认值全是 ``/opt/...``；Windows 上
    ``Path('/opt/x').is_absolute()`` 为 False，直接 import ``qtg.config`` 就抛
    ValueError（平台差异，不是业务缺陷）。这里静态解析 ``Settings`` 字段默认值，
    把每个含 ``/opt`` 的路径映射到隔离根下，完全落在临时目录内。
    """
    out: dict[str, str] = {}
    if not config_py.is_file():
        return out
    tree = ast.parse(config_py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "Settings"):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            name = stmt.target.id
            value = stmt.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            if "/opt" not in value.value:
                continue
            if "," in value.value:
                # 逗号分隔的多个绝对路径（如 extra_password_files）
                parts = [str(iso.root / "qtg" / "secrets" / f"extra{i}")
                         for i in range(value.value.count(",") + 1)]
                out[f"QTG_{name.upper()}"] = ",".join(parts)
            else:
                out[f"QTG_{name.upper()}"] = str(iso.root / "qtg" / name)
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def view_manifest(view: Path) -> dict[str, str]:
    entries = {}
    for path in sorted(view.rglob('*')):
        relative = path.relative_to(view)
        if any(part in {'__pycache__', 'node_modules', '.git'} for part in relative.parts):
            continue
        name = relative.as_posix()
        if path.is_symlink():
            entries[name] = 'symlink:' + os.readlink(path)
        elif path.is_file():
            entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            entries[name] = 'directory'
    return entries


def manifest_digest(entries: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def view_digest(view: Path) -> str:
    return manifest_digest(view_manifest(view))


def run_one(target: str, args: argparse.Namespace, iso: IsolatedEnv,
            fake: FakeProvider) -> int:
    cfg = TARGETS[target]
    view = prepare_view(iso, target)

    extra_env: dict[str, str] = {}
    if target != 'qtg':
        # The view name is target-specific; never point at a nonexistent sibling.
        extra_env['WB_STATIC_DIR'] = str(view / 'web' / 'out')
    if getattr(args, 'require_built_web', False):
        if not (view / 'web' / 'out' / 'index.html').is_file():
            raise SystemExit('Release verification requires the real built web/out artifact')
    if target == "qtg":
        extra_env.update(qtg_path_env(iso, view / "app" / "qtg" / "config.py"))
        for key, value in extra_env.items():
            for piece in value.split(","):
                if not str(Path(piece).resolve()).startswith(str(iso.root.resolve())):
                    raise SystemExit(f"[ZH-ISO] {key} 未落在隔离根内: {piece}")

    env = iso.build_env(fake_provider=fake, extra=extra_env)
    extra_syspath: list[str] = []
    if target == "manager":
        # 关键 fixture 修复：把完整源码树的 dev 包挂上（test_upstream_patches 收集失败）
        dev_root = REPO / "src" / "three-platform"
        if dev_root.is_dir():
            extra_syspath.append(str(dev_root))
    for rel in cfg.get("extra_syspath", []):
        extra_syspath.append(str(view / rel))
    if extra_syspath:
        env["ZH_EXTRA_SYSPATH"] = os.pathsep.join(extra_syspath)

    problems = iso.validate(env)
    if problems:
        print("[ZH-ISO] 隔离环境校验失败，拒绝运行：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 3

    # 生效到当前进程（pytest 将会在本进程内运行）
    os.environ.update(env)
    sys.dont_write_bytecode = True
    # 关键：tempfile.gettempdir() 会被缓存。IsolatedEnv.create 之前已经调用过一次，
    # 缓存里是系统 TEMP；不覆盖的话所有 TemporaryDirectory 都落在隔离根之外，
    # 既不受隔离根清理，也会被路径守卫误判。这里强制指向隔离根。
    tempfile.tempdir = str(iso.d("tmp"))

    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = EVIDENCE / 'runs' / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    junit = run_dir / f"junit-{target}.xml"
    # Pytest writes only inside the isolation root, including JUnit. Copy the
    # finalized report out after guards are uninstalled by pytest_unconfigure.
    isolated_junit = iso.d('artifacts') / run_id / junit.name
    isolated_junit.parent.mkdir(parents=True, exist_ok=False)
    test_paths = getattr(args, 'test_path', None) or [cfg['pytest_path']]
    for item in test_paths:
        candidate = (view / item.split('::', 1)[0]).resolve()
        if not candidate.is_relative_to(view.resolve()) or not candidate.exists():
            raise SystemExit(f'Test path must exist inside the isolated target: {item}')

    pytest_args = [
        *test_paths,
        "-p", "no:cacheprovider",
        "--tb=short",
        "-q",
        "--continue-on-collection-errors",
        f"--junitxml={isolated_junit}",
    ]
    if args.collect_only:
        pytest_args.append("--collect-only")
    if getattr(args, 'verbose', False):
        pytest_args.append('-vv')
    pytest_args.extend(args.pytest_arg or [])

    print(f"[ZH-ISO] target={target}  {cfg['description']}")
    print(f"[ZH-ISO] 隔离根: {iso.root}")
    print(f"[ZH-ISO] 视图:   {view}")
    print(f"[ZH-ISO] 假上游: {fake.base_url}  (OPENAI_BASE_URL={env['OPENAI_BASE_URL']})")
    print(f"[ZH-ISO] 网络策略: {env['ZH_NET_POLICY']}；附加 sys.path: {extra_syspath or '无'}")
    print(f"[ZH-ISO] pytest: {sys.executable} -m pytest {' '.join(pytest_args)}", flush=True)

    input_manifest = view_manifest(view)
    input_digest = manifest_digest(input_manifest)
    (run_dir / 'view-before.json').write_text(json.dumps(input_manifest, indent=2), encoding='utf-8')
    old_cwd = os.getcwd()
    os.chdir(view)
    started = time.time()
    try:
        import pytest
        code = int(pytest.main(pytest_args, plugins=[pytest_plugin]))
    finally:
        os.chdir(old_cwd)
        elapsed = time.time() - started

    if isolated_junit.exists():
        shutil.copy2(isolated_junit, junit)
    stats = parse_junit(junit)
    out_stats = run_dir / f"stats-{target}.json"
    output_manifest = view_manifest(view)
    output_digest = manifest_digest(output_manifest)
    (run_dir / 'view-after.json').write_text(json.dumps(output_manifest, indent=2), encoding='utf-8')
    changed = [path for path in sorted(set(input_manifest) | set(output_manifest))
               if input_manifest.get(path) != output_manifest.get(path)]
    verification_exit = code or (4 if changed else 0)
    payload = {
        "run_id": run_id,
        "scope": "collect-only" if args.collect_only else "focused" if (args.pytest_arg or getattr(args, 'test_path', None)) else "full",
        "pytest_args": pytest_args,
        "requested_test_paths": test_paths,
        "python": sys.version,
        "platform": platform.platform(),
        "view_sha256": input_digest,
        "view_digest_schema": 'path-kind-content/v2',
        "view_sha256_after": output_digest,
        "view_changed_during_tests": input_digest != output_digest,
        "view_changed_paths": changed,
        "verification_exit": verification_exit,
        "built_web_required": getattr(args, 'require_built_web', False),
        "isolation_run_id": iso.run_id,
        "target": target,
        "description": cfg["description"],
        "iso_root": str(iso.root),
        "cwd": str(view),
        "pytest_exit": code,
        "elapsed_s": round(elapsed, 2),
        "junit": str(junit),
        "summary": summarize_stats(stats),
        "stats": stats,
        "fake_provider": getattr(fake.stats, "snapshot", lambda: {})(),
    }
    out_stats.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Immutable run records are authoritative; this small pointer is replaceable.
    pointer = EVIDENCE / f'latest-{target}.json'
    pointer.write_text(json.dumps({'run_id': run_id, 'scope': payload['scope'],
                                  'stats': str(out_stats), 'summary': payload['summary']},
                                 ensure_ascii=False, indent=2), encoding='utf-8')
    iso.write_manifest(target, code, log_path="", report_path=env.get("ZH_ISO_REPORT"))

    print(f"\n[ZH-ISO] 结果: exit={code}  用时 {elapsed:.1f}s  {summarize_stats(stats)}")
    print(f"[ZH-ISO] junit: {junit}")
    print(f"[ZH-ISO] stats: {out_stats}", flush=True)
    if changed:
        print(f'[ZH-ISO] Verification rejected: test view changed at {changed}', flush=True)
    return verification_exit


def _ensure_utf8_mode() -> None:
    """检查是否以 UTF-8 模式运行；不是则给出明确告警。

    原因：Windows 的 ``subprocess.run(..., text=True)`` 用
    ``locale.getpreferredencoding`` 解码子进程输出；未开启 UTF-8 模式时是 GBK，
    而子进程输出 UTF-8 中文会触发 ``UnicodeDecodeError``，使
    ``CompletedProcess.stdout`` 变为 ``None``——测试随后在拼接失败消息时抛
    ``TypeError``（已实测 test_dockerfile_structure_checker_passes）。

    正确启动方式是 ``python -X utf8 tests/zhihui_harness/run_tests.py ...``
    （``tests/run-isolated.ps1`` 已内置）。注意：Windows 上 ``os.execve`` 重执行
    会丢失输出，所以这里只告警，不做自替换。
    """
    if sys.flags.utf8_mode:
        return
    sys.stderr.write(
        "[ZH-ISO] 警告：当前解释器未开启 UTF-8 模式（建议 `python -X utf8` 启动）。\n"
        "[ZH-ISO]       Windows 下依赖子进程中文输出的用例可能因 GBK 解码失败而误报。\n")
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        _ensure_utf8_mode()
    ap = argparse.ArgumentParser(description="智惠网关隔离测试运行器")
    ap.add_argument("--target", default="manager",
                    help=f"目标：{', '.join(TARGETS)}（也可 all）")
    ap.add_argument("--keep", action="store_true", help="保留临时根用于诊断")
    ap.add_argument("--net-policy", default=None, choices=["block", "warn", "off"],
                    help="网络策略，默认 block")
    ap.add_argument("--pytest-arg", action="append", default=None,
                    help="追加给 pytest 的参数（可重复）")
    ap.add_argument('--test-path', action='append', default=None,
                    help='Replace the default test directory with target-relative file/directory/node paths (repeatable)')
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument('--verbose', action='store_true', help='Show every test ID without narrowing full-run scope')
    ap.add_argument('--require-built-web', action='store_true',
                    help='Fail if the compiled web/out artifact is missing (release gate)')
    ap.add_argument("--list-targets", action="store_true")
    ap.add_argument("--fake-mode", default="normal", help="假 provider 默认模式")
    ap.add_argument("--base-dir", default=None, help="临时根父目录（默认系统 TEMP）")
    args = ap.parse_args(argv)

    if args.list_targets:
        for name, cfg in TARGETS.items():
            print(f"{name:16} {cfg['description']}")
        return 0

    targets = list(TARGETS) if args.target == "all" else [t.strip() for t in args.target.split(",") if t.strip()]
    for t in targets:
        if t not in TARGETS:
            raise SystemExit(f"未知 target: {t}（可选：{', '.join(TARGETS)}）")

    if args.net_policy:
        os.environ["ZH_NET_POLICY"] = args.net_policy
    else:
        os.environ.setdefault("ZH_NET_POLICY", "block")

    iso = IsolatedEnv.create(REPO, keep=args.keep, base_dir=args.base_dir)
    fake = FakeProvider(mode=args.fake_mode, chunk_delay=0.0).start()
    print(f"[ZH-ISO] run_id={iso.run_id}")
    codes: dict[str, int] = {}
    try:
        for target in targets:
            codes[target] = run_one(target, args, iso, fake)
    finally:
        fake.stop()
        if args.keep:
            print(f"[ZH-ISO] 临时根已保留: {iso.root}")
        else:
            iso.cleanup()
    bad = {t: c for t, c in codes.items() if c != 0}
    print(f"\n[ZH-ISO] 退出码: {codes}")
    if bad:
        print(f"[ZH-ISO] 失败目标: {bad}")
        return max(bad.values())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
