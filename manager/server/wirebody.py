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
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    try:
        for part in encoder.iterencode(payload):
            if len(part) > limit - len(output):
                raise TooLarge('serialized inference body exceeds its limit')
            for start in range(0, len(part), 65536):
                raw = part[start:start + 65536].encode('utf-8')
                size = len(output) + len(raw)
                if size > limit:
                    raise TooLarge('serialized inference body exceeds its limit')
                if reserve is not None:
                    reserve(size)
                output.extend(raw)
    except TooLarge:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError):
        # Never include a payload fragment or an encoder's exception text.
        raise Invalid('inference body cannot be serialized as JSON') from None
    return bytes(output)
