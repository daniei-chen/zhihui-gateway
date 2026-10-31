"""Wait for owned blocking work before a cancelled coroutine releases its lease."""
from __future__ import annotations

import asyncio


async def joined_task(task):
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancelled = error
        except Exception:
            if cancelled is not None:
                raise cancelled from None
            raise
        else:
            if cancelled is not None:
                raise cancelled
            return result


async def joined_thread(function, /, *args, **kwargs):
    return await joined_task(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))
