"""pytest 隔离插件：强制隔离运行、挂载 dev 包、拦外网与生产路径写入。

由 ``run_tests.py`` 通过 ``pytest.main(..., plugins=[模块])`` 加载，或在命令行用
``-p zhihui_harness.pytest_plugin`` 加载（需 ``tests/`` 在 sys.path）。

硬约束：
  * 没有 ``ZH_ISO_ROOT`` 环境变量时**直接拒绝运行**（exit 3），防止有人绕过隔离器
    直接 ``pytest`` 把测试写到真实 ``manager/data`` 或 ``/opt``；
  * 写受保护路径（/opt、/var、/etc、/root、/tmp、/home、生产库、仓库 manager/data）
    抛 ``PermissionError``，测试因此失败——这是 T02 第 5 条要求的行为；
  * 默认阻断非回环网络（``ZH_NET_POLICY=block``），只放行 127.0.0.1/::1/localhost，
    假 provider 因此可用，真实上游不可达。

所有观测结果写入 ``ZH_ISO_REPORT`` 指向的 JSON，供 RESULTS.md 引用。
"""
from __future__ import annotations

import builtins
import io
import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from .isolation import is_loopback, protected_reason

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
_STATE: dict[str, Any] = {
    "iso_root": "",
    "run_id": "",
    "net_policy": "block",
    "started_at": 0.0,
    "net_attempts": [],
    "net_blocked": 0,
    "protected_writes": [],
    "outside_writes": [],
    "outside_write_count": 0,
    "installed": False,
    "env_problems": [],
    "fake_stats_url": "",
}
_MAX_SAMPLES = 300
_PATCHES: list[tuple[Any, str, Any]] = []


def _patch(target: Any, name: str, replacement: Any) -> None:
    _PATCHES.append((target, name, getattr(target, name)))
    setattr(target, name, replacement)


def pytest_unconfigure(config: pytest.Config) -> None:
    # The runner persists finalized evidence after pytest.main() returns.
    # Global guards must not remain installed between separate test targets.
    while _PATCHES:
        target, name, original = _PATCHES.pop()
        setattr(target, name, original)
    _STATE['installed'] = False


class NetworkBlocked(RuntimeError):
    """非回环网络访问被隔离策略阻断。"""


def _sample(bucket: str, item: dict[str, Any]) -> None:
    lst = _STATE[bucket]
    if len(lst) < _MAX_SAMPLES:
        lst.append(item)


def _host_of(address: Any) -> str | None:
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("utf-8", "replace")
        return str(host)
    return None


# ---------------------------------------------------------------------------
# pytest 选项
# ---------------------------------------------------------------------------
def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("zhihui-isolation")
    group.addoption("--zh-net-policy", action="store", default=None,
                    choices=["block", "warn", "off"],
                    help="隔离网络策略：block(默认)/warn/off")
    group.addoption("--zh-report", action="store", default=None,
                    help="隔离报告 JSON 输出路径（默认读 ZH_ISO_REPORT）")


# ---------------------------------------------------------------------------
# 启动：校验隔离 + 安装守卫
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    iso_root = os.environ.get("ZH_ISO_ROOT", "")
    if not iso_root:
        pytest.exit(
            "未在隔离运行器下运行：缺少 ZH_ISO_ROOT。\n"
            "请用 `tests/run-isolated.ps1` 或 `python tests/zhihui_harness/run_tests.py` 启动测试，\n"
            "不要直接调用 pytest —— 直接运行会把测试写到真实数据目录/系统路径。",
            returncode=3,
        )
    root = Path(iso_root).resolve()
    if not root.is_dir():
        pytest.exit(f"隔离根不存在: {root}", returncode=3)

    policy = config.getoption("--zh-net-policy") or os.environ.get("ZH_NET_POLICY", "block")
    _STATE.update(net_attempts=[], net_blocked=0, protected_writes=[], outside_writes=[],
                  outside_write_count=0)
    _STATE.pop('final_report', None)
    _STATE.update({
        "iso_root": str(root),
        "run_id": os.environ.get("ZH_ISO_RUN_ID", ""),
        "net_policy": policy,
        "started_at": time.time(),
        "fake_stats_url": (os.environ.get("ZH_FAKE_PROVIDER_BASE", "") or "").rstrip("/"),
    })

    # 1) 校验业务路径确实落在隔离根内
    _STATE["env_problems"] = _validate_env(root)
    # 2) 挂载完整源码树里的 dev 包等额外 sys.path（修复 test_upstream_patches 收集）
    _install_syspath()
    # 3) 安装守卫
    _install_net_guard(policy)
    _install_path_guard(root)
    _STATE["installed"] = True


def _validate_env(root: Path) -> list[str]:
    """独立于 run_tests 的二次校验：防止环境变量被子进程/测试改写后仍算合格。"""
    problems: list[str] = []
    keys = ("WB_DATA_DIR", "WB_DB", "WB_USERS_FILE", "WB_AUTH_DIR",
            "WB_UPSTREAM_CONFIG", "WB_UPSTREAM_DIR")
    for key in keys:
        value = os.environ.get(key, "")
        if not value:
            problems.append(f"{key} 未设置")
            continue
        p = Path(value)
        try:
            p.resolve().relative_to(root)
        except (ValueError, OSError):
            problems.append(f"{key}={value} 不在隔离根内")
        reason = protected_reason(p, iso_root=root)
        if reason:
            problems.append(f"{key}={value} 命中受保护路径：{reason}")
    return problems


def _install_syspath() -> None:
    entries = [e for e in os.environ.get("ZH_EXTRA_SYSPATH", "").split(os.pathsep) if e]
    for entry in reversed(entries):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)
            _STATE.setdefault("syspath_added", []).append(entry)


# ---------------------------------------------------------------------------
# 网络守卫
# ---------------------------------------------------------------------------
def _install_net_guard(policy: str) -> None:
    if policy == "off":
        return
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_gai = socket.getaddrinfo
    orig_cc = socket.create_connection

    def _decide(host: str, where: str) -> None:
        _sample("net_attempts", {"host": host, "where": where,
                                 "t": round(time.time() - _STATE["started_at"], 3)})
        if is_loopback(host):
            return
        _STATE["net_blocked"] += 1
        if policy == "block":
            raise NetworkBlocked(
                f"[ZH-ISO] 阻断非回环网络访问：{host}（{where}）。"
                "隔离测试只允许 127.0.0.1 上的假 provider。")

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        host = _host_of(address)
        if host:
            _decide(host, "socket.connect")
        return orig_connect(self, address)

    def guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
        host = _host_of(address)
        if host:
            _decide(host, "socket.connect_ex")
        return orig_connect_ex(self, address)

    def guarded_getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host not in (None, "", b""):
            text = host.decode("utf-8", "replace") if isinstance(host, bytes) else str(host)
            _decide(text, "socket.getaddrinfo")
        return orig_gai(host, port, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = _host_of(address)
        if host:
            _decide(host, "socket.create_connection")
        return orig_cc(address, *args, **kwargs)

    _patch(socket.socket, 'connect', guarded_connect)
    _patch(socket.socket, 'connect_ex', guarded_connect_ex)
    _patch(socket, 'getaddrinfo', guarded_getaddrinfo)
    _patch(socket, 'create_connection', guarded_create_connection)
    _STATE["net_guard_installed"] = True


# ---------------------------------------------------------------------------
# 路径守卫
# ---------------------------------------------------------------------------
def _install_path_guard(root: Path) -> None:
    def check_write(target: Any, mode: str, where: str) -> None:
        if isinstance(target, int):
            return
        try:
            p = Path(target)
        except (TypeError, ValueError):
            return
        write = any(c in mode for c in "wax+")
        if not write:
            return
        # 隔离根内一律放行：测试会在临时目录里模拟部署包（workbuddy-manager-vX/ 等）
        try:
            p.resolve().relative_to(root)
            return
        except (ValueError, OSError):
            pass
        reason = protected_reason(p, iso_root=root)
        if reason:
            _sample("protected_writes", {"path": str(p), "reason": reason, "where": where})
            raise PermissionError(
                f"[ZH-ISO] 拒绝写入受保护的生产路径：{p}（{reason}），来源 {where}")
        _STATE["outside_write_count"] += 1
        _sample("outside_writes", {"path": str(p), "where": where,
                                   "t": round(time.time() - _STATE["started_at"], 3)})

    orig_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        check_write(file, str(mode), "builtins.open")
        return orig_open(file, mode, *args, **kwargs)

    orig_io_open = io.open

    def guarded_io_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        check_write(file, str(mode), "io.open")
        return orig_io_open(file, mode, *args, **kwargs)

    orig_os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        write_flag = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
        if write_flag:
            check_write(path, "w", "os.open")
        return orig_os_open(path, flags, *args, **kwargs)

    _patch(builtins, 'open', guarded_open)
    _patch(io, 'open', guarded_io_open)
    _patch(os, 'open', guarded_os_open)
    _STATE["path_guard_installed"] = True


# ---------------------------------------------------------------------------
# 收尾：写报告 + 终端摘要
# ---------------------------------------------------------------------------
def _fake_provider_stats() -> dict[str, Any]:
    base = _STATE.get("fake_stats_url") or ""
    if not base:
        return {}
    try:
        with urllib.request.urlopen(f"{base}/__stats", timeout=5) as resp:  # noqa: S310 - 仅本机
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    report_path = os.environ.get("ZH_ISO_REPORT", "")
    payload = {
        "iso_root": _STATE["iso_root"],
        "run_id": _STATE["run_id"],
        "net_policy": _STATE["net_policy"],
        "installed": _STATE["installed"],
        "syspath_added": _STATE.get("syspath_added", []),
        "env_problems": _STATE["env_problems"],
        "pytest_exitstatus": int(exitstatus),
        "duration_s": round(time.time() - _STATE["started_at"], 2),
        "network": {
            "attempts": _STATE["net_attempts"],
            "attempt_count": len(_STATE["net_attempts"]),
            "blocked": _STATE["net_blocked"],
        },
        "writes": {
            "protected_violations": _STATE["protected_writes"],
            "outside_iso_samples": _STATE["outside_writes"],
            "outside_iso_count": _STATE["outside_write_count"],
        },
        "fake_provider": _fake_provider_stats(),
    }
    if report_path:
        try:
            out = Path(report_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 报告失败不能影响测试结论
            sys.stderr.write(f"[ZH-ISO] 写隔离报告失败: {exc}\n")
    _STATE["final_report"] = payload


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:  # type: ignore[no-untyped-def]
    payload = _STATE.get("final_report") or {}
    if not payload:
        return
    tr = terminalreporter
    tr.write_sep("-", "隔离环境摘要 (ZH-ISO)")
    tr.write_line(f"隔离根: {payload.get('iso_root')}")
    tr.write_line(f"网络策略: {payload.get('net_policy')}；"
                  f"非回环尝试 {payload['network']['attempt_count']} 次，"
                  f"阻断 {payload['network']['blocked']} 次")
    writes = payload.get("writes", {})
    tr.write_line(f"受保护路径写入拦截: {len(writes.get('protected_violations', []))} 次；"
                  f"越界写(仅记录): {writes.get('outside_iso_count', 0)} 次")
    if payload.get("env_problems"):
        tr.write_line(f"隔离校验问题: {payload['env_problems']}")
    fp = payload.get("fake_provider") or {}
    if fp and "total" in fp:
        tr.write_line(f"假 provider 调用: total={fp['total']} cancelled={fp.get('cancelled')} "
                      f"by_mode={fp.get('by_mode')}")
