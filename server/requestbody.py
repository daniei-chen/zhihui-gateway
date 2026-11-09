"""One bounded upload policy for admission buffering and protocol JSON readers."""
from __future__ import annotations

import asyncio
import time
import zlib

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


class BodyEncodingError(ValueError):
    pass


class BodySizeExceeded(ValueError):
    pass


class BodyDisconnected(Exception):
    pass


def content_encoding(headers):
    values = [value.decode('ascii', errors='replace').strip().lower()
              for name, value in headers if name.lower() == b'content-encoding']
    if not values:
        return 'identity'
    if len(values) != 1 or values[0] not in ('identity', 'gzip'):
        raise BodyEncodingError('unsupported_content_encoding')
    return values[0]


async def body_frames(receive, encoding, limit, *, window=None, reserve=None):
    """Bound wire and decoded bytes; yield before retaining another 64 KiB."""
    window = window or BodyReadWindow()
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
    wire = decoded = 0
    # Coalesce tiny frames without retaining unbounded ASGI dict/list overhead.
    pending = bytearray()
    def retain(raw):
        for start in range(0, len(raw), 65536):
            part = raw[start:start + 65536]
            room = 65536 - len(pending)
            pending.extend(part[:room])
            if len(pending) == 65536:
                yield {'type': 'http.request', 'body': bytes(pending), 'more_body': True}
                pending.clear()
            pending.extend(part[room:])
    while True:
        message = await window.read(receive)
        if message['type'] == 'http.disconnect':
            raise BodyDisconnected()
        raw = message.get('body', b'')
        wire += len(raw)
        if wire > limit:
            raise BodySizeExceeded()
        if reserve is not None:
            reserve(wire + decoded if decoder else wire)
        if decoder is None:
            decoded = wire
            for frame in retain(raw):
                yield frame
        else:
            remaining = raw
            while remaining:
                try:
                    chunk = decoder.decompress(remaining, min(65536, limit - decoded + 1))
                except zlib.error:
                    raise BodyEncodingError('invalid_gzip_body') from None
                if decoder.unused_data:
                    raise BodyEncodingError('invalid_gzip_body')
                remaining = decoder.unconsumed_tail
                decoded += len(chunk)
                if decoded > limit:
                    raise BodySizeExceeded()
                if reserve is not None:
                    reserve(wire + decoded)
                if chunk:
                    for frame in retain(chunk):
                        yield frame
                    await asyncio.sleep(0)
                    window.check_total()
            if not message.get('more_body', False):
                if not decoder.eof:
                    raise BodyEncodingError('invalid_gzip_body')
        if not message.get('more_body', False):
            yield {'type': 'http.request', 'body': bytes(pending), 'more_body': False}
            return


class BodyReadWindow:
    def __init__(self, *, clock=None):
        self.clock = clock or time.monotonic
        self.total = config.GATEWAY_BODY_READ_TIMEOUT_S
        self.idle = config.GATEWAY_BODY_IDLE_TIMEOUT_S
        self.started = self.progress = self.clock()

    def check_total(self):
        if self.clock() >= self.started + self.total:
            raise BodyReadTimeout('total', self.total, self.idle)

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
