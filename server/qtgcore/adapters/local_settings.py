"""适配器直读 Manager 本地库时的**唯一**设置面板实现（U28 收敛，2026-10-28）。

## 为什么需要这个模块

`qtgcore/adapters/workbuddy.py` 的 sqlite 读模式要读 Manager 的 `settings` 表。
它原来的实现是：

```python
row = conn.execute("SELECT * FROM settings LIMIT 1").fetchone()
cols = [c[0] for c in conn.execute("SELECT * FROM settings LIMIT 1").description or []]
panel = dict(zip(cols, row))          # → {'key': ..., 'value': ...}
```

`settings` 是 `(key TEXT PRIMARY KEY, value TEXT)` 的 **key/value 表**，不是宽表。
于是那份实现和 U19 在 `server/routers/settings.py` 修掉的是**同一个缺陷**：

* **形状不对**：调用方拿到的是"某一行的两个列"，不是配置本身；
* **取到哪一行由扫描顺序决定** —— 可能正好是 `github_token`，于是设置面板
  把**凭据明文**当"配置"返回；
* 表里有 N 行时也只暴露第一行，其余全部丢失。

## 为什么不直接 import Manager 的实现

`qtgcore` 是**可独立部署的服务**（自带 `configcontract.py` / `config-schema.json`，
见 `qtgcore/config.py`），不能 import `server.routers.settings` —— 那会把 Manager 的
FastAPI 路由层拖进引擎，破坏「引擎可单独打包」的既有边界。

所以白名单在这里保留一份，**并由 `server/tests/test_module_convergence.py` 断言
两份完全相等、且同一份数据库上两份实现返回同一个 dict**：
重复可以接受，漂移不行。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

#: 对外暴露的设置键**白名单**（与 `server/routers/settings.py::_PANEL_SETTING_KEYS`
#: 逐字一致；U19 的口径：白名单失效时"新键不显示"，黑名单失效时"新凭据静默泄露"）。
#: 被排除的敏感键：`github_token`、`pg_sync_config`、`update_proxy`、`proxy_pool`、
#: `proxy_health`、`proxy_sticky`、`security`。
PANEL_SETTING_KEYS = frozenset({
    'model_map',
    'prompt_compat_enabled',
    'prompt_defuse_rules',
    'global_proxy_enabled',
    'task_claim_schedule',
    'daily_tasks_plan',
    'token_refresh_plan',
    'credits_snapshot',
    'update_package',
})


def panel_value(raw: object) -> object:
    """`settings.value` 是 JSON 文本；解析失败就回原文（不猜、不丢）。"""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def read_panel(conn: sqlite3.Connection) -> Dict[str, Any]:
    """把 key/value 的 `settings` 表读成**扁平的白名单字典**。

    * 表不存在 / 无 `key`,`value` 列 → 返回 `{}`（"没有配置"是合法状态，
      不是"读不到"；后者由调用方按 sqlite3.Error 处理）；
    * 只返回白名单内的键，值按 JSON 解析 —— 与
      `server/routers/settings.py::_panel_value` **同一口径**（解析失败回原文，
      不像 `db.get_setting` 那样回落成 default：面板要把原值展示出来）。
    """
    try:
        rows = conn.execute('SELECT key, value FROM settings').fetchall()
    except sqlite3.Error:
        return {}
    panel: Dict[str, Any] = {}
    for row in rows:
        try:
            key = str(row[0])
            raw = row[1]
        except (IndexError, TypeError):      # pragma: no cover - 防御性
            continue
        if key not in PANEL_SETTING_KEYS:
            continue
        panel[key] = panel_value(raw)
    return panel
