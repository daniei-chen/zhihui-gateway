"""One bounded upload policy for admission buffering and protocol JSON readers."""
from __future__ import annotations

import asyncio
import time

from . import config


class BodyReadTimeout(TimeoutError):
    def __init__(self, reason, total, idle):
        self.reason, self.total, self.idle = reason, total, idle
        super().__init__('request_body_' + reason + '_timeout')

    @property
    def message(self):
        if self.reason == 'idle':
            return f'请求体上传连续 {self.idle} 秒未收到数据，请检查上传链路后重试'
        return f'请求体上传累计超过 {self.total} 秒，请精简上下文或检查上传链路后重试'


class BodyReadWindow:
    def __init__(self, *, clock=None):
        self.clock = clock or time.monotonic
        self.total = config.GATEWAY_BODY_READ_TIMEOUT_S
        self.idle = config.GATEWAY_BODY_IDLE_TIMEOUT_S
        self.started = self.progress = self.clock()

    async def read(self, operation):
        total_left = self.started + self.total - self.clock()
        idle_left = self.progress + self.idle - self.clock()
        reason = 'total' if total_left <= idle_left else 'idle'
        timeout = min(total_left, idle_left)
        if timeout <= 0:
            raise BodyReadTimeout(reason, self.total, self.idle)
        try:
            value = await asyncio.wait_for(operation(), timeout)
        except TimeoutError:
            raise BodyReadTimeout(reason, self.total, self.idle) from None
        # Empty ASGI frames are not upload progress and cannot renew the lease.
        now = self.clock()
        if now >= self.started + self.total:
            raise BodyReadTimeout('total', self.total, self.idle)
        if now >= self.progress + self.idle:
            raise BodyReadTimeout('idle', self.total, self.idle)
        data = value.get('body', b'') if isinstance(value, dict) else value
        if data:
            self.progress = now
        return value
