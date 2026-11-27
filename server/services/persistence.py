"""Bounded, cancellation-owned offloading for request ledger writes.

Admission owns the request while it waits for a slot. Waiting is asynchronous;
the executor itself never accepts an unbounded backlog. Once facts are known,
cancelling the HTTP task must wait for their transaction/spool attempt before
the request's budget, key and retained-byte leases can be released.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
import functools
import threading

import anyio

from .asyncwork import joined_task


class PersistencePool:
    def __init__(self, workers: int = 4, pending: int = 8):
        if workers < 1 or pending < 0:
            raise ValueError('invalid persistence capacity')
        self.capacity = workers + pending
        self._slots = threading.BoundedSemaphore(self.capacity)
        self._lock = threading.Lock()
        self._accepted = 0
        self._owners = 0
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix='wb-persistence')

    @property
    def accepted(self) -> int:
        with self._lock:
            return self._accepted

    def submit(self, function):
        """Return None on saturation; never block the event loop on a semaphore."""
        if not self._slots.acquire(blocking=False):
            return None
        with self._lock:
            if self._closed:
                self._slots.release()
                raise RuntimeError('persistence pool is closed')
            self._accepted += 1
        try:
            future = self._executor.submit(function)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(lambda _: self._release())
        return future

    def _release(self):
        with self._lock:
            self._accepted -= 1
        self._slots.release()

    async def run(self, function, /, *args, **kwargs):
        from .. import drain
        context = contextvars.copy_context()
        call = functools.partial(context.run, function, *args, **kwargs)

        async def owned():
            cancelled = None
            while True:
                future = self.submit(call)
                if future is not None:
                    result = await joined_task(asyncio.wrap_future(future))
                    if cancelled is not None:
                        raise cancelled
                    return result
                # The admitted request retains its existing leases and facts;
                # no executor queue entry or additional body copy is created.
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError as error:
                    cancelled = error

        # AnyIO level cancellation must not repeatedly interrupt the join loop.
        # Direct asyncio cancellation is still remembered by joined_task and
        # re-raised after the accepted transaction has finished.
        with self._lock:
            if self._closed:
                raise RuntimeError('persistence pool is closed')
            self._owners += 1
        try:
            with drain.track('persistence'), anyio.CancelScope(shield=True):
                return await joined_task(asyncio.create_task(owned()))
        finally:
            with self._lock:
                self._owners -= 1

    def snapshot(self):
        with self._lock:
            return {'capacity': self.capacity, 'submitted': self._accepted,
                    'owned_calls': self._owners,
                    'waiting_for_slot': max(0, self._owners - self._accepted)}

    def close(self, *, wait: bool = True):
        with self._lock:
            if self._owners:
                raise RuntimeError('persistence pool still has owned writes')
            self._closed = True
        # Accepted writes are never cancelled: callers own them until completion.
        self._executor.shutdown(wait=wait, cancel_futures=False)


_pool = None
_pool_lock = threading.Lock()


def pool() -> PersistencePool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = PersistencePool()
        return _pool


async def run(function, /, *args, **kwargs):
    return await pool().run(function, *args, **kwargs)


def snapshot():
    with _pool_lock:
        current = _pool
    return current.snapshot() if current is not None else {
        'capacity': 12, 'submitted': 0, 'owned_calls': 0, 'waiting_for_slot': 0}
