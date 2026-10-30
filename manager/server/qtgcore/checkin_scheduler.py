"""每日随机签到调度器（2026-10-23 从 /opt/signin-random/runner.py 迁入）。

旧实现是独立 systemd oneshot + 09:00 timer，随机等到当天某时刻后打
qoder-bff:8791 / trae-bff:8792 的 /api/checkin，因而必须常年保留那两个容器。
新实现跑在 qtg 进程内，直接调用自己的适配器（qoder2 -> 127.0.0.1:8799，
trae2 -> 127.0.0.1:8080 的 aiwork），不再依赖 bff 容器；签到的也是真正在
跑流量的账号池（旧路径签的是 qoder-hub 面板池，两者不是同一批）。

规则（Asia/Shanghai）：
  trae   09:05-20:55   每天随机一个时刻
  qoder  10:05-20:55
  · 到点执行一次；派发前竞争/失败 5 分钟后重试，每平台最多 6 次
  · 不确定/部分完成/已提交未完成的结果保留证据，跨日也不自动重发，等待核对
  · 21:00 后尚未派发的任务补跑；不重放失败或结果不确定的批次
  · qoder2 自带排程，由引擎负责；禁用标志不证明旧任务已停，不自动接管
  · 状态落 /opt/workbuddy-manager/engine/var/random_checkin.json，按自然日隔离
  · 受 /opt/workbuddy-manager/engine/var/compat-plans.json 的 daily.checkin.enabled 控制
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import random
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException

from .checkin_result import confirmed_summary, summarize_result, validate_summary

log = logging.getLogger("qtg.checkin")

TZ = dt.timezone(dt.timedelta(hours=8))

WINDOWS = {"trae": ((9, 5), (20, 55)), "qoder": ((10, 5), (20, 55))}
FALLBACK_HOUR = 21
RETRY_AFTER = 300
MAX_ATTEMPTS = 6
TICK = 60


class DispatchNotStarted(HTTPException):
    """Only raised before this attempt writes its durable dispatch marker."""

    def __init__(self, status_code: int, detail: str, *, confirmed: dict | None = None):
        super().__init__(status_code=status_code, detail=detail)
        self.confirmed = confirmed


def _load(state_path: Path) -> Dict[str, Any]:
    try:
        if state_path.is_symlink() or state_path.stat().st_size > 262144:
            raise ValueError('unsafe checkin state')
        value = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError('invalid checkin state')
        if dt.date.fromisoformat(value['day']) > dt.date.fromisoformat(_today()):
            raise ValueError('checkin state is from the future')
        for field in ('targets', 'done', 'attempts', 'last', 'uncertain'):
            mapping = value.get(field, {})
            if not isinstance(mapping, dict) or any(name not in WINDOWS for name in mapping):
                raise ValueError('invalid checkin state field')
            for item in mapping.values():
                if field in ('targets', 'attempts', 'last'):
                    if type(item) is not int or item < 0:
                        raise ValueError('invalid checkin state counter')
                elif not isinstance(item, str) or len(item) > 64:
                    raise ValueError('invalid checkin state marker')
        if any(field not in value for field in ('targets', 'done', 'attempts', 'last')):
            raise ValueError('incomplete checkin state')
        if not value['targets']:
            raise ValueError('missing checkin targets')
        references = value.get('dispatch_attempts', {})
        if (not isinstance(references, dict) or any(name not in WINDOWS for name in references)
                or any(not isinstance(item, str) or re.fullmatch('[0-9a-f]{32}', item) is None
                       for item in references.values())):
            raise ValueError('invalid checkin attempt reference')
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError):
        # Lost/corrupt evidence is not permission to send another provider action.
        raise RuntimeError('checkin state unreadable; reconciliation required') from None


def _save(state: Dict[str, Any], state_path: Path) -> bool:
    tmp = None
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if state_path.is_symlink():
            raise ValueError('unsafe checkin state')
        fd, name = tempfile.mkstemp(prefix='.checkin-', suffix='.tmp', dir=state_path.parent)
        tmp = Path(name)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, state_path)
        if os.name != 'nt':
            directory = os.open(state_path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("checkin state 写入失败: %s", type(exc).__name__)
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _scheduler_owner(directory: Path, lock_name: str = '.random-checkin.lock'):
    """A lifetime, nonblocking OS lock prevents duplicate worker schedulers."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / lock_name
    if path.is_symlink():
        raise RuntimeError('unsafe checkin lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+b') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('unsafe checkin lock')
        if info.st_size == 0:
            handle.write(b'1')
            handle.flush()
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _plans_enabled(plans_path: Path) -> bool:
    try:
        plans = json.loads(plans_path.read_text(encoding="utf-8"))
        enabled = plans.get("daily", {}).get("checkin", {}).get("enabled", True)
        return enabled if isinstance(enabled,bool) else False
    except FileNotFoundError:
        # Preserve the existing default only for an absent plan; an unreadable
        # or corrupt plan must not accidentally enable upstream operations.
        return True
    except (OSError,ValueError,AttributeError,TypeError):
        return False


def _today() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def _fresh_state() -> Dict[str, Any]:
    day = _today()
    st: Dict[str, Any] = {"day": day, "targets": {}, "done": {}, "attempts": {}, "last": {}}
    for name, ((sh, sm), (eh, em)) in WINDOWS.items():
        start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(sh, sm), TZ)
        end = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(eh, em), TZ)
        span = int((end - start).total_seconds())
        st["targets"][name] = int(start.timestamp()) + random.randint(0, max(span, 1))
    return st


def _ensure_state(state_path: Path) -> Dict[str, Any]:
    st = _load(state_path)
    if st.get("day") != _today():
        previous = st
        st = _fresh_state()
        # Midnight does not establish that yesterday's pending operation stopped.
        st['uncertain'] = dict(previous.get('uncertain', {}))
        st['dispatch_attempts'] = {name: reference
                                  for name, reference in previous.get('dispatch_attempts', {}).items()
                                  if name in st['uncertain']}
        if not _save(st,state_path):
            raise RuntimeError('checkin state could not be persisted')
        for name, ts in st["targets"].items():
            when = dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S")
            log.info("今日 %s 随机签到目标 %s", name, when)
    return st


async def _run_one(rt: Any, name: str, attempt_id: str) -> Dict[str, Any]:
    adapter = getattr(rt, name, None)
    if adapter is None:
        return {"ok": False, "error": "adapter 未启用"}
    return await dispatch_checkin(rt, name, source='scheduler', attempt_id=attempt_id)


async def automatic_owner(rt: Any, name: str) -> str:
    """No takeover from an independently scheduled engine without idle proof."""
    adapter = getattr(rt, name, None)
    observe = getattr(adapter, 'automatic_checkin_owner', None)
    if observe is None:
        return 'manager'
    try:
        value = await observe()
        return value if value in ('engine', 'unverified') else 'unverified'
    except Exception:  # Ownership failure must not cause a provider action.
        return 'unverified'


def _dispatch_time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid dispatch time')
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None or stamp > dt.datetime.now(TZ):
        raise ValueError('invalid dispatch time')
    return stamp


def _read_dispatch(path: Path, name: str) -> Dict[str, Any]:
    if path.is_symlink():
        raise ValueError('unsafe dispatch evidence')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise ValueError('unsafe dispatch evidence')
        raw = handle.read(65537)
    if len(raw) > 65536 or path.is_symlink():
        raise ValueError('unsafe dispatch evidence')
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('platform') != name or value.get('source') not in ('manual', 'scheduler')
            or value.get('scope') not in ('single', 'all')
            or not isinstance(value.get('attempt_id'), str)
            or re.fullmatch('[0-9a-f]{32}', value['attempt_id']) is None
            or value.get('phase') not in ('started', 'uncertain', 'complete')):
        raise ValueError('invalid dispatch evidence')
    started = _dispatch_time(value.get('started_at'))
    if value['phase'] in ('complete', 'uncertain'):
        validate_summary(value.get('summary'))
        if value['phase'] == 'complete' and not confirmed_summary(value['summary']):
            raise ValueError('unconfirmed dispatch evidence')
    if 'completed_at' in value and _dispatch_time(value['completed_at']) < started:
        raise ValueError('dispatch completion precedes its start')
    return value


def _recover_completed(directory: Path, state: Dict[str, Any], name: str) -> bool:
    """Repair only the scheduler's exact attempt with a durable terminal response."""
    reference = state.get('dispatch_attempts', {}).get(name)
    if name not in state.get('uncertain', {}) or reference is None:
        return False
    with _scheduler_owner(directory, '.checkin-dispatch-' + name + '.lock') as owned:
        if not owned:
            return False
        try:
            value = _read_dispatch(directory / ('checkin-dispatch-' + name + '.json'), name)
            if (value['phase'] != 'complete' or value['source'] != 'scheduler'
                    or value['scope'] != 'all' or value['attempt_id'] != reference
                    or 'completed_at' not in value):
                return False
            completed = _dispatch_time(value['completed_at']).astimezone(TZ).date().isoformat()
        except (OSError, ValueError, TypeError, OverflowError):
            return False
        state['result_' + name] = value['summary']
        state['uncertain'].pop(name, None)
        state['dispatch_attempts'].pop(name, None)
        if value['summary']['ok'] and completed == state['day']:
            state['done'][name] = value['completed_at']
        return True


async def dispatch_checkin(rt: Any, name: str, *, uid: str | None = None,
                           source: str = 'manual', attempt_id: str | None = None) -> Dict[str, Any]:
    """Fence manual, plan and scheduler writes through the same durable owner."""
    if name not in WINDOWS or source not in ('manual', 'scheduler'):
        raise ValueError('unsupported checkin dispatch')
    if uid is not None and (not isinstance(uid, str) or not uid or len(uid) > 256):
        raise ValueError('invalid account uid')
    if attempt_id is not None and (source != 'scheduler' or not isinstance(attempt_id, str)
                                  or re.fullmatch('[0-9a-f]{32}', attempt_id) is None):
        raise ValueError('invalid scheduler attempt')
    attempt_id = attempt_id or uuid.uuid4().hex
    directory = Path(rt.settings.data_dir)
    path = directory / ('checkin-dispatch-' + name + '.json')
    with _scheduler_owner(directory, '.checkin-dispatch-' + name + '.lock') as owned:
        if not owned:
            raise DispatchNotStarted(409, '该平台正在处理签到；未重复派发')
        if source == 'scheduler' and await automatic_owner(rt, name) != 'manager':
            raise DispatchNotStarted(409, '自动签到归属引擎或尚未确认；智汇未重复派发')
        if source == 'manual':
            try:
                scheduled = _load(directory / 'random_checkin.json')
            except RuntimeError:
                raise DispatchNotStarted(409, '调度证据不可读；未派发，需先核对') from None
            if name in scheduled.get('uncertain', {}):
                raise DispatchNotStarted(409, '该平台存在待核对的签到；未重复派发')
        try:
            previous = _read_dispatch(path, name)
            if previous['phase'] != 'complete':
                raise ValueError('unconfirmed dispatch evidence')
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, OverflowError):
            raise DispatchNotStarted(409, '该平台上次签到未确认；未重复派发，请核对记录') from None
        else:
            # A completed manual pool check also satisfies today's automatic run.
            # A single-account result cannot stand in for the whole pool.
            if (source == 'scheduler' and previous['scope'] == 'all' and previous['summary']['ok']
                    and 'completed_at' in previous
                    and _dispatch_time(previous['completed_at']).astimezone(TZ).date().isoformat() == _today()):
                raise DispatchNotStarted(409, '今日整池签到已有完整结果；未自动重复派发',
                                         confirmed=previous['summary'])
        record = {'version': 1, 'platform': name, 'phase': 'started', 'source': source,
                  'scope': 'single' if uid is not None else 'all', 'attempt_id': attempt_id,
                  'started_at': dt.datetime.now(TZ).isoformat()}
        if not _save(record, path):
            raise DispatchNotStarted(503, '无法保存签到派发证据；未联系上游')
        adapter = getattr(rt, name)
        # Cancellation or a lost response leaves the durable started marker intact.
        result = await (adapter.checkin_one(uid) if uid is not None else adapter.checkin())
        summary = summarize_result(result)
        record['phase'] = 'complete' if confirmed_summary(summary) else 'uncertain'
        record['summary'] = summary
        record['completed_at'] = dt.datetime.now(TZ).isoformat()
        if not _save(record, path):
            raise HTTPException(status_code=503, detail='签到已派发但结果证据未保存；请核对，勿重复提交')
        return result


def dispatch_state(directory: Path, name: str) -> str:
    """Read-only presentation of dispatch evidence; never unlock or create state."""
    if name not in WINDOWS:
        return 'unknown'
    path = directory / ('checkin-dispatch-' + name + '.json')
    try:
        value = _read_dispatch(path, name)
        return 'confirmed' if value.get('phase') == 'complete' else 'uncertain'
    except FileNotFoundError:
        return 'absent'
    except (OSError, ValueError, TypeError, OverflowError):
        return 'unknown'


async def _loop(rt: Any) -> None:
    directory = Path(rt.settings.data_dir)
    with _scheduler_owner(directory) as owned:
        if not owned:
            log.info('另一进程持有每日签到调度锁，本进程不重复派发')
            return
        await _owned_loop(rt, directory)


async def _owned_loop(rt: Any, directory: Path) -> None:
    state_path, plans_path = directory/'random_checkin.json', directory/'compat-plans.json'
    while True:
        try:
            await asyncio.sleep(TICK)
            if not _plans_enabled(plans_path):
                continue
            st = _ensure_state(state_path)
            now = dt.datetime.now(TZ)
            now_ts = int(now.timestamp())
            changed = False
            for name, target in list(st["targets"].items()):
                if _recover_completed(directory, st, name):
                    if not _save(st, state_path):
                        raise RuntimeError('recovered checkin evidence could not be persisted')
                if name in st["done"] or name in st.get("uncertain", {}):
                    continue
                due = now_ts >= int(target) or now.hour >= FALLBACK_HOUR
                if not due:
                    continue
                if await automatic_owner(rt, name) != 'manager':
                    # Do not mark this as done: ownership is not a reward receipt.
                    continue
                attempts = st["attempts"].get(name, 0)
                if attempts >= MAX_ATTEMPTS:
                    continue
                last = st["last"].get(name, 0)
                if last and now_ts - last < RETRY_AFTER:
                    continue
                st["attempts"][name] = attempts + 1
                st["last"][name] = now_ts
                st.setdefault("uncertain", {})[name] = now.strftime("%Y-%m-%d %H:%M:%S")
                reference = uuid.uuid4().hex
                st.setdefault('dispatch_attempts', {})[name] = reference
                changed = True
                if not _save(st,state_path):
                    continue  # no durable attempt claim: do not contact upstream
                try:
                    result = await _run_one(rt, name, reference)
                    summary = summarize_result(result)
                    ok = summary["ok"]
                    log.info("签到 %s 第 %d 次 -> %s", name, attempts + 1, summary)
                    st["result_" + name] = summary
                    changed = True
                    if confirmed_summary(summary):
                        st["uncertain"].pop(name, None)
                        st['dispatch_attempts'].pop(name, None)
                    if ok:
                        st["done"][name] = now.strftime("%Y-%m-%d %H:%M:%S")
                except DispatchNotStarted as exc:
                    # This claim never reached the provider. An existing engine
                    # fence still blocks new dispatches independently of this row.
                    st['uncertain'].pop(name, None)
                    st['dispatch_attempts'].pop(name, None)
                    if exc.confirmed is not None:
                        st['result_' + name] = exc.confirmed
                        st['done'][name] = now.strftime('%Y-%m-%d %H:%M:%S')
                    changed = True
                except Exception as exc:  # noqa: BLE001
                    # The request may already have reached the provider; do not replay it.
                    st.setdefault("uncertain", {})[name] = now.strftime("%Y-%m-%d %H:%M:%S")
                    st["result_" + name] = {"ok": False, "unknown": 1, "accounts": 0}
                    log.warning("签到 %s 结果未确认（第 %d 次，%s），暂停自动重发，等待核对",
                                name, attempts + 1, type(exc).__name__)
            if changed:
                _save(st,state_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin 主循环异常: %s", type(exc).__name__)


def start(rt: Any) -> "asyncio.Task[None] | None":
    """在 lifespan 里调用，返回可取消的 Task。"""
    if not rt.settings.checkin_scheduler_enabled:
        log.info('每日随机签到调度已禁用')
        return None
    log.info("每日随机签到调度已启动（trae 09:05-20:55 / qoder 10:05-20:55）")
    return asyncio.create_task(_loop(rt), name="qtg-checkin-scheduler")


__all__ = ["start"]
