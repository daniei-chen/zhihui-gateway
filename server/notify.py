"""告警投递层（K46）。

## 为什么需要它

`operational.transition()` 只写日志。日志能证明"信号被采集到"，但**不能证明"有人会知道"**
——这正是 S4「监控必须有真实采集和通知投递/恢复证据」的缺口。

这里加一个**最小的投递抽象**：
  - 默认 `disabled`：什么都不做（保持现网行为，不擅自外发）；
  - `file` 模式：写到本地 JSONL（用于验证链路，**不联网**）；
  - `http` 模式：POST 到配置的 URL（**必须显式开启**，默认不发）。

## 安全约束（硬性）

1. **默认不发送**：`notify.enabled` 缺省 false。任何外部发送都需要部署方显式配置。
2. **不发凭据**：payload 只含 `name / active / detail / since / host`，
   `detail` 过一遍脱敏（URL 里的 userinfo、token 形态的字符串）。
3. **失败不阻塞主流程**：投递异常只记 warning，绝不让告警逻辑把服务带崩。
4. **去重**：同一 `name` 的同一状态只投递一次（进入/恢复各一次），避免刷屏。

## 未配置时：丢弃，但必须留下痕迹

`disabled` 不等于「这件事没发生」。告警已经被 `operational.transition()` 算出来了，
只是没人会被叫醒 —— 这正是 P0-1（每日备份连续失败）靠人工审计才发现的成因。
所以 `dispatch()` 在 `disabled` 分支**不静默**：它把这条告警记进本地 JSONL、
记一条 WARNING，并计入健康端点的 `notify.dropped`（见 `_DROPPED`）。

## 与 operational 的接线

`operational.transition()` 在状态变化时调用 `dispatch(...)`。投递是**同步、尽力而为**，
因为 transition 本身在 monitor 循环里（异步上下文），用同步 http 会阻塞——所以 http 模式
只接受 0.1–3s 的有限 socket 超时（默认 3s），超时即放弃并记 warning。
这不是 DNS、重定向等全部阶段合计的硬性 deadline；调用方仍应离开事件循环投递。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger('workbuddy.notify')

#: 未配置时健康端点与启动日志用的固定措辞。改这句话就等于改了运维看到的结论，
#: 所以只在这里定义一次，两个出口共用。
UNCONFIGURED_WARNING = '告警未配置，运行告警将被丢弃'

#: 未配置时的丢弃台账（进程内）。
#:
#: ## 为什么需要它
#:
#: `_mode()` 在配置缺失时返回 `disabled`，旧的 `dispatch()` 在这一支直接返回
#: `{'mode': 'disabled', 'delivered': False}` —— **告警算出来就被丢弃**：不落盘、
#: 不发 HTTP、也不打日志。默认部署（没有 `/opt/notify/config.json`）下每一条
#: 运行告警都是这样消失的，2026-11 的 P0-1「每日备份连续失败」最后是靠人工审计
#: 发现的，不是告警发现的。
#:
#: 于是 `disabled` 分支留下三层痕迹，任一层都足以看见：
#:   1. 本地 JSONL（`spool_path()`）—— 跨重启可查；
#:   2. 一条 WARNING —— 启动之后第一次触发时就出现在日志里；
#:   3. `/api/health/alerts` 的 `notify` 字段 —— 这里累计的计数与告警名。
#:
#: 计数随进程重启归零是对的：它回答的是「这个进程启动以来丢了多少」，
#: 跨重启的账在 JSONL 里。
_DROPPED: dict[str, Any] = {'count': 0, 'names': {}, 'last_at': 0}
_DROPPED_LOCK = threading.Lock()

# 脱敏：URL userinfo、常见 token 前缀、长 base64 串
_URL_USERINFO = re.compile(r'(?<=://)[^/\s:@]+:[^/\s@]+@')
_TOKENISH = re.compile(r'\b(wbt_|wbk_|sk-|ghp_|Bearer\s+)[A-Za-z0-9_\-\.]{8,}')
_LONG_B64 = re.compile(r'\b[A-Za-z0-9_\-]{40,}\b')


def redact(text: str, *, limit: int = 500) -> str:
    """把 detail 里可能的凭据抹掉。宁可多抹，不可漏。"""
    s = str(text or '')
    s = _URL_USERINFO.sub('***:***@', s)
    s = _TOKENISH.sub(lambda m: m.group(1) + '***', s)
    s = _LONG_B64.sub('***', s)
    s = re.sub(r'(?i)(["\x27](?:password|passwd|api[_-]?key|authcode|access[_-]?token|refresh[_-]?token|cookie|secret)["\x27]\s*:\s*)'
               r'("(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\]|\\.)*\x27)', r'\1"***"', s)
    s = re.sub(r'(?i)\b(password|passwd|api[_-]?key|authcode|access[_-]?token|refresh[_-]?token|cookie|secret)'
               r'(\s*[=:]\s*)[^\s,;"\x27<>]+', r'\1\2***', s)
    return s[:max(0, min(20000, limit))]


def _config_path() -> Path:
    return Path(os.environ.get('WB_NOTIFY_CONFIG', '/opt/notify/config.json'))


def _settings() -> dict[str, Any]:
    """读取投递配置。文件不存在/损坏 -> 全默认（disabled），绝不因此崩。"""
    path = _config_path()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _mode(cfg) -> str:
    if not isinstance(cfg, dict) or cfg.get('enabled') is not True:
        return 'disabled'
    mode = str(cfg.get('mode') or '').strip().lower()
    return mode if mode in ('file', 'http') else 'disabled'


def configured_mode() -> str:
    return _mode(_settings())


def spool_path() -> Path:
    """未投递告警的本地账。

    刻意**不依赖投递配置文件** —— 这个账存在的唯一理由就是配置文件缺失或没启用，
    所以它的位置只能来自服务自己的数据目录。`config.DATA_DIR` 是唯一口径
    （与 manager.db、ledger-spool 同一个根），不另开环境变量。
    """
    from . import config
    return Path(config.DATA_DIR) / 'notify' / 'undelivered.jsonl'


def dropped_summary() -> dict:
    """进程内丢弃计数的副本（调用方不该直接改内部状态）。"""
    with _DROPPED_LOCK:
        return {'count': int(_DROPPED['count']),
                'names': dict(_DROPPED['names']),
                'last_at': int(_DROPPED['last_at'])}


def status() -> dict:
    """投递通道的当前状态：给启动日志、健康端点和运维脚本用。**永不抛**。"""
    path = _config_path()
    try:
        mode = _mode(_settings())
    except Exception:      # noqa: BLE001 - 健康检查不能因为读取配置而 500
        mode = 'disabled'
    try:
        present = path.is_file()
    except OSError:        # Path.is_file() 对 EACCES 会抛（Python 3.12 起）
        present = False
    configured = mode != 'disabled'
    result: dict[str, Any] = {
        'configured': configured,
        'mode': mode,
        'config_path': str(path),
        'config_present': present,
        'warning': '' if configured else UNCONFIGURED_WARNING,
        'dropped': dropped_summary(),
    }
    try:
        result['spool'] = str(spool_path())
    except Exception:      # noqa: BLE001
        result['spool'] = ''
    return result


def log_startup_state() -> dict:
    """启动自检：未配置就在启动日志里点名后果。不阻断启动。"""
    state = status()
    if state['configured']:
        log.info('alert delivery configured mode=%s config=%s',
                 state['mode'], state['config_path'])
    else:
        log.warning(
            '%s（alert delivery unconfigured: %s 不存在或未 enabled）。'
            'monitor 每轮算出的运行告警没人会被叫醒；未投递的告警写入 %s，'
            '并计入 /api/health/alerts 的 notify 字段。要真正投递请设置 '
            'WB_NOTIFY_CONFIG 指向启用的配置（最小示例见 docs/ALERT-ROUTING.md）。',
            UNCONFIGURED_WARNING, state['config_path'], state['spool'])
    return state


def _http_timeout(value) -> float:
    """Finite socket timeout in [0.1, 3] seconds; invalid input uses 3 seconds."""
    try:
        timeout = float(value) if not isinstance(value, bool) else 3.0
    except (TypeError, ValueError, OverflowError):
        timeout = 3.0
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = 3.0
    return max(0.1, min(timeout, 3.0))


def _record_drop(name: str, payload: dict) -> dict:
    """记一条「因为没配置所以送不出去」的告警。**永不抛**。

    三层痕迹都要试着留，任何一层失败都不能影响其它层 —— 这个函数在监控循环里跑，
    让一次磁盘满把整个 `_monitor_tick` 带崩，比丢一条告警更糟。
    """
    path = None
    journaled = False
    try:
        path = spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(payload, delivered=False, reason='notify_unconfigured')
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
        journaled = True
    except Exception as exc:  # noqa: BLE001 - 账写不进去仍有日志与健康端点
        log.warning('alert drop journal unavailable path=%s: %s', path, exc)

    with _DROPPED_LOCK:
        _DROPPED['count'] += 1
        _DROPPED['names'][name] = _DROPPED['names'].get(name, 0) + 1
        _DROPPED['last_at'] = int(time.time())

    log.warning(
        'alert dropped: notify unconfigured, nobody is paged name=%s active=%s '
        'spool=%s detail=%s',
        name, payload.get('active'), str(path) if journaled else '<unavailable>',
        payload.get('detail'))
    return {'mode': 'disabled', 'delivered': False, 'dropped': True,
            'spool': str(path) if journaled else '', 'payload': payload}


def dispatch(name: str, active: bool, detail: str = '', *, host: str = '') -> dict:
    """投递一条告警状态变化。返回结果字典（便于测试与审计）。

    永远不抛：任何异常都降级为 `{'delivered': False, 'error': ...}`。
    未配置时不是「什么都没发生」：走 `_record_drop()`，落本地账、记 warning、
    计入健康端点。
    """
    cfg = _settings()
    mode = _mode(cfg)
    payload = {
        'name': name,
        'active': bool(active),
        'detail': redact(detail),
        'since': int(time.time()),
        'host': host or os.uname().nodename if hasattr(os, 'uname') else host,
        'schema': 'zhihui-alert/1',
    }
    if mode == 'disabled':
        return _record_drop(name, payload)

    try:
        if mode == 'file':
            out = Path(cfg.get('file') or '/opt/notify/alerts.jsonl')
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + '\n')
            return {'mode': 'file', 'delivered': True, 'path': str(out)}
        if mode == 'http':
            url = str(cfg.get('url') or '')
            if not url.startswith(('http://', 'https://')):
                return {'mode': 'http', 'delivered': False,
                        'error': 'url 必须显式配置为 http(s)://'}
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(
                url, data=body, method='POST',
                headers={'Content-Type': 'application/json'})
            timeout = _http_timeout(cfg.get('timeout_sec'))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {'mode': 'http', 'delivered': 200 <= resp.status < 300,
                        'status': resp.status, 'url': url}
    except Exception as exc:  # noqa: BLE001 - 投递失败绝不能影响主流程
        log.warning('alert delivery failed name=%s mode=%s: %s', name, mode, exc)
        return {'mode': mode, 'delivered': False, 'error': str(exc)[:200]}
    return {'mode': mode, 'delivered': False, 'error': 'unknown mode'}
