"""A single writer emits SSE comments while an owned upstream iterator is idle."""
from __future__ import annotations

import asyncio
import contextlib
import math
import os

from .services.asyncwork import joined_task


def interval_seconds():
    try:
        value = int(os.environ.get('WB_SSE_HEARTBEAT_SECONDS', '15'))
    except ValueError:
        return 15.0
    return float(value) if value == 0 or 1 <= value <= 60 else 15.0


async def comments(iterator, *, interval=None):
    interval = interval_seconds() if interval is None else interval
    source = iterator.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(source))
            if interval > 0:
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield b': keepalive\n\n'
                    continue
            else:
                await asyncio.wait({pending})
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield chunk
    finally:
        if pending is not None:
            pending.cancel()
            # Retain ownership until the source has run its cancellation cleanup.
            with contextlib.suppress(asyncio.CancelledError):
                await joined_task(pending)
        close = getattr(source, 'aclose', None)
        if close is not None:
            await close()
