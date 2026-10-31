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

## 与 operational 的接线

`operational.transition()` 在状态变化时调用 `dispatch(...)`。投递是**同步、尽力而为**，
因为 transition 本身在 monitor 循环里（异步上下文），用同步 http 会阻塞——所以 http 模式
只接受短超时（默认 3s），超时即放弃并记 warning。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger('workbuddy.notify')

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


def _settings() -> dict[str, Any]:
    """读取投递配置。文件不存在/损坏 -> 全默认（disabled），绝不因此崩。"""
    path = Path(os.environ.get('WB_NOTIFY_CONFIG', '/opt/notify/config.json'))
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def configured_mode() -> str:
    cfg = _settings()
    if not cfg.get('enabled'):
        return 'disabled'
    mode = str(cfg.get('mode') or '').strip().lower()
    return mode if mode in ('file', 'http') else 'disabled'


def dispatch(name: str, active: bool, detail: str = '', *, host: str = '') -> dict:
    """投递一条告警状态变化。返回结果字典（便于测试与审计）。

    永远不抛：任何异常都降级为 `{'delivered': False, 'error': ...}`。
    """
    mode = configured_mode()
    payload = {
        'name': name,
        'active': bool(active),
        'detail': redact(detail),
        'since': int(time.time()),
        'host': host or os.uname().nodename if hasattr(os, 'uname') else host,
        'schema': 'zhihui-alert/1',
    }
    if mode == 'disabled':
        return {'mode': 'disabled', 'delivered': False, 'payload': payload}

    cfg = _settings()
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
            timeout = float(cfg.get('timeout_sec') or 3)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {'mode': 'http', 'delivered': 200 <= resp.status < 300,
                        'status': resp.status, 'url': url}
    except Exception as exc:  # noqa: BLE001 - 投递失败绝不能影响主流程
        log.warning('alert delivery failed name=%s mode=%s: %s', name, mode, exc)
        return {'mode': mode, 'delivered': False, 'error': str(exc)[:200]}
    return {'mode': mode, 'delivered': False, 'error': 'unknown mode'}
