"""Single-event-loop capacity queue: bounded key lanes and round-robin grants.

The caller's synchronous reserve callback owns its reservation immediately, even
if cancellation wins after a grant. Its outer finally must release and notify.
No request body or upstream account is acquired while a ticket is waiting.
"""
import asyncio
from collections import deque


class FairGate:
    def __init__(self, maximum=64, per_key=4):
        self.maximum = maximum
        self.per_key = per_key
        self.lanes = {}
        self.order = deque()
        self.pending = 0
        self.timeouts = 0
        self.overflows = 0

    def _remove(self, key, ticket):
        lane = self.lanes.get(key)
        if lane is None or ticket not in lane:
            return
        lane.remove(ticket)
        self.pending -= 1
        if not lane:
            del self.lanes[key]
            self.order.remove(key)

    def notify(self):
        while self.order:
            progress = False
            for key in tuple(self.order):
                lane = self.lanes[key]
                future, reserve = lane[0]
                if future.cancelled():
                    lane.popleft()
                    self.pending -= 1
                    progress = True
                else:
                    try:
                        granted = reserve()
                    except Exception as exc:
                        lane.popleft()
                        self.pending -= 1
                        future.set_exception(exc)
                        progress = True
                    else:
                        if granted:
                            lane.popleft()
                            self.pending -= 1
                            future.set_result(True)
                            progress = True
                if progress:
                    self.order.remove(key)
                    if lane:
                        self.order.append(key)
                    else:
                        del self.lanes[key]
                    break
            if not progress:
                break

    async def acquire(self, key, reserve, timeout=1):
        if not self.pending and reserve():
            return True
        if timeout <= 0:
            return False
        if self.pending >= self.maximum or len(self.lanes.get(key, ())) >= self.per_key:
            self.overflows += 1
            return False
        future = asyncio.get_running_loop().create_future()
        ticket = (future, reserve)
        if key not in self.lanes:
            self.lanes[key] = deque()
            self.order.append(key)
        self.lanes[key].append(ticket)
        self.pending += 1
        self.notify()
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.timeouts += 1
            return False
        finally:
            self._remove(key, ticket)
            self.notify()


def wait_seconds(raw):
    try:
        value = int(raw)
    except (ValueError, TypeError):
        value = 1000
    return value / 1000 if 0 <= value <= 5000 else 1.0
