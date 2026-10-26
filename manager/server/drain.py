"""发布期排空与在途计数（K12）。

Manager 是三平台共同的公开数据面，发布时不能用"进程 3 秒退出"来代表安全：
- 进入 draining 后 `/api/readyz` 立即返回 503，入口据此把新流量切走；
- 已经接单的请求继续跑完（uvicorn graceful shutdown + 本模块的在途计数）；
- lifespan 退出时等待在途归零，超过预算才放弃，并把剩余数量写进日志。

边界：本模块只提供**观测与等待**，不负责杀流。发布记录里"接入中断"和
"流截断"必须分开记（见 03-最小停机发布方案 §1），不能用一个数字代替。
"""
from __future__ import annotations

import asyncio
import time
from threading import RLock

_started_at: float | None = None
_counts: dict[str, int] = {}
_shutdown_started = False
_lock = RLock()


def begin(*, shutdown: bool = False) -> None:
    """进入 draining（幂等）。

    与实例租约联动：本进程若持有 `instance_leases` 行，role 变 `draining`
    并写入四类在途计数快照（供交接时间线复核）。**readyz 语义不变**。
    """
    global _started_at, _shutdown_started
    with _lock:
        if _started_at is None:
            _started_at = time.time()
        _shutdown_started = _shutdown_started or shutdown
    _sync_instance_role('draining')


def resume() -> bool:
    """Abort a pre-stop deployment drain; a terminating process cannot resume."""
    global _started_at
    with _lock:
        if _shutdown_started:
            return False
        _started_at = None
    _sync_instance_role('serving')
    return True


def snapshot() -> dict:
    with _lock:
        return {'draining': is_draining(), 'shutdown_started': _shutdown_started,
                'started_at': _started_at, 'in_flight': in_flight(),
                'total_in_flight': total_in_flight()}


def is_draining() -> bool:
    return _started_at is not None


def started_at() -> float | None:
    return _started_at


def track(kind: str = 'http'):
    """在途计数上下文管理器（请求进入 +1、退出 -1，异常也成对）。"""
    class _Tracker:
        def __enter__(self):
            self.kind = kind
            self.active = True
            with _lock:
                _counts[self.kind] = _counts.get(self.kind, 0) + 1
            return self

        def set_kind(self, new_kind):
            with _lock:
                if self.active and new_kind != self.kind:
                    _counts[self.kind] = max(0, _counts.get(self.kind, 0) - 1)
                    self.kind = new_kind
                    _counts[self.kind] = _counts.get(self.kind, 0) + 1

        def __exit__(self, *exc):
            with _lock:
                if self.active:
                    _counts[self.kind] = max(0, _counts.get(self.kind, 0) - 1)
                    self.active = False
            return False

    return _Tracker()


def in_flight() -> dict[str, int]:
    """当前在途请求，按类别。"""
    with _lock:
        return {k: v for k, v in _counts.items() if v}


def total_in_flight() -> int:
    with _lock:
        return sum(_counts.values())


def classify(method: str, path: str, headers: dict | None = None) -> str:
    """把请求归类，供在途计数与发布记录区分。

    inference_stream / inference / write / read —— 四类的中断后果不同：
    流式推理被截断是用户可感知的失败，读请求重试即可。
    """
    headers = headers or {}
    accept = ''
    for k, v in headers.items():
        if str(k).lower() == 'accept':
            accept = str(v).lower()
            break
    if path.startswith(('/v1/', '/v2/', '/qoder/v1/', '/trae/v1/',
                        '/workbuddy/v1/', '/wb/v1/')):
        return 'inference_stream' if 'text/event-stream' in accept else 'inference'
    if method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return 'write'
    return 'read'


async def wait_until_drained(timeout: float, interval: float = 0.1) -> dict:
    """等待在途归零。

    Returns:
        {'drained': bool, 'waited_sec': float, 'remaining': dict}
    """
    began = time.monotonic()
    while total_in_flight() > 0 and (time.monotonic() - began) < timeout:
        await asyncio.sleep(interval)
    return {
        'drained': total_in_flight() == 0,
        'waited_sec': round(time.monotonic() - began, 3),
        'remaining': in_flight(),
    }


def reset_for_tests() -> None:
    """仅测试用：清空状态。"""
    global _started_at, _shutdown_started
    _started_at = None
    _shutdown_started = False
    _counts.clear()


def handoff_snapshot() -> dict:
    """交接观测快照（四类在途计数 + readyz 语义 + 单调时钟）。

    写进 `instance_leases.drain_snapshot`，让交接时间线可以事后从 DB 复核，
    不必依赖某一方的日志。**只读聚合**：不改 drain 状态、不改 readyz 语义。
    """
    return {
        't_mono': round(time.monotonic(), 6),
        't_epoch': int(time.time()),
        'draining': is_draining(),
        'shutdown_started': _shutdown_started,
        'readyz_ok': not is_draining(),
        'in_flight': in_flight(),
        'total_in_flight': total_in_flight(),
    }


def _sync_instance_role(role: str) -> None:
    """把本进程的实例租约与 drain 状态联动（serving ↔ draining）。

    三个约束：
    * drain 必须能在**没有**实例租约时正常工作（测试、未启用交接的部署）——
      租约里没有本进程的行时 `sync_role` 静默返回 False；
    * 只在 drain.begin()/resume()（低频控制路径）同步，**绝不能**放进
      `DrainMiddleware`（每请求路径），否则每个请求都要写一次 DB；
    * 任何失败都不得影响 drain 本身 —— readyz 语义与在途计数不依赖租约。
    """
    try:
        from . import instancelease
        instancelease.sync_role(role, snapshot=handoff_snapshot())
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            '实例租约同步失败（不影响 drain 本身） role=%s', role)


class DrainMiddleware:
    """Track the complete ASGI response, including its streaming body.

    A deployment controller must begin drain before sending SIGTERM, move new
    admission away, and abort/resume if old requests do not finish in its window.
    Monitoring and the authenticated drain controller remain reachable.
    """

    CONTROL_PATHS = frozenset({'/healthz', '/readyz', '/api/healthz',
                               '/api/readyz', '/api/system/drain'})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path', '').rstrip('/') in self.CONTROL_PATHS:
            await self.app(scope, receive, send)
            return
        if is_draining():
            from starlette.responses import JSONResponse
            response = JSONResponse(
                {'error': {'type': 'service_unavailable', 'code': 'gateway_draining',
                           'message': 'Gateway is draining for maintenance; retry later.'}},
                status_code=503, headers={'Retry-After': '1', 'Connection': 'close'})
            await response(scope, receive, send)
            return
        headers = {k.decode('latin-1'): v.decode('latin-1') for k, v in scope.get('headers', [])}
        kind = classify(scope.get('method', 'GET'), scope.get('path', ''), headers)
        with track(kind) as tracker:
            async def counted_send(message):
                if message['type'] == 'http.response.start' and kind.startswith('inference'):
                    content_type = next((v for k, v in message.get('headers', [])
                                         if k.lower() == b'content-type'), b'')
                    if content_type.lower().startswith(b'text/event-stream'):
                        tracker.set_kind('inference_stream')
                await send(message)
            await self.app(scope, receive, counted_send)
