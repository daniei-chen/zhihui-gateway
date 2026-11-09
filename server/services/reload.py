"""Observe account hot-loading without restarting serving inference.

Legacy configuration/restart entry points remain separate. Adding an account is
not permission to restart an engine; a failed status read is not proof it needs
a restart. A short engine shutdown grace period cannot protect long SSE calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from .. import upstreamsvc
from . import wb2api

logger = logging.getLogger('workbuddy.reload')

# 合并窗口：这段时间内的多次请求只触发一次重启
COALESCE_SECONDS = 0.8

_state: dict = {
    'running': False,
    'pending': False,
    'last_at': 0.0,
    'last_ok': None,
    'last_message': '',
    'restart_count': 0,
}
_pending = False
_worker: asyncio.Task | None = None
_lock = asyncio.Lock()


def state() -> dict:
    """当前重载状态，供前端展示。"""
    result = dict(_state)
    if isinstance(result.get('account_reload'), dict):
        result['account_reload'] = dict(result['account_reload'])
    result['account_reload_scopes'] = [dict(value) for value in _account_reload_states.values()]
    return result


async def _worker_loop() -> None:
    global _pending
    async with _lock:
        _state['running'] = True
        try:
            while _pending:
                _pending = False
                _state['pending'] = False
                # 等一小段，把紧挨着的多次改动合并成一次重启
                await asyncio.sleep(COALESCE_SECONDS)
                if _pending:
                    # 等待期间又有新请求，重新开始计时
                    continue
                ok, message = await wb2api.restart_container()
                _state['restart_count'] += 1
                _state['last_at'] = time.time()
                _state['last_ok'] = ok
                _state['last_message'] = message
        finally:
            _state['running'] = False
            _state['pending'] = False


def request_restart(upstream: dict | None = None) -> bool:
    """请求一次上游重载（幂等：短时间内多次调用只重启一次）。

    upstream 非空 = 该**分组**（多分组 / 账号池）：登记了容器名就后台
    `docker restart` 它；没登记就**什么都不做**——账号文件的增删改会被上游的
    auths 热加载（约 5 秒）自动收录，为「立刻生效」去重启别的对象才是灾难。
    默认上游（upstream=None）保持既有行为：合并窗口 + 后台重启 + 状态上报。

    立即返回，不阻塞请求；实际重启在后台完成。
    返回 False 表示没有运行中的事件循环，无法调度后台任务。
    """
    if upstream is not None:
        container = str(upstream.get('container') or '').strip()
        if not container:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(_restart_container_bg(container))
        return True
    global _pending, _worker
    _pending = True
    _state['pending'] = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环：无法后台调度，交由调用方决定是否同步执行
        return False
    if _worker is None or _worker.done():
        _worker = loop.create_task(_worker_loop())
    return True


async def _restart_container_bg(container: str) -> None:
    """分组容器的后台重启，结果照常记进 reload 状态（设置页可见）。"""
    ok, message = await wb2api.restart_container(container)
    _state['restart_count'] += 1
    _state['last_at'] = time.time()
    _state['last_ok'] = ok
    _state['last_message'] = message


async def restart_now(upstream: dict | None = None) -> tuple[bool, str]:
    """立即重启并等待结果，供「重启上游」这类显式操作使用。

    upstream 非空 = 该**分组**：没登记容器名时明确报错——绝不能拿默认上游的
    容器名去顶（那会重启另一个实例，而用户以为重启的是这一组）。
    """
    if upstream is None:
        return await wb2api.restart_container()
    container = str(upstream.get('container') or '').strip()
    if not container:
        return False, '该分组未配置容器名，无法从面板重启（账号改动由上游热加载自动收录）'
    return await wb2api.restart_container(container)


# Account observation has its own state; it must not overwrite the last explicit
# restart result or turn a configuration-save notice into a false success.
HOT_RELOAD_WAIT_SECONDS = 8.0    # 上游热加载周期约 5 秒，留一次往返的余量
HOT_RELOAD_POLL_SECONDS = 1.0
MAX_RELOAD_SCOPES = 64
MAX_PENDING_ACCOUNTS = 256
_pending_uids: set[str] = set()
_reload_worker: asyncio.Task | None = None
_account_reload_states: dict[str, dict] = {}


def _record_account_reload(scope, upstream_id, phase, reason, pending):
    item = {'upstream_id': upstream_id, 'phase': phase, 'reason': reason,
            'pending_count': len(pending), 'observed_at': time.time(),
            'restart_scheduled': False}
    _state['account_reload'] = item
    _account_reload_states.pop(scope, None)
    _account_reload_states[scope] = item
    while len(_account_reload_states) > MAX_RELOAD_SCOPES:
        _account_reload_states.pop(next(iter(_account_reload_states)))


async def _observe_accounts(pending, fetch, wait_seconds, *, scope, upstream_id):
    saw_upstream = False
    _record_account_reload(scope, upstream_id, 'observing', '', pending)
    try:
        async with asyncio.timeout(wait_seconds):
            while pending:
                try:
                    status = await fetch()
                except Exception:
                    status = None
                if isinstance(status, dict) and status.get('connected') is True:
                    rows = status.get('accounts')
                    if isinstance(rows, list) and all(isinstance(a, dict) for a in rows):
                        saw_upstream = True
                        seen = {a['uid'] for a in rows if isinstance(a.get('uid'), str) and a['uid']}
                        pending.difference_update(seen)
                        if not pending:
                            _record_account_reload(scope, upstream_id, 'confirmed', '', pending)
                            return
                await asyncio.sleep(HOT_RELOAD_POLL_SECONDS)
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        _record_account_reload(scope, upstream_id, 'needs_attention', 'observation_cancelled', pending)
        pending.clear()
        raise
    _record_account_reload(scope, upstream_id, 'needs_attention',
                           'account_not_observed' if saw_upstream else 'upstream_unavailable', pending)
    pending.clear()
    logger.warning('Account hot-load not confirmed; serving engine was not restarted')


async def _reload_worker_loop(wait_seconds: float) -> None:
    if not _pending_uids:
        return
    await _observe_accounts(_pending_uids, wb2api.get_status, wait_seconds,
                            scope='default', upstream_id=None)


def request_reload_or_restart(uid: str, *, upstream: dict | None = None) -> bool:
    """Compatibility name: schedule bounded hot-load observation, never restart."""
    if upstream is not None:
        return _request_group_reload(uid, upstream)
    global _reload_worker
    uid = str(uid or '').strip()
    if not uid or len(uid) > 256 or len(_pending_uids) >= MAX_PENDING_ACCOUNTS:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if uid:
        _pending_uids.add(uid)
    if _reload_worker is None or _reload_worker.done():
        _reload_worker = loop.create_task(_reload_worker_loop(HOT_RELOAD_WAIT_SECONDS))
    return True


# ── 分组（多账号池）专用的热加载等待 ──────────────────────────────────
#
# Route and credential generations must not join a previous group's waiter.
_group_pending: dict[str, set[str]] = {}
_group_workers: dict[str, asyncio.Task] = {}


def _group_identity(upstream: dict, api_key: str) -> str:
    values = (str(upstream.get('id') or ''), str(upstream.get('base_url') or '').strip(),
              api_key)
    return hashlib.sha256(repr(values).encode()).hexdigest()


def _request_group_reload(uid: str, upstream: dict) -> bool:
    base = str(upstream.get('base_url') or '').strip()
    if not base:
        return False
    api_key = upstreamsvc.forward_api_key(upstream)
    scope = _group_identity(upstream, api_key)
    uid = str(uid or '').strip()
    if (not uid or len(uid) > 256
            or (scope not in _group_pending and len(_group_pending) >= MAX_RELOAD_SCOPES)
            or len(_group_pending.get(scope, ())) >= MAX_PENDING_ACCOUNTS):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _group_pending.setdefault(scope, set()).add(uid)
    task = _group_workers.get(scope)
    if task is None or task.done():
        task = loop.create_task(_group_reload_worker(dict(upstream), scope=scope, api_key=api_key))
        _group_workers[scope] = task
        def forget(completed):
            if _group_workers.get(scope) is completed:
                _group_workers.pop(scope, None)
        task.add_done_callback(forget)
    return True


async def _group_reload_worker(upstream: dict, *, scope: str | None = None,
                               api_key: str | None = None) -> None:
    base = str(upstream.get('base_url') or '').strip()
    api_key = upstreamsvc.forward_api_key(upstream) if api_key is None else api_key
    scope = scope or _group_identity(upstream, api_key)
    pending = _group_pending.get(scope) or set()
    if not pending:
        return
    async def fetch():
        return await wb2api.get_status(base_url=base, api_key=api_key)
    try:
        await _observe_accounts(pending, fetch, HOT_RELOAD_WAIT_SECONDS,
                                scope=scope, upstream_id=upstream.get('id'))
    finally:
        if _group_pending.get(scope) is pending:
            _group_pending.pop(scope, None)

