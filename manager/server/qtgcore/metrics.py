"""Prometheus 指标（自实现，零依赖）。

WorkBuddy Manager 的短板之一是「全仓 grep 不到 /metrics」，外部监控接不进来。
这里从第一天就暴露标准文本格式，字段名沿用 BFF 已有的 `qoder_bff_*` 语义，
并统一加 `qtg_` 前缀避免与 BFF 混淆。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, Tuple

_LOCK = threading.Lock()
_START = time.time()

# 计数器：name -> value（name 不含 help/type 元信息）
_counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = defaultdict(float)
_gauges: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
# 直方图：name -> (buckets, {le: count}, sum, count)
_hist_buckets = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_hist: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], list] = {}

_HELP = {
    "qtg_requests_total": ("Total HTTP requests handled by qtg.", "counter"),
    "qtg_request_duration_seconds": ("HTTP request latency.", "histogram"),
    "qtg_upstream_requests_total": ("Calls made to upstream control planes.", "counter"),
    "qtg_upstream_errors_total": ("Failed upstream calls by kind.", "counter"),
    "qtg_breaker_open_total": ("Times a circuit breaker opened.", "counter"),
    "qtg_actions_total": ("Write actions executed by kind/result.", "counter"),
    "qtg_audit_records": ("Audit records appended.", "gauge"),
    "qtg_cache_hit_ratio": ("In-process cache hit ratio.", "gauge"),
    "qtg_upstream_up": ("Whether each upstream control plane is reachable.", "gauge"),
    "qtg_accounts_total": ("Accounts discovered, by platform/state.", "gauge"),
    "qtg_process_uptime_seconds": ("Process uptime.", "gauge"),
}


def _key(name: str, labels: dict | None) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    items = tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))
    return name, items


def inc(name: str, labels: dict | None = None, value: float = 1.0) -> None:
    with _LOCK:
        _counters[_key(name, labels)] += value


def set_gauge(name: str, value: float, labels: dict | None = None) -> None:
    with _LOCK:
        _gauges[_key(name, labels)] = float(value)


def observe(name: str, value: float, labels: dict | None = None) -> None:
    k = _key(name, labels)
    v = max(0.0, float(value))
    with _LOCK:
        state = _hist.get(k)
        if state is None:
            state = [[0 for _ in _hist_buckets], 0.0, 0]
            _hist[k] = state
        for i, b in enumerate(_hist_buckets):
            if v <= b:
                state[0][i] += 1
        state[1] += v
        state[2] += 1


def _fmt_labels(items: Tuple[Tuple[str, str], ...], extra: Tuple[Tuple[str, str], ...] = ()) -> str:
    parts = [f'{k}="{_escape(v)}"' for k, v in items] + [f'{k}="{_escape(v)}"' for k, v in extra]
    return "{" + ",".join(parts) + "}" if parts else ""


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def observe_hist_from_cache() -> None:
    """把缓存命中率同步成 gauge（供 /metrics 直接暴露）。"""
    from .cache import cache

    st = cache.stats()
    set_gauge("qtg_cache_hit_ratio", st["hit_ratio"])


def render() -> str:
    observe_hist_from_cache()
    set_gauge("qtg_process_uptime_seconds", time.time() - _START)
    lines: list[str] = []
    seen: set[str] = set()

    def head(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        help_text, kind = _HELP.get(name, (name, "gauge"))
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    with _LOCK:
        for (name, labels), value in sorted(_counters.items()):
            head(name)
            lines.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
        for (name, labels), value in sorted(_gauges.items()):
            head(name)
            lines.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
        for (name, labels), state in sorted(_hist.items()):
            head(name)
            cum = 0
            for i, b in enumerate(_hist_buckets):
                cum = state[0][i]
                lines.append(
                    f"{name}_bucket{_fmt_labels(labels, (('le', str(b)),))} {cum}"
                )
            lines.append(f"{name}_bucket{_fmt_labels(labels, (('le', '+Inf'),))} {state[2]}")
            lines.append(f"{name}_sum{_fmt_labels(labels)} {_num(state[1])}")
            lines.append(f"{name}_count{_fmt_labels(labels)} {state[2]}")
    return "\n".join(lines) + "\n"


def _num(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def reset() -> None:
    """仅测试使用。"""
    with _LOCK:
        _counters.clear()
        _gauges.clear()
        _hist.clear()
