#!/opt/workbuddy-manager/venv/bin/python
"""从 config-schema.json + 生产生效环境生成 `.env.example`（U29，2026-10-28）。

## 为什么需要生成而不是手写

`.env.example` 手写时只有 20 项，而 `config-schema.json` 有 96 项 —— 差的 76 项里
包括 `WB_LEDGER_*`、`WB_BUDGET_*`、`WB_DRAIN_TIMEOUT_SEC`、`WB_GRACEFUL_SHUTDOWN_S`
这些运维真会去设的项。更糟的是手写版与生产不一致（`WB_MANAGER_HOST=0.0.0.0`
而生产生效 127.0.0.1、`WB_UPSTREAM_TIMEOUT=120` 而生产 600、`WB2API_CONTAINER`
还是旧容器名）。

本脚本以 schema 为唯一来源，逐项标注：
  * 默认值（schema）
  * **生产生效值**（`/proc/<MainPID>/environ`，读不到就标 `未设置`）
  * 敏感项只写「需设置」占位，绝不输出明文

用法：gen_env_example.py [--out PATH] [--main-pid PID]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCHEMA = Path('/opt/workbuddy-manager/server/config-schema.json')
DEFAULT_OUT = Path('/opt/workbuddy-manager/.env.example')

GROUPS = [
    ('本服务', ('WB_MANAGER_', 'WB_SECURE_COOKIE', 'WB_SESSION_DAYS', 'WB_CORS_ORIGINS',
                'WB_LOG_LEVEL', 'WB_ENABLE_DOCS', 'WB_RUN_MODE', 'WB_SERVICE_NAME')),
    ('上游 workbuddy2api', ('WB2API_',)),
    ('数据与静态目录', ('WB_AUTH_DIR', 'WB_UPSTREAM_CONFIG', 'WB_DATA_DIR', 'WB_DB',
                        'WB_USERS_FILE', 'WB_STATIC_DIR', 'WB_BACKUP_STATUS_FILE')),
    ('网络与超时', ('WB_UPSTREAM_TIMEOUT', 'WB_TENCENT_TIMEOUT', 'WB_TRUST_PROXY',
                    'WB_TRUSTED_PROXY_HOPS', 'WB_HTTP_PROXY', 'WB_HTTPS_PROXY',
                    'WB_DRAIN_TIMEOUT_SEC', 'WB_GRACEFUL_SHUTDOWN_S')),
    ('并发与准入', ('WB_GATEWAY_MAX_', 'WB_KEY_CONCURRENCY', 'WB_GATEWAY_RATE_',
                    'WB_GATEWAY_LARGE_')),
    ('预算与账本', ('WB_KEY_BUDGET_', 'WB_BUDGET_', 'WB_LEDGER_')),
    ('安全与凭据', ('WB_ADMIN_PASSWORD', 'WB_METRICS_TOKEN', 'WB_GITHUB_TOKEN',
                    'WB_QTG_')),
]


def group_of(name: str) -> str:
    for title, prefixes in GROUPS:
        if any(name.startswith(prefix) for prefix in prefixes):
            return title
    return '其他'


def process_env(pid: str | None) -> dict:
    if not pid:
        try:
            pid = Path('/run/workbuddy-web.pid').read_text().strip()
        except OSError:
            pid = None
    if not pid:
        return {}
    try:
        raw = Path('/proc/%s/environ' % pid).read_bytes()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b'\0'):
        if b'=' not in item:
            continue
        key, _, value = item.partition(b'=')
        try:
            out[key.decode('utf-8')] = value.decode('utf-8')
        except UnicodeDecodeError:
            continue
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--main-pid', default=None)
    args = parser.parse_args(argv)

    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    settings = schema['settings']
    live = process_env(args.main_pid)

    buckets: dict[str, list[tuple[str, dict]]] = {}
    for name in sorted(settings):
        buckets.setdefault(group_of(name), []).append((name, settings[name]))

    lines = [
        '# WorkBuddy Manager 环境变量示例（U29 自动生成，勿手改）',
        '#',
        '# 生成来源：server/config-schema.json（96 项）+ 生产进程真实环境。',
        '# 「生产生效值」来自 /proc/<MainPID>/environ；标「未设置」表示该变量没在',
        '# systemd unit / drop-in 里出现，运行时用 schema 默认值。',
        '#',
        '# 注意：新增未知 WB_* 会让服务因 unknown_variable **启动失败**，',
        '# 所以设之前先确认它在 schema 里（GET /api/system/config 可查四源对照）。',
        '',
    ]
    order = [title for title, _ in GROUPS] + ['其他']
    for title in order:
        items = buckets.get(title) or []
        if not items:
            continue
        lines.append('# ── %s ──────────────────────────────────────────' % title)
        for name, spec in items:
            sensitive = bool(spec.get('sensitive'))
            default = spec.get('default')
            live_value = live.get(name)
            note = []
            if sensitive:
                note.append('敏感：需在 unit/drop-in 里显式设置，不要写进版本库')
                value = ''
            else:
                value = live_value if live_value is not None else ''
                if live_value is not None:
                    note.append('生产生效值')
                elif default is not None:
                    note.append('未设置，用 schema 默认 %r' % (default,))
                else:
                    note.append('未设置，且 schema 无默认值')
            if spec.get('unit'):
                note.append('单位 %s' % spec['unit'])
            if spec.get('minimum') is not None:
                note.append('最小 %s' % spec['minimum'])
            if note:
                lines.append('# %s' % '；'.join(note))
            lines.append('%s=%s' % (name, value))
        lines.append('')

    Path(args.out).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    total = sum(len(v) for v in buckets.values())
    print('written %s: %d settings, %d groups, live env %d keys'
          % (args.out, total, len([t for t in order if buckets.get(t)]), len(live)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
