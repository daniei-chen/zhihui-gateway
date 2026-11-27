"""Wait for owned blocking work before a cancelled coroutine releases its lease."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import anyio


async def joined_task(task):
    cancelled = None
    # AnyIO level cancellation otherwise re-enters shield in a hot loop while
    # the worker is still alive. Direct Task.cancel is remembered and re-raised.
    with anyio.CancelScope(shield=True):
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


def loop_callback(function):
    """Marshal a worker's reservation back to the request's owning event loop."""
    if function is None:
        return None
    loop = asyncio.get_running_loop()

    def callback(*args):
        result = Future()

        def apply():
            try:
                value = function(*args)
            except BaseException as error:
                result.set_exception(error)
            else:
                result.set_result(value)

        loop.call_soon_threadsafe(apply)
        return result.result()

    return callback
