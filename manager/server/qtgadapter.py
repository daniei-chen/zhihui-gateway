"""QTG 适配器契约客户端（U3 阶段 2）：Manager 读引擎侧数据。

## 为什么需要它

U3 的结论是「QTG 报引擎侧真实数据、Manager 报自己的账本，两者同名不同源」。
所以控制面收敛不是删掉 QTG 的实现，而是让它以稳定契约把引擎侧数据交给
Manager，Manager 再合成统一入口。QTG 侧已实现 `/api/v1/adapter/*`
（见 `qtg/app/qtg/routers/adapter.py`），本模块是消费方。

对称关系：K26 的 `read_api` 是「QTG 读 Manager」，这里是「Manager 读 QTG」。

## 三条硬约束

1. **降级必须可见**：未配置、令牌缺失、超时、单端点失败，全部写进
   `available` / `reason` / `degraded`，**绝不静默返回空列表**。
   引擎上真的没有账号，和读不到账号，是两件完全不同的事——前者是业务状态，
   后者是故障，混在一起会让人对着「零账号」面板查半天。
2. **不重试、不放大**：单次 GET，超时即放弃。引擎侧慢的时候重试只会把它压得
   更狠；要更新鲜的数据，调用方下一轮再取。
3. **不碰业务账本**：这是观测通道，不写 `request_logs` / `usage_daily`。

## 为什么不自己造 client

`config.http_client()` 已经是统一出口：明确忽略系统/环境代理、按超时与 lane
复用 transport 池、关闭时归还 lease 而不是销毁。这里额外传 `proxy=''`
**强制直连**——内部通道被面板设置的出口代理劫持，会变成「时好时坏」，
这类间歇故障最难归因。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from . import config

#: 契约端点。顺序固定，返回结构里按名字挂。
ENDPOINTS = ('contract', 'health', 'accounts', 'models', 'usage')

#: 期望的契约版本。
#:
#: 刻意**不**自动跟随 QTG 侧改动：自动跟随等于「上游改字段、下游静默跑歪」。
#: QTG 提升 CONTRACT_VERSION 时，这里必须显式改并同步测试。
EXPECTED_CONTRACT_VERSION = 1

#: usage 的窗口上限（与 QTG 侧 `Query(default=14)` + `min(90, ...)` 对齐）。
_DAYS_MAX = 90


def _iso_now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _read_token() -> tuple[str | None, str | None]:
    """读内部服务令牌。返回 `(token, reason)`；读不到时 reason 说明原因。"""
    path = Path(config.QTG_ADAPTER_TOKEN_FILE)
    try:
        token = path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return None, 'token_file_missing'
    except OSError:
        return None, 'token_file_unreadable'
    if not token:
        return None, 'token_file_empty'
    return token, None


def _overview(*, available: bool, reason: str | None,
              contract_version: int | None = None,
              data: dict[str, Any] | None = None,
              degraded: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        'source': 'qtg_adapter',
        'available': available,
        'reason': reason,
        'contract_version': contract_version,
        'fetched_at': _iso_now(),
        'data': data or {},
        'degraded': degraded or [],
    }


async def _fetch_one(client, base: str, token: str, name: str,
                     params: dict[str, Any] | None = None
                     ) -> tuple[str, Any, str | None]:
    """取单个端点。返回 `(name, body | None, reason | None)`，永不抛异常。"""
    import httpx

    url = f'{base}/api/v1/adapter/{name}'
    try:
        response = await client.get(
            url,
            headers={'X-QTG-Internal-Auth': token},
            params=params or {},
        )
    except httpx.TimeoutException:
        return name, None, 'timeout'
    except httpx.HTTPError as exc:            # 连接失败 / TLS / 协议错误
        return name, None, f'transport_error:{type(exc).__name__}'
    if response.status_code != 200:
        return name, None, f'http_{response.status_code}'
    try:
        return name, response.json(), None
    except ValueError:
        return name, None, 'invalid_json'


async def fetch_overview(*, days: int = 14, client=None) -> dict[str, Any]:
    """取引擎侧总览。

    参数
    ----
    days
        usage 的窗口（天）。夹到 `[1, 90]`，与 QTG 侧同口径。
    client
        仅测试注入。传入时**不关闭**（生命周期由调用方管）。

    返回
    ----
    dict，字段见 `_overview()`。`available=False` 时 `reason` 一定非空；
    `available=True` 也可能带 `degraded`（部分端点失败，其余仍可用）。
    """
    base = (config.QTG_ADAPTER_BASE or '').rstrip('/')
    if not base:
        return _overview(available=False, reason='adapter_not_configured')

    token, reason = _read_token()
    if token is None:
        return _overview(available=False, reason=reason)

    try:
        window = max(1, min(_DAYS_MAX, int(days)))
    except (TypeError, ValueError):
        window = 14

    timeout = config.QTG_ADAPTER_TIMEOUT_S
    own_client = client is None
    if own_client:
        client = config.http_client(max(1, timeout), proxy='')
    try:
        results = await asyncio.gather(*(
            _fetch_one(client, base, token, name,
                       {'days': window} if name == 'usage' else None)
            for name in ENDPOINTS
        ))
    finally:
        if own_client:
            await client.aclose()

    data: dict[str, Any] = {}
    degraded: list[dict[str, Any]] = []
    for name, body, why in results:
        if why is not None:
            degraded.append({'endpoint': name, 'reason': why})
        else:
            data[name] = body

    contract_version = None
    contract = data.get('contract')
    if isinstance(contract, dict):
        raw_version = contract.get('contract_version')
        if isinstance(raw_version, int):
            contract_version = raw_version
        if contract_version != EXPECTED_CONTRACT_VERSION:
            # 版本不符不阻断：仍然把能读到的数据交出去，但必须显式标出来，
            # 让调用方知道字段可能对不上，而不是假装兼容。
            degraded.append({
                'endpoint': 'contract',
                'reason': 'contract_version_mismatch',
                'expected': EXPECTED_CONTRACT_VERSION,
                'actual': contract_version,
            })

    # 模型目录的失败原因是端点自己报的（`errors` 字段），不是 HTTP 层能看出来的：
    # 上游模型接口挂了，端点仍会 200 + 空数组。不把它透到 degraded，调用方就会把
    # "引擎取不到模型"读成"引擎上没有模型"。
    models_body = data.get('models')
    if isinstance(models_body, dict):
        for detail in models_body.get('errors') or []:
            degraded.append({'endpoint': 'models',
                             'reason': 'upstream_error',
                             'detail': str(detail)[:200]})

    if not data:
        return _overview(available=False, reason='all_endpoints_failed',
                         degraded=degraded)

    return _overview(available=True, reason=None,
                     contract_version=contract_version,
                     data=data, degraded=degraded)
