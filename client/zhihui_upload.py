"""Small opt-in JSON upload helper with bounded, useful gzip compression.

This helper is intentionally separate from the gateway.  It does not change
third-party clients; callers choose whether to use it before sending a request.
"""
from __future__ import annotations

import gzip
import json


MIN_COMPRESS_BYTES = 256 * 1024
MIN_SAVINGS_RATIO = 0.10
MAX_DECODED_BYTES = 128 * 1024 * 1024


def encode_upload(payload: object) -> tuple[bytes, dict[str, str]]:
    """Encode one JSON request and compress only when it saves at least 10%."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError('decoded upload exceeds MAX_DECODED_BYTES')
    headers = {'Content-Type': 'application/json'}
    if len(raw) < MIN_COMPRESS_BYTES:
        return raw, headers
    compressed = gzip.compress(raw, compresslevel=1)
    if len(compressed) <= len(raw) * (1 - MIN_SAVINGS_RATIO):
        headers['Content-Encoding'] = 'gzip'
        return compressed, headers
    return raw, headers
