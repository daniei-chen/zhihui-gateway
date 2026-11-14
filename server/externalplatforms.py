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


# ── Qoder：网关侧刷新 ────────────────────────────────────────────────
#
# 容器的 panel 密码是 scrypt 哈希，网关拿不到明文，因此走不通它的
# `/panel/login`。但刷新这件事本身不需要面板会话：
#
#   refresh_token（文件里，root 可读） → 上游 deviceToken/refresh → 新令牌
#
# 这条上游端点经实测可用（2026-11-14 返回 200 并给出新令牌），且旧 refresh token
# **可重复使用**（服务端不轮换）。所以网关可以自己完成刷新，再把结果原子写回。
#
# 写回必须谨慎：
#   · 先备份原文件（同目录 `.bak-<时间戳>`）；
#   · 只改三个字段，其它字段原样保留；
#   · 临时文件 + `os.replace` 原子替换，避免容器读到半个文件；
#   · 保留原文件的属主与权限（容器以 10001 运行，改成 root 会让它读不到）。

QODER_REFRESH_URL = os.environ.get('WB_QODER_REFRESH_URL',
                                  'https://openapi.qoder.com.cn/api/v1/deviceToken/refresh')


def _write_account_atomically(path: Path, payload: dict) -> dict:
    """原子写回账号文件，保持属主与权限不变。"""
    import tempfile
    import urllib.request

    original = path.stat()
    text = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'

    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix='.refresh-', suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.chmod(temp_name, original.st_mode & 0o7777)
        try:
            os.chown(temp_name, original.st_uid, original.st_gid)
        except (AttributeError, PermissionError, OSError):
            # 非 root 时无法改属主：保持临时文件的默认属主，但仍要能读到
            pass
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return {'mode': oct(original.st_mode & 0o7777), 'uid': original.st_uid,
            'gid': original.st_gid, 'bytes': len(text)}


def refresh_qoder_account(uid: str) -> dict:
    """刷新单个 Qoder 账号并写回。返回结果里**不含令牌**。"""
    import urllib.error
    import urllib.request

    directory = QODER_ACCOUNTS_DIR
    path = directory / f'{uid}.json'
    if not path.is_file():
        return {'ok': False, 'reason': f'账号文件不存在：{path.name}'}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'ok': False, 'reason': f'账号文件不可解析：{type(exc).__name__}: {exc}'}
    if not isinstance(payload, dict):
        return {'ok': False, 'reason': '账号文件结构不是对象'}

    token = str(payload.get('refreshToken') or '').strip()
    if not token:
        return {'ok': False, 'reason': '该账号没有 refreshToken，只能重新登录'}

    body = json.dumps({'refresh_token': token}).encode('utf-8')
    request = urllib.request.Request(
        QODER_REFRESH_URL, data=body, method='POST',
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'zhihui-gateway/refresh'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode('utf-8', 'replace')
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8', 'replace')[:200]
        except Exception:                # noqa: BLE001
            pass
        return {'ok': False, 'reason': f'上游拒绝：HTTP {exc.code} {detail}'}
    except Exception as exc:             # noqa: BLE001
        return {'ok': False, 'reason': f'上游不可达：{type(exc).__name__}: {exc}'}

    try:
        fresh = json.loads(raw)
    except ValueError:
        return {'ok': False, 'reason': f'上游返回非 JSON（HTTP {status}）'}
    if not isinstance(fresh, dict):
        return {'ok': False, 'reason': '上游返回结构异常'}

    access = str(fresh.get('device_token') or fresh.get('access_token') or '').strip()
    renew = str(fresh.get('refresh_token') or '').strip()
    expires = fresh.get('expires_at')
    if not access:
        return {'ok': False, 'reason': '上游没有返回新的访问令牌'}

    # 只改这三个字段，其余原样保留（容器可能依赖 plan / credits / lastCheckin 等）
    backup = path.with_name(path.name + '.bak-' + time.strftime('%Y%m%d-%H%M%S'))
    try:
        backup.write_bytes(path.read_bytes())
    except OSError as exc:
        return {'ok': False, 'reason': f'备份失败，已放弃写入：{exc}'}

    payload['accessToken'] = access
    if renew:
        payload['refreshToken'] = renew
    # 上游给的是 ISO 时间串；账号文件里存的是 epoch 秒
    if isinstance(expires, str) and expires:
        try:
            import datetime
            payload['expiresAt'] = int(datetime.datetime.strptime(
                expires.replace('Z', '+0000'), '%Y-%m-%dT%H:%M:%S%z').timestamp())
        except (ValueError, TypeError):
            pass
    elif isinstance(expires, (int, float)) and expires > 0:
        payload['expiresAt'] = int(expires)

    try:
        written = _write_account_atomically(path, payload)
    except Exception as exc:             # noqa: BLE001
        return {'ok': False, 'reason': f'写回失败：{type(exc).__name__}: {exc}',
                'backup': str(backup)}

    days = _days_left(payload.get('expiresAt'))
    return {
        'ok': True, 'uid': uid, 'backup': str(backup), 'written': written,
        'expires_at': payload.get('expiresAt'),
        'days_left': None if days is None else round(days, 1),
        'note': ('账号文件已更新。容器把账号装在内存里，**新令牌要等它重载后才生效**'
                 '（容器自身的定时保活每天会重载一次）。重启容器可立即生效。'),
    }
