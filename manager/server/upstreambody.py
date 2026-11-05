"""Bound upstream bodies before HTTPX buffers or decodes them.

The byte budget is cumulative for a response, including long SSE streams.  Wire
bytes and every decoded layer are bounded separately: checking Content-Length
or a decoded chunk after HTTPX has expanded it does not bound a compression bomb.
Clients request identity encoding; gzip and deflate remain supported for servers
which compress anyway.  Other unexpected encodings fail closed.
"""
from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator, Iterator

import httpx


CHUNK_BYTES = 64 * 1024


class ResponseTooLarge(httpx.DecodingError):
    """The upstream response exceeded the configured wire/decoded byte budget."""


class _Decoder:
    def __init__(self, encoding: str, maximum: int):
        self.encoding = encoding
        self.maximum = maximum
        self.total = 0
        self.prefix = bytearray()
        self.decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS)
                        if encoding == 'gzip' else None)

    def feed(self, data: bytes) -> Iterator[bytes]:
        if self.decoder is None:
            # Raw deflate is widely used despite the HTTP zlib wrapper contract.
            # Inspect the two-byte wrapper without buffering an arbitrary chunk.
            missing = max(0, 2 - len(self.prefix))
            self.prefix.extend(data[:missing])
            data = data[missing:]
            if len(self.prefix) < 2:
                return
            cmf, flg = self.prefix
            wrapped = (cmf & 15) == 8 and ((cmf << 8) | flg) % 31 == 0
            self.decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            data = bytes(self.prefix) + data
            self.prefix.clear()
        pending = data
        while pending:
            if self.decoder.eof:
                raise httpx.DecodingError('上游压缩响应包含多余尾部数据')
            try:
                piece = self.decoder.decompress(
                    pending, min(CHUNK_BYTES, self.maximum - self.total + 1))
            except zlib.error as exc:
                raise httpx.DecodingError('上游响应压缩格式无效') from exc
            self.total += len(piece)
            if self.total > self.maximum:
                raise ResponseTooLarge('上游解压后响应超过字节预算')
            pending = self.decoder.unconsumed_tail
            if self.decoder.unused_data:
                raise httpx.DecodingError('上游压缩响应包含多余尾部数据')
            if piece:
                yield piece

    def finish(self) -> None:
        if self.decoder is None or not self.decoder.eof:
            raise httpx.DecodingError('上游压缩响应提前结束')


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, encodings: list[str],
                 maximum: int, declared: int | None):
        self.stream = stream
        self.encodings = encodings
        self.maximum = maximum
        self.declared = declared
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            if self.declared is not None and self.declared > self.maximum:
                raise ResponseTooLarge('上游响应 Content-Length 超过字节预算')
            if len(self.encodings) > 4 or any(
                    value not in ('gzip', 'deflate') for value in self.encodings):
                raise httpx.DecodingError('上游返回了未支持的响应压缩格式')
            decoders = [_Decoder(value, self.maximum) for value in reversed(self.encodings)]
            total = 0
            pieces = 0
            frames = 0

            def decode(data: bytes, layer: int = 0) -> Iterator[bytes]:
                if layer == len(decoders):
                    yield data
                    return
                for piece in decoders[layer].feed(data):
                    yield from decode(piece, layer + 1)

            async for raw in self.stream:
                frames += 1
                if frames % 32 == 0:
                    # Empty raw frames and compressed frames without decoded
                    # output must also let cancellation/deadline tasks run.
                    await asyncio.sleep(0)
                total += len(raw)
                if total > self.maximum:
                    raise ResponseTooLarge('上游响应传输字节超过预算')
                # Bound each decoder invocation and preserve downstream backpressure.
                for offset in range(0, len(raw), CHUNK_BYTES):
                    for chunk in decode(raw[offset:offset + CHUNK_BYTES]):
                        yield chunk
                        pieces += 1
                        if pieces % 32 == 0:
                            await asyncio.sleep(0)
            for decoder in decoders:
                decoder.finish()
        except BaseException:
            # AsyncClient closes successful bodies itself; on a decoding failure
            # it must also release the response's shared transport reference.
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self.closed:
            await self.stream.aclose()
            self.closed = True


class BoundedTransport(httpx.AsyncBaseTransport):
    """Keep HTTPX's public client interface while bounding its body reader."""
    def __init__(self, transport: httpx.AsyncBaseTransport, maximum: int):
        self.transport = transport
        self.maximum = max(1, int(maximum))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.transport.handle_async_request(request)
        encodings = [item.strip().lower() for item in
                     response.headers.get('content-encoding', '').split(',')
                     if item.strip() and item.strip().lower() != 'identity']
        declared = None
        try:
            declared = int(response.headers.get('content-length', ''))
        except (ValueError, TypeError):
            pass
        if request.method == 'HEAD' or response.status_code in (204, 304):
            encodings, declared = [], None
        response.stream = _BoundedStream(response.stream, encodings, self.maximum, declared)
        # The wrapped stream now yields decoded bytes. Leaving these headers
        # would ask HTTPX to decompress a second time or report a stale length.
        if encodings:
            del response.headers['content-encoding']
            response.headers.pop('content-length', None)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()
