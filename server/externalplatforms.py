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

#: Trae 的数据库。容器卷 `/opt/trae-work/data` 映射到容器的 `/app/data`，
#: 所以宿主机可以直接读，不需要 `docker exec`，也不需要容器内有 sqlite3。
TRAE_DB = Path(os.environ.get('WB_TRAE_DB',
                              '/opt/trae-work/data/data/aiwork.sqlite'))

#: 2026-11-22 起 Trae 侧由 trae2api-hub 托管，账号不再落在这个 sqlite 里。
#: 继续读它会得到一份早已停用的历史账号列表，让人误以为当前账号不可用。
#: 所以 trae_accounts() 先看引擎开关，hub 引擎走下面的 JSON 账号目录。
TRAE_ENGINE_SWITCH = Path(os.environ.get('WB_TRAE_ENGINE_SWITCH',
                                         '/opt/workbuddy-manager/engine/var/trae_engine'))
TRAE_HUB_ACCOUNTS_DIR = Path(os.environ.get('WB_TRAE_HUB_ACCOUNTS',
                                            '/opt/trae-hub/accounts'))
TRAE_HUB_HEALTH = os.environ.get('WB_TRAE_HUB_HEALTH',
                                 'http://127.0.0.1:8791/health')
TRAE_HUB_BASE = os.environ.get('WB_TRAE_HUB_BASE', 'http://127.0.0.1:8791')
TRAE_HUB_PASSWORD = Path(os.environ.get(
    'WB_TRAE_HUB_PASSWORD', '/opt/workbuddy-manager/engine/secrets/trae_hub_password'))

#: hub 面板会话令牌的进程内缓存：'token' -> (值, 到期时间戳)
_HUB_PANEL: dict = {}

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


def _redact_trae(payload: dict, user_id: str) -> dict:
    """Trae 账号的脱敏视图。

    ## 一个已经改正的判断错误

    最初这里给出 `needs_login`，依据是「`jwt` 与 `refresh_token` 都为空」。
    **那个判断是错的。**

    2026-11-14 实测：直接向引擎发一次请求，成功返回 200，同时——

    - `api_usage` 记下了这次调用（`{"total":{"requests":2,"ok":2}}`）；
    - `accounts` 表的 `updated_at` 被刷新到调用那一刻；
    - `conf/vault.stronghold`（加密存储）同步更新。

    也就是说：**真正的凭据在加密存储里，不在这个表**。表里的 `jwt` 为空是正常的，
    拿它推断「未登录」会得出与事实相反的结论。

    所以这里只报**事实字段**，不再给结论。是否可用应当看引擎的实际响应，
    而不是看某个字段是否为空。
    """
    jwt = str(payload.get('jwt') or '').strip()
    refresh = str(payload.get('refresh_token') or '').strip()
    fails = payload.get('refresh_token_fails')
    invalid = payload.get('refresh_token_invalid')
    return {
        'uid': str(payload.get('UserID') or user_id or ''),
        'nickname': str(payload.get('name') or ''),
        # 这两个只表示「元数据表里是否存了值」。为空**不代表**不可用 ——
        # 实际凭据在加密存储里，见上面的说明。
        'jwt_in_metadata': bool(jwt),
        'refresh_token_in_metadata': bool(refresh),
        'refresh_token_fails': fails if isinstance(fails, int) else 0,
        'refresh_token_invalid': bool(invalid) if invalid is not None else False,
        'added_at': str(payload.get('added_at') or ''),
        'updated_at': str(payload.get('updated_at') or ''),
        'auth_saved_at': str(payload.get('auth_saved_at') or ''),
    }


def _trae_engine() -> str:
    """当前 Trae 引擎名；读不到就返回空串（调用方按 aiwork 旧路径处理）。"""
    try:
        return TRAE_ENGINE_SWITCH.read_text(encoding='utf-8').strip().lower()
    except OSError:
        return ''


def _hub_health() -> dict:
    """hub 的 /health（含 auto_refresh 心跳）。取不到返回空 dict，不抛。"""
    try:
        import urllib.request
        with urllib.request.urlopen(TRAE_HUB_HEALTH, timeout=3) as handle:
            value = json.loads(handle.read().decode('utf-8'))
        return value if isinstance(value, dict) else {}
    except Exception:                   # noqa: BLE001
        return {}


def _redact_hub_account(raw: dict) -> dict:
    """hub 账号文件的脱敏视图。

    账号文件里既有 access/refresh token 也有绑设备用的私钥，**一个字节都不能
    返回给前端**。这里只取「能不能用、还能用多久」相关的字段。
    """
    extra = raw.get('extra') if isinstance(raw.get('extra'), dict) else {}
    profile = raw.get('account') if isinstance(raw.get('account'), dict) else {}
    expired_at = raw.get('expired_at') or 0
    refresh_expired_at = raw.get('refresh_expired_at') or 0
    days = _days_left(expired_at)
    try:
        expired_int = int(float(expired_at)) if expired_at else None
    except (TypeError, ValueError, OverflowError):
        expired_int = None
    try:
        refresh_int = int(float(refresh_expired_at)) if refresh_expired_at else None
    except (TypeError, ValueError, OverflowError):
        refresh_int = None
    return {
        'uid': str(raw.get('uid') or ''),
        'nickname': str(raw.get('nickname') or profile.get('username') or ''),
        'realm': str(raw.get('realm') or ''),
        'domain': str(raw.get('host') or ''),
        'enabled': bool(raw.get('enabled', True)),
        'has_refresh_token': bool(str(raw.get('refresh_token') or '').strip()),
        'expires_at': expired_int,
        'expires_iso': (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expired_int))
                        if expired_int else None),
        'refresh_expires_at': refresh_int,
        'days_left': None if days is None else round(days, 1),
        'expiring_soon': bool(days is not None and days < EXPIRY_WARN_DAYS),
        'last_error': str(raw.get('last_error') or '')[:200],
        'cooldown_until': 0,
        # 绑设备续期是轮换式的：设备身份在，续期才能成功。运维最需要看到这一项。
        'device_bound': bool(extra.get('privateKeyPEM') and extra.get('device_id')),
        'device_id': str(extra.get('device_id') or ''),
        'source': str(raw.get('source') or ''),
    }


def _hub_panel_token() -> str:
    """登一次 hub 面板拿会话令牌（进程内缓存，失败返回空串）。

    为什么不直接读账号文件：那些文件是 **0600 root**，而 Manager 以 10001 运行
    —— 直读会全部失败，再被当成「没有账号」，启动告警就会报出
    「trae has no usable accounts」这种与事实相反的结论（2026-11-22 真实发生）。
    hub 面板接口返回的本来就是脱敏视图，用它更稳。
    """
    cached = _HUB_PANEL.get('token')
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        password = TRAE_HUB_PASSWORD.read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    if not password:
        return ''
    try:
        import urllib.request
        body = json.dumps({'password': password}).encode('utf-8')
        request = urllib.request.Request(
            TRAE_HUB_BASE + '/panel/login', data=body,
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=5) as handle:
            value = json.loads(handle.read().decode('utf-8'))
        token = str((value or {}).get('token') or '')
    except Exception:                   # noqa: BLE001
        return ''
    if token:
        _HUB_PANEL['token'] = (token, time.time() + 600)
    return token


def _hub_http_accounts():
    """经 hub 面板接口取账号；任何一步失败返回 None（交给文件回退）。"""
    token = _hub_panel_token()
    if not token:
        return None
    try:
        import urllib.request
        request = urllib.request.Request(
            TRAE_HUB_BASE + '/accounts', headers={'X-Panel-Token': token})
        with urllib.request.urlopen(request, timeout=6) as handle:
            value = json.loads(handle.read().decode('utf-8'))
    except Exception:                   # noqa: BLE001
        _HUB_PANEL.pop('token', None)
        return None
    rows = (value or {}).get('accounts')
    if not isinstance(rows, list):
        return None
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        credits = row.get('credits') if isinstance(row.get('credits'), dict) else {}
        days = _days_left(row.get('expiresAt'))
        entries.append({
            'uid': str(row.get('uidTail') or row.get('uid') or ''),
            'uid_hash': str(row.get('uidHash') or ''),
            'nickname': str((row.get('account') or {}).get('username') or ''),
            'realm': str(row.get('realm') or ''),
            'domain': str(row.get('host') or ''),
            'enabled': bool(row.get('enabled', True)),
            'status': str(row.get('status') or ''),
            'has_refresh_token': bool(row.get('hasRefreshToken')),
            'expires_at': row.get('expiresAt'),
            'expires_iso': row.get('expiresIso'),
            'expires_in': row.get('expiresIn'),
            'refresh_expires_iso': row.get('refreshExpiresIso'),
            'refresh_expires_in': row.get('refreshExpiresIn'),
            'days_left': None if days is None else round(days, 1),
            'expiring_soon': bool(days is not None and days < EXPIRY_WARN_DAYS),
            'last_error': str(row.get('lastError') or '')[:200],
            'cooldown_for': row.get('cooldownFor'),
            'device_bound': bool(row.get('deviceBound')),
            'device_id': str(row.get('deviceId') or ''),
            # 2026-11-22：hub 的 credits 是
            # {plan, total, limit, used, remain, checkin, checked_in}，
            # **没有 `size`** —— 只读 size 会让 total 恒为 None，余额占比算不出来。
            'credits_remain': credits.get('remain'),
            'credits_size': (credits.get('size') or credits.get('total')
                             or credits.get('limit')),
            'credits_used': credits.get('used'),
            'credits_plan': credits.get('plan') or '',
            'source': str(row.get('source') or ''),
        })
    return {'accounts': entries, 'via': 'hub-panel-api'}


def _trae_hub_accounts() -> dict:
    """hub 引擎下的 Trae 账号。

    优先走 hub 面板接口（脱敏视图、与 hub 内存状态一致）；接口不可用
    （未拿到会话、hub 未起）再退化为直接读 JSON 账号目录 —— 那条路需要 root。
    """
    via_api = _hub_http_accounts()
    if via_api is not None:
        health = _hub_health()
        auto = health.get('auto_refresh') if isinstance(health.get('auto_refresh'), dict) else {}
        rows = via_api['accounts']
        return {
            'available': True, 'platform': 'trae', 'engine': 'hub',
            'via': via_api['via'],
            'accounts': rows,
            'count': len(rows),
            'expiring_soon': sum(1 for row in rows if row.get('expiring_soon')),
            'refresh_owner': 'trae2api-hub（容器内自持，每 30 分钟检查一次）',
            'auto_refresh': {
                'enabled': bool(auto.get('enabled')),
                'last_check_at': auto.get('last_check_at'),
                'last_ok_at': auto.get('last_ok_at'),
                'last_error': str(auto.get('last_error') or ''),
                'refreshes': int(auto.get('refreshes') or 0),
                'next_check_at': auto.get('next_check_at'),
            } if auto else None,
            'note': ('账号经 hub 面板接口读取（脱敏视图，不含任何令牌）；'
                     '续期由 hub 自己完成，网关只读不写。'),
        }
    directory = TRAE_HUB_ACCOUNTS_DIR
    if not directory.is_dir():
        return {'available': False, 'platform': 'trae', 'engine': 'hub',
                'reason': f'账号目录不存在，面板接口也不可用：{directory}',
                'accounts': []}
    entries = []
    for path in sorted(directory.glob('*.json')):
        if path.name in ('settings.json', 'machine_identity.json'):
            continue
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            log.warning('trae hub 账号文件读取失败 %s: %s', path.name, exc)
            continue
        if isinstance(raw, dict) and raw.get('uid'):
            entries.append(_redact_hub_account(raw))
    health = _hub_health()
    auto = health.get('auto_refresh') if isinstance(health.get('auto_refresh'), dict) else {}
    return {
        'available': True, 'platform': 'trae', 'engine': 'hub',
        'accounts': entries,
        'count': len(entries),
        'expiring_soon': sum(1 for e in entries if e.get('expiring_soon')),
        # 「谁在刷新」必须明确：轮换式 refresh token 只能有一个持有者，
        # 网关若也去刷，两边会互相把对方的令牌作废。
        'refresh_owner': 'trae2api-hub（容器内自持，每 30 分钟检查一次）',
        'auto_refresh': {
            'enabled': bool(auto.get('enabled')),
            'last_check_at': auto.get('last_check_at'),
            'last_ok_at': auto.get('last_ok_at'),
            'last_error': str(auto.get('last_error') or ''),
            'refreshes': int(auto.get('refreshes') or 0),
            'next_check_at': auto.get('next_check_at'),
        } if auto else None,
        'note': ('账号来自 trae2api-hub 的 JSON 存储；令牌续期由 hub 自己完成，'
                 '网关只读不写 —— 轮换式 refresh token 不允许两个持有者。'),
    }


def trae_accounts() -> dict:
    """读取 Trae 账号（只读）。

    Trae 的数据库通过容器卷映射到宿主机（`/opt/trae-work/data/data/aiwork.sqlite`），
    所以不需要 `docker exec`，也不需要容器内有 sqlite3 —— 用 Python 自带的 sqlite3
    直接读。

    ## 为什么专门标出「凭据为空」

    2026-11-14 实测：直接向引擎发一次请求，成功返回 200，同时账号表的更新时间被
    刷新到调用那一刻，`conf/vault.stronghold`（加密存储）同步更新。

    **结论：真正的凭据在加密存储里，不在这个表。** 表里的令牌字段为空是正常的。

    所以这里只报事实字段，不给「要不要重新登录」的结论 —— 拿字段是否为空去推断
    可用性，会得出与事实相反的答案。要看可用性，请看引擎的实际响应。
    """
    engine = _trae_engine()
    if engine == 'hub':
        return _trae_hub_accounts()
    path = TRAE_DB
    if not path.is_file():
        return {'available': False, 'platform': 'trae',
                'reason': f'数据库不存在：{path}', 'accounts': []}
    try:
        import sqlite3
        # 只读打开，且用 timeout 避免和容器里的写入撞上（WAL 下读不会被写阻塞，
        # 但元数据锁仍需等待）。
        handle = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5.0)
        try:
            handle.row_factory = sqlite3.Row
            rows = handle.execute('SELECT user_id, data FROM accounts').fetchall()
            keys = handle.execute('SELECT count(*) AS c FROM api_keys').fetchone()
        finally:
            handle.close()
    except Exception as exc:             # noqa: BLE001
        return {'available': False, 'platform': 'trae',
                'reason': f'读取失败：{type(exc).__name__}: {exc}', 'accounts': []}

    entries = []
    for row in rows:
        try:
            payload = json.loads(row['data'] if not isinstance(row, tuple) else row[1])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        entries.append(_redact_trae(payload, row[0] if not isinstance(row, tuple) else row[0]))

    return {
        'available': True, 'platform': 'trae', 'accounts': entries,
        'count': len(entries),
        'api_keys': keys['c'] if keys else 0,
        'note': ('这里是元数据表的内容。实际凭据存放在同级 conf 目录的加密存储里，'
                 '因此本表里令牌字段为空**不代表**未登录 —— 判断可用性请看引擎的'
                 '实际响应。'),
    }


def snapshot() -> dict:
    """两个平台的账号快照。任一个不可用时带着原因返回，绝不静默省略。"""
    return {
        'platforms': ['workbuddy', 'trae'],
        'trae': trae_accounts(),
        'sampled_at': int(time.time()),
        'expiry_warn_days': EXPIRY_WARN_DAYS,
        # Qoder 已下线（2026-11-22）：官方不再提供每日领取的免费积分。
        #
        # 键**保留**，但给的是"已移除"的形状而不是直接删掉：前端老构建会读
        # `data.qoder.accounts`，删键会让它拿到 undefined 直接报错。形状保持
        # 兼容（available/accounts/count），语义交给 removed 标记。
        'qoder': {
            'available': False,
            'removed': True,
            'platform': 'qoder',
            'accounts': [],
            'count': 0,
            'expiring_soon': 0,
            'at': '2026-11-22',
            'reason': ('Qoder 不再提供每日领取的免费积分，智汇网关已移除该平台。'
                       '我们正在积极寻找其他免费额度的反向代理，敬请期待。'),
        },
    }


def refresh(platform: str) -> dict:
    """请求容器刷新它自己的 token。

    **不做的事**：网关不直接改容器内的 token。容器的刷新逻辑要维护
    `refreshToken` / `expiresAt` / `lastError` / `cooldownUntil` 的配对关系，
    外部改一个字段会让它的状态机进入自相矛盾的状态。
    """
    if platform == 'qoder':
        return {'ok': False, 'removed': True,
                'reason': 'Qoder 已在 2026-11-22 从智汇网关移除（官方停止每日免费积分）。'}
    if platform != 'trae':
        return {'ok': False, 'reason': f'未知平台：{platform}'}
    # hub 引擎下**禁止**由网关触发刷新：refresh token 是轮换式的，两个持有者
    # 各自刷新会把对方的令牌作废（2026-11-21 已经因此毁过一次凭证）。
    if _trae_engine() == 'hub':
        health = _hub_health()
        auto = health.get('auto_refresh') if isinstance(health.get('auto_refresh'), dict) else {}
        return {'ok': True, 'triggered': False, 'engine': 'hub',
                'reason': ('Trae 由 trae2api-hub 自持续期，网关不代刷。'
                           '轮换式令牌只允许一个持有者。'),
                'auto_refresh': auto or None}
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
