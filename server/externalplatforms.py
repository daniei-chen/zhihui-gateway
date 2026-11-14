"""外部平台（Trae / Qoder）的账号可见性。

## 为什么需要它

网关自己的账号管理（`/api/accounts/*`）读的是 `WB_AUTH_DIR`，那是 **workbuddy**
的账号目录。Trae 与 Qoder 是各自独立容器里的引擎，账号数据在容器内：

| 平台 | 账号在哪 | 网关原本能看吗 |
| --- | --- | --- |
| workbuddy | `WB_AUTH_DIR`（宿主机目录） | 能 |
| Qoder | `/opt/qoder-hub/accounts/*.json`（容器卷，宿主机可读） | **不能** |
| Trae | 容器内 sqlite | **不能** |

结果就是：界面上对这两个平台点"刷新"，实际是空操作 —— 网关根本不知道它们存在。
2026-11-14 排查时确认，这两个平台已经连续六天没有任何调用（Qoder 64 次、Trae 58
次，都停在 11-08），而 workbuddy 同期 15 万次。

## 设计原则

1. **只读 + 最小写**：这个模块只读取账号元数据，唯一的写操作是"请求容器刷新"。
   它不直接改容器内的 token —— 那是容器自己的事，网关越权改会破坏容器的一致性
   假设（比如 `cooldownUntil` 与 `lastError` 的配对）。
2. **读不到就说读不到**：任何一个平台不可用时返回明确的 `available: false` 与原因，
   绝不返回空列表冒充"没有账号"。
3. **不外泄凭据**：token 一律不返回，只返回过期时间、剩余额度、健康标记。

## 数据来源

Qoder 的账号目录通过容器卷映射到宿主机（`/opt/qoder-hub/accounts`），可以直接读。
Trae 的账号在容器内 sqlite 里，需要 `docker exec` —— 这条路径在没有 docker 权限时
会失败，因此单独捕获并如实报告。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger('workbuddy.externalplatforms')

#: Qoder 账号目录（容器卷映射到宿主机）。可用环境变量覆盖，便于测试。
QODER_ACCOUNTS_DIR = Path(os.environ.get('WB_QODER_ACCOUNTS_DIR', '/opt/qoder-hub/accounts'))

#: Qoder 的 panel 密码文件（容器内 `/run/secrets/panel_password` 的来源）。
QODER_PANEL_SECRET = Path(os.environ.get('WB_QODER_PANEL_SECRET',
                                        '/opt/qoder-hub/secrets/panel_password'))

#: 容器名 → 平台名。刷新时用 `docker exec` 调容器内的刷新入口。
CONTAINERS = {'qoder': 'zhihui-qoder', 'trae': 'zhihui-trae'}

#: 过期告警阈值：剩余天数低于它就标红。
EXPIRY_WARN_DAYS = 7


def _days_left(expires_at) -> float | None:
    """距离过期的天数；拿不到过期时间返回 None（**不要**返回 0 或负数冒充）。"""
    try:
        value = float(expires_at)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return (value - time.time()) / 86400


def _redact(entry: dict) -> dict:
    """只保留可展示的字段，绝不返回 token 本身。"""
    credits = entry.get('credits') if isinstance(entry.get('credits'), dict) else {}
    expires_at = entry.get('expiresAt') or credits.get('expires_at')
    days = _days_left(expires_at)
    # `expiresAt` 可能是字符串、0、None 或垃圾值。统一折算成"可展示的整数或 None"，
    # 不要让一个坏字段把整条账号记录打挂 —— 那会让一个平台的账号全读不出来。
    expires_int = None
    if days is not None:
        try:
            expires_int = int(float(expires_at))
        except (TypeError, ValueError, OverflowError):
            expires_int = None
    return {
        'uid': entry.get('uid') or '',
        'nickname': entry.get('nickname') or '',
        'realm': entry.get('realm') or '',
        'domain': entry.get('domain') or '',
        'enabled': bool(entry.get('enabled', True)),
        'has_refresh_token': bool(str(entry.get('refreshToken') or '').strip()),
        'expires_at': expires_int,
        'expires_iso': (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_int))
                        if expires_int else None),
        'days_left': None if days is None else round(days, 1),
        'expiring_soon': bool(days is not None and days < EXPIRY_WARN_DAYS),
        'last_error': str(entry.get('lastError') or '')[:200],
        'cooldown_until': entry.get('cooldownUntil') or 0,
        'credits_remain': credits.get('remain'),
        'credits_size': credits.get('size'),
    }


def qoder_accounts() -> dict:
    """读取 Qoder 账号（只读）。失败时如实报告原因。"""
    directory = QODER_ACCOUNTS_DIR
    try:
        if not directory.is_dir():
            return {'available': False, 'platform': 'qoder',
                    'reason': f'账号目录不存在：{directory}', 'accounts': []}
        entries = []
        for path in sorted(directory.glob('*.json')):
            if path.name in ('settings.json', 'machine_identity.json'):
                continue
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                log.warning('qoder 账号文件读取失败 %s: %s', path.name, exc)
                continue
            if isinstance(raw, dict) and raw.get('uid'):
                entries.append(_redact(raw))
        return {'available': True, 'platform': 'qoder', 'accounts': entries,
                'count': len(entries),
                'expiring_soon': sum(1 for e in entries if e['expiring_soon'])}
    except Exception as exc:            # noqa: BLE001
        return {'available': False, 'platform': 'qoder',
                'reason': f'{type(exc).__name__}: {exc}', 'accounts': []}


def _docker_exec(container: str, argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
    """在容器里跑一条命令。返回 (退出码, 输出)。任何异常都折算成非零退出码。"""
    try:
        done = subprocess.run(['docker', 'exec', container, *argv],
                              capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, 'docker 命令不存在'
    except subprocess.TimeoutExpired:
        return 124, f'超时（{timeout} 秒）'
    except Exception as exc:            # noqa: BLE001
        return 1, f'{type(exc).__name__}: {exc}'
    output = (done.stdout or b'').decode('utf-8', 'replace')
    error = (done.stderr or b'').decode('utf-8', 'replace')
    return done.returncode, (output + error).strip()


def trae_accounts() -> dict:
    """读取 Trae 账号（只读）。

    Trae 的账号在容器内 sqlite 里，且管理面需要独立的 API Key —— 网关没有那条
    凭据，所以这里用容器内的只读查询。读不到时如实报告，不返回空列表冒充。
    """
    container = CONTAINERS['trae']
    code, output = _docker_exec(container, [
        'sh', '-c',
        'sqlite3 /app/data/data/aiwork.sqlite '
        '"SELECT name FROM sqlite_master WHERE type=\'table\';" 2>/dev/null || echo NO_SQLITE',
    ])
    if code == 127 or 'docker 命令不存在' in output:
        return {'available': False, 'platform': 'trae',
                'reason': '宿主机没有 docker 命令，无法读取容器内账号', 'accounts': []}
    if 'NO_SQLITE' in output or not output:
        return {'available': False, 'platform': 'trae',
                'reason': '容器内没有 sqlite3，或数据库不可读', 'accounts': []}
    return {'available': True, 'platform': 'trae', 'accounts': [],
            'count': 0, 'tables': output.splitlines()[:20],
            'note': 'Trae 的账号结构尚未映射，这里只确认容器可读；未做推测性解析。'}


def snapshot() -> dict:
    """两个平台的账号快照。任一个不可用时带着原因返回，绝不静默省略。"""
    return {
        'qoder': qoder_accounts(),
        'trae': trae_accounts(),
        'sampled_at': int(time.time()),
        'expiry_warn_days': EXPIRY_WARN_DAYS,
    }


def refresh(platform: str) -> dict:
    """请求容器刷新它自己的 token。

    **不做的事**：网关不直接改容器内的 token。容器的刷新逻辑要维护
    `refreshToken` / `expiresAt` / `lastError` / `cooldownUntil` 的配对关系，
    外部改一个字段会让它的状态机进入自相矛盾的状态。
    """
    if platform not in CONTAINERS:
        return {'ok': False, 'reason': f'未知平台：{platform}'}
    container = CONTAINERS[platform]
    if platform == 'qoder':
        # Qoder 容器内没有暴露可用的刷新 HTTP 端点（panel 密码与 API key 是两套
        # 鉴权，网关手里没有 panel 会话）。改用容器内的刷新脚本。
        code, output = _docker_exec(container, [
            'sh', '-c',
            'cd /app && python3 -c "'
            'import qoder_accounts as q; '
            'accs=q.load_accounts(q.ACCOUNTS_DIR) if hasattr(q,\'load_accounts\') else []; '
            'print(len(accs))" 2>&1 || echo UNSUPPORTED',
        ], timeout=60.0)
        return {'ok': False, 'platform': platform, 'container': container,
                'reason': '容器内刷新入口尚未接线（需要 panel 会话或专用脚本）',
                'probe': output[:300]}
    return {'ok': False, 'platform': platform, 'container': container,
            'reason': 'Trae 的刷新入口需要管理面 API Key，网关尚未持有'}
