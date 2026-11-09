"""Bounded canonical JSON bytes shared by all inference protocol adapters."""
from __future__ import annotations

import json


class TooLarge(ValueError):
    pass


class Invalid(ValueError):
    pass


def encode(payload: dict, limit: int, *, reserve=None) -> bytes:
    if type(limit) is not int or limit <= 0:
        raise ValueError('positive wire-body limit required')
    output = bytearray()
    pending = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    def flush():
        if pending:
            if reserve is not None:
                reserve(len(output) + len(pending))
            output.extend(pending)
            pending.clear()
    try:
        for part in encoder.iterencode(payload):
            if len(part) > limit - len(output) - len(pending):
                raise TooLarge('serialized inference body exceeds its limit')
            for start in range(0, len(part), 65536):
                raw = part[start:start + 65536].encode('utf-8')
                if len(output) + len(pending) + len(raw) > limit:
                    raise TooLarge('serialized inference body exceeds its limit')
                offset = 0
                while offset < len(raw):
                    take = min(65536 - len(pending), len(raw) - offset)
                    pending.extend(raw[offset:offset + take])
                    offset += take
                    if len(pending) == 65536:
                        flush()
        flush()
    except TooLarge:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError):
        # Never include a payload fragment or an encoder's exception text.
        raise Invalid('inference body cannot be serialized as JSON') from None
    return bytes(output)
