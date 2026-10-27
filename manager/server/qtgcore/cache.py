"""进程内 TTL 缓存 + single-flight（防击穿）。

为什么需要它：`/api/overview` 一次要打 5 个上游接口，而前端多个标签页 /
外部探活会并发请求。没有 single-flight 时 N 个并发请求会放大成 5N 次上游
调用，既慢又容易把上游 429 打出来。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: Optional[T] = None
    expires_at: float = 0.0
    lock: Optional[asyncio.Lock] = None


class TTLCache:
    """按 key 缓存，带 single-flight；线程/协程安全由事件循环串行保证。"""

    def __init__(self) -> None:
        self._entries: Dict[str, _Entry[Any]] = {}
        self._hits = 0
        self._misses = 0
        self._coalesced = 0

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "coalesced": self._coalesced,
            "hit_ratio": round(self._hits / total, 4) if total else 0.0,
        }

    def invalidate(self, prefix: str = "") -> int:
        if not prefix:
            n = len(self._entries)
            self._entries.clear()
            return n
        keys = [k for k in self._entries if k.startswith(prefix)]
        for k in keys:
            self._entries.pop(k, None)
        return len(keys)

    async def get_or_set(
        self,
        key: str,
        ttl: float,
        factory: Callable[[], Awaitable[T]],
        *,
        force: bool = False,
    ) -> T:
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry()
            self._entries[key] = entry
        if not force and entry.value is not None and entry.expires_at > now:
            self._hits += 1
            return entry.value  # type: ignore[return-value]

        if entry.lock is None:
            entry.lock = asyncio.Lock()
        lock = entry.lock
        if lock.locked():
            self._coalesced += 1
        async with lock:
            # 双检：等锁期间可能已被别的协程填好
            now = time.monotonic()
            if not force and entry.value is not None and entry.expires_at > now:
                self._hits += 1
                return entry.value  # type: ignore[return-value]
            self._misses += 1
            value = await factory()
            entry.value = value
            entry.expires_at = time.monotonic() + max(ttl, 0.0)
            return value


cache = TTLCache()
