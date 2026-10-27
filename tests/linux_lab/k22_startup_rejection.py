"""K22 补测：未知变量 / 非法单位 / 敏感值不回显的启动期实测。

与 qtg/tests/test_k22_configuration.py 的区别：那个用 TestClient 在进程内造
app；这里用**真实子进程**跑 configcontract 的 CLI 入口，验证"配置在启动前就被
拒绝"这条链路（含退出码），并核对 N33 修复后 WB_NOTIFY_CONFIG 不再被拒。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = [
    # (标题, service, env, 期望退出码, 期望错误码)
    ('未知变量必须拒绝', 'qtg', {'QTG_PROXY_TIMOUT_SEC': '7'}, 2, 'unknown_variable'),
    ('拼错的 Manager 变量必须拒绝', 'manager', {'WB_UPSTREM_TIMEOUT': '7'}, 2, 'unknown_variable'),
    ('非法单位必须拒绝', 'qtg', {'QTG_PROXY_TIMEOUT_SEC': '30ms'}, 2, 'expected_number_in_seconds'),
    ('越界值必须拒绝', 'qtg', {'QTG_PROXY_TIMEOUT_SEC': '99999'}, 2, 'above_maximum'),
    ('非法布尔必须拒绝', 'qtg', {'QTG_AUTHZ_ENFORCE': 'perhaps'}, 2, 'expected_boolean'),
    ('相对路径必须拒绝', 'qtg', {'QTG_DATA_DIR': './relative'}, 2, 'expected_absolute_path'),
    ('非法选择项必须拒绝', 'qtg', {'QTG_WB_READ_MODE': 'postgres'}, 2, 'invalid_choice'),
    ('非法 URL 必须拒绝', 'qtg', {'QTG_WB_PANEL_BASE': 'ftp://x'}, 2, 'invalid_url'),
    # N33 回归：notify.py 直接读的变量登记后必须可以通过
    ('WB_NOTIFY_CONFIG 登记后可加载', 'manager', {'WB_NOTIFY_CONFIG': '/tmp/notify.json'}, 0, None),
]


def run(service: str, env_overrides: dict) -> tuple[int, dict | None, str]:
    folder = ROOT / ('qtg/app/qtg' if service == 'qtg' else 'manager/server')
    env = {'PATH': os.environ.get('PATH', ''), 'SYSTEMROOT': os.environ.get('SYSTEMROOT', '')}
    # 只保留与本服务前缀相关的变量，避免父进程环境干扰
    prefix = 'QTG_' if service == 'qtg' else 'WB'
    for key, value in os.environ.items():
        if key.startswith(prefix):
            continue
        env.setdefault(key, value)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, '-X', 'utf8', str(folder / 'configcontract.py')],
        capture_output=True, text=True, encoding='utf-8',
        env=env, cwd=str(folder), timeout=60,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stdout + proc.stderr


def main() -> int:
    failures = []
    records = []
    for title, service, env, want_code, want_error in CASES:
        code, payload, blob = run(service, env)
        ok = code == want_code
        got = None
        if payload:
            if payload.get('ok') is True:
                got = None
            else:
                errors = payload.get('errors') or []
                got = errors[0].get('code') if errors else None
                ok = ok and got == want_error
        else:
            ok = False
        # 失败诊断里绝不能回显取值；成功路线的快照本就该包含非敏感生效值
        if code != 0:
            for value in env.values():
                if len(value) >= 6 and value in blob:
                    ok = False
                    title += '（诊断回显了取值）'
        records.append({'case': title, 'service': service,
                        'exit': code, 'error_code': got, 'ok': ok})
        print(('PASS ' if ok else 'FAIL ') + f'{title}  exit={code} code={got}')
        if not ok:
            failures.append(title)
    out = ROOT / 'evidence' / 'k22-startup-rejection.json'
    out.write_text(json.dumps({'schema': 'zhihui-k22-startup/v1', 'cases': records},
                              ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'\n{len(records) - len(failures)}/{len(records)} passed; evidence -> {out}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
