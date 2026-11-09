"""Compatibility entry point for the authoritative config HTTP factory.

No separate pool, capacity counter, cookies or lifecycle exists here.
"""
from . import config

entries = config._CLIENTS


def client(timeout, proxy, identity):
    # The former helper used None to mean direct, unlike the main factory.
    return config.http_client(timeout, proxy=proxy or '', identity=identity)


async def close():
    await config.close_clients()
