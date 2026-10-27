"""每日随机签到调度器（2026-10-23 从 /opt/signin-random/runner.py 迁入）。

旧实现是独立 systemd oneshot + 09:00 timer，随机等到当天某时刻后打
qoder-bff:8791 / trae-bff:8792 的 /api/checkin，因而必须常年保留那两个容器。
新实现跑在 qtg 进程内，直接调用自己的适配器（qoder2 -> 127.0.0.1:8799，
trae2 -> 127.0.0.1:8080 的 aiwork），不再依赖 bff 容器；签到的也是真正在
跑流量的账号池（旧路径签的是 qoder-hub 面板池，两者不是同一批）。

规则（Asia/Shanghai）：
  trae   09:05-20:55   每天随机一个时刻
  qoder  10:05-20:55
  · 到点执行一次；失败 5 分钟后重试，每平台最多 6 次
  · 21:00 后仍未完成则立即补跑（上游幂等，已签会返回「今日已签到」）
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
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("qtg.checkin")

TZ = dt.timezone(dt.timedelta(hours=8))

WINDOWS = {"trae": ((9, 5), (20, 55)), "qoder": ((10, 5), (20, 55))}
FALLBACK_HOUR = 21
RETRY_AFTER = 300
MAX_ATTEMPTS = 6
TICK = 60


def _load(state_path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) else {}
    except Exception:
        return {}


def _save(state: Dict[str, Any], state_path: Path) -> bool:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, state_path)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("checkin state 写入失败: %s", exc)
        return False


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
        st = _fresh_state()
        if not _save(st,state_path):
            raise RuntimeError('checkin state could not be persisted')
        for name, ts in st["targets"].items():
            when = dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S")
            log.info("今日 %s 随机签到目标 %s", name, when)
    return st


async def _run_one(rt: Any, name: str) -> Dict[str, Any]:
    adapter = getattr(rt, name, None)
    if adapter is None:
        return {"ok": False, "error": "adapter 未启用"}
    return await adapter.checkin()


async def _loop(rt: Any) -> None:
    directory = Path(rt.settings.data_dir)
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
                if name in st["done"]:
                    continue
                due = now_ts >= int(target) or now.hour >= FALLBACK_HOUR
                if not due:
                    continue
                attempts = st["attempts"].get(name, 0)
                if attempts >= MAX_ATTEMPTS:
                    continue
                last = st["last"].get(name, 0)
                if last and now_ts - last < RETRY_AFTER:
                    continue
                st["attempts"][name] = attempts + 1
                st["last"][name] = now_ts
                changed = True
                if not _save(st,state_path):
                    continue  # no durable attempt claim: do not contact upstream
                try:
                    result = await _run_one(rt, name)
                    ok = bool(result.get("ok", True))
                    log.info("签到 %s 第 %d 次 -> ok=%s %s", name, attempts + 1, ok,
                             str(result)[:200])
                    if ok:
                        st["done"][name] = now.strftime("%Y-%m-%d %H:%M:%S")
                        st["result_" + name] = result
                        changed = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("签到 %s 失败（第 %d 次）: %s", name, attempts + 1, exc)
            if changed:
                _save(st,state_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin 主循环异常: %s", exc)


def start(rt: Any) -> "asyncio.Task[None] | None":
    """在 lifespan 里调用，返回可取消的 Task。"""
    if not rt.settings.checkin_scheduler_enabled:
        log.info('每日随机签到调度已禁用')
        return None
    log.info("每日随机签到调度已启动（trae 09:05-20:55 / qoder 10:05-20:55）")
    return asyncio.create_task(_loop(rt), name="qtg-checkin-scheduler")


__all__ = ["start"]
