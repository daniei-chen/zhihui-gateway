"""Public management probes with one validated DNS answer bound to TCP.

HTTP host and TLS SNI remain the original hostname. No environment proxy,
redirect, fallback resolver or private IP is permitted for the REST probe.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpcore
import httpx


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_global and not address.is_multicast
    except ValueError:
        return False


async def resolve_public(host: str, port: int) -> tuple[str, ...]:
    if not host or host.lower().endswith(('.local', '.internal', '.localhost')):
        raise ValueError('不允许探测内部主机')
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM), 3)
    except (OSError, TimeoutError) as exc:
        raise ValueError('无法核实探测地址的 DNS，未发送请求') from exc
    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(public_address(value) for value in addresses):
        raise ValueError('探测地址包含非公网 IP，未发送请求')
    return addresses


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, host: str, port: int, addresses: tuple[str, ...], backend=None):
        if not addresses or not all(public_address(value) for value in addresses):
            raise ValueError('Invalid public address pin')
        self.host, self.port, self.addresses = host, port, addresses
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.host or port != self.port:
            raise httpcore.ConnectError('Probe destination changed')
        # No hostname is handed to the TCP backend, so DNS cannot rebind after
        # validation. A failed address does not trigger an unvalidated fallback.
        return await self.backend.connect_tcp(self.addresses[0], port, timeout=timeout,
            local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError('Unix sockets are not probe destinations')

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, host: str, port: int, addresses: tuple[str, ...]):
        self.pool = httpcore.AsyncConnectionPool(network_backend=PinnedBackend(host, port, addresses),
            max_connections=1, max_keepalive_connections=0, retries=0)

    async def handle_async_request(self, request):
        raw = httpcore.Request(method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        try:
            result = await self.pool.handle_async_request(raw)
        except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException) as exc:
            raise httpx.RequestError('Public probe connection failed', request=request) from exc
        return httpx.Response(result.status, headers=result.headers,
            stream=_ResponseStream(result.stream), extensions=result.extensions)

    async def aclose(self):
        await self.pool.aclose()
