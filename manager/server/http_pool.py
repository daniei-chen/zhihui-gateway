"""Bounded, loop-owned HTTP transports with per-request response lifetimes.

Clients are cheap, isolated leases. TLS/keepalive transports are reused; a
transport cannot be evicted until every response using it has been closed.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
import logging
import threading
import time
import weakref

import anyio
import httpx

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Entry:
    key: tuple
    loop: asyncio.AbstractEventLoop
    transport: httpx.AsyncBaseTransport | None = None
    references: int = 1
    retired: bool = False
    closed: bool = False
    closing: Future | None = None


class TransportPool:
    def __init__(self, cache: dict):
        self.cache = cache
        self.owned: set[Entry] = set()
        self.lock = threading.RLock()
        self.waiters: set[tuple] = set()
        self.guards: dict = {}
        self.closing_loops = weakref.WeakSet()
        self.close_tasks: set[asyncio.Task] = set()

    def notify(self):
        with self.lock:
            waiters = list(self.waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # A cancelled/closed loop cannot consume capacity.

    def _guard(self, loop):
        with self.lock:
            if loop not in self.guards:
                self.guards[loop] = loop.create_task(self._loop_lifetime(loop))

    async def _loop_lifetime(self, loop):
        try:
            await asyncio.Future()
        finally:
            # asyncio.run/uvicorn closes the loop only after cancelling tasks.
            # This also cleans short-lived synchronous bridge loops.
            with self.lock:
                self.closing_loops.add(loop)
            try:
                await self.close_loop(loop, force=True)
            finally:
                with self.lock:
                    self.guards.pop(loop, None)

    def _close(self, entry: Entry, *, retry=False) -> Future:
        with self.lock:
            if (retry and entry.closing is not None and entry.closing.done()
                    and not entry.closed):
                entry.closing = None
            if entry.closing is not None:
                return entry.closing
            completion = entry.closing = Future()

        async def finish():
            error = None
            try:
                if entry.transport is not None:
                    try:
                        with anyio.move_on_after(5, shield=True) as scope:
                            await entry.transport.aclose()
                        if scope.cancel_called:
                            raise httpx.CloseError('HTTP transport cleanup exceeded its budget')
                    except asyncio.CancelledError:
                        # Runner cancellation must not strand a cleanup future.
                        with anyio.move_on_after(5, shield=True) as scope:
                            await entry.transport.aclose()
                        if scope.cancel_called:
                            raise httpx.CloseError('HTTP transport cleanup exceeded its budget')
            except BaseException as exc:
                error = exc
                log.error('HTTP transport cleanup failed (%s)', type(exc).__name__)
            finally:
                with self.lock:
                    entry.closed = error is None
                    if entry.closed:
                        self.owned.discard(entry)
                if error is None:
                    completion.set_result(None)
                else:
                    completion.set_exception(error)
                self.notify()

        def start():
            task = entry.loop.create_task(finish())
            self.close_tasks.add(task)
            def done(completed):
                self.close_tasks.discard(completed)
                if not completion.done():
                    # A task cancelled before its first instruction has no finally.
                    start()
            task.add_done_callback(done)

        try:
            entry.loop.call_soon_threadsafe(start)
        except RuntimeError as error:
            # Do not silently reuse a slot while its transport is unclosed.
            completion.set_exception(error)
        return completion

    def retire(self, entry: Entry):
        with self.lock:
            entry.retired = True
            if self.cache.get(entry.key) is entry:
                self.cache.pop(entry.key)
            idle = not entry.references
        if idle:
            self._close(entry)

    def release(self, entry: Entry):
        with self.lock:
            if entry.references <= 0:
                raise RuntimeError('HTTP transport reference released twice')
            entry.references -= 1
            close = entry.retired and not entry.references and not entry.closed
        if close:
            self._close(entry)
        self.notify()

    async def acquire(self, logical_key, factory, limit, timeout, cancelled):
        loop = asyncio.get_running_loop()
        key = (loop, logical_key)
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        self._guard(loop)
        while True:
            victim = None
            event = None
            with self.lock:
                if cancelled() or loop in self.closing_loops:
                    raise RuntimeError('HTTP lease or event loop is closed')
                entry = self.cache.get(key)
                if entry is not None and not entry.retired and not entry.closed:
                    entry.references += 1
                    self.cache.pop(key)
                    self.cache[key] = entry
                    return entry
                if len(self.owned) < max(1, limit()):
                    entry = Entry(key, loop)
                    self.owned.add(entry)
                    self.cache[key] = entry
                    break
                victim = next((value for value in self.cache.values()
                               if not value.references and not value.retired), None)
                if victim is not None:
                    victim.retired = True
                    self.cache.pop(victim.key)
                else:
                    event = asyncio.Event()
                    self.waiters.add((loop, event))
            remaining = None if deadline is None else deadline - time.monotonic()
            if victim is not None:
                # Close on the owning loop before allocating the replacement.
                try:
                    await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._close(victim))),
                                           None if remaining is None else max(0, remaining))
                except asyncio.TimeoutError as exc:
                    raise httpx.PoolTimeout('Timed out retiring an idle upstream transport') from exc
                continue
            try:
                if remaining is not None and remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError as exc:
                raise httpx.PoolTimeout('All upstream transport slots are busy') from exc
            finally:
                with self.lock:
                    self.waiters.discard((loop, event))
        try:
            entry.transport = factory()
            return entry
        except BaseException:
            with self.lock:
                self.cache.pop(key, None)
                self.owned.discard(entry)
            self.notify()
            raise

    async def close_loop(self, loop=None, *, force=False):
        loop = loop or asyncio.get_running_loop()
        with self.lock:
            entries = [entry for entry in self.owned if entry.loop is loop]
            for entry in entries:
                entry.retired = True
                if self.cache.get(entry.key) is entry:
                    self.cache.pop(entry.key)
            futures = [self._close(entry, retry=True) for entry in entries if force or not entry.references]
        self.notify()
        if futures:
            await asyncio.gather(*(asyncio.shield(asyncio.wrap_future(future)) for future in futures))


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream, entry, pool):
        self.stream, self.entry, self.pool = stream, entry, pool
        self.closed = False

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        try:
            with anyio.move_on_after(5, shield=True) as scope:
                await self.stream.aclose()
            if scope.cancel_called:
                raise httpx.CloseError('Upstream response close exceeded its budget')
        except BaseException:
            self.pool.retire(self.entry)
            raise
        finally:
            self.pool.release(self.entry)


class LeaseTransport(httpx.AsyncBaseTransport):
    def __init__(self, pool, key, factory, limit):
        self.pool, self.key, self.factory, self.limit = pool, key, factory, limit
        self.closed = False

    async def handle_async_request(self, request):
        entry = await self.pool.acquire(self.key, self.factory, self.limit,
                                        request.extensions.get('timeout', {}).get('pool'),
                                        lambda: self.closed)
        try:
            response = await entry.transport.handle_async_request(request)
            if response.is_stream_consumed:
                # Mock/custom transports can return an already buffered body.
                await response.aclose()
                self.pool.release(entry)
                return response
            response.stream = ResponseStream(response.stream, entry, self.pool)
            return response
        except BaseException:
            self.pool.release(entry)
            raise

    async def aclose(self):
        self.closed = True
        self.pool.notify()
