"""Prepare JSON uploads; callers send returned bytes and headers exactly once.

No credentials, network, provider retries, or file reads occur in this module.
Third-party applications must integrate this helper or equivalent SDK behavior.
"""
from __future__ import annotations

import gzip
import json

THRESHOLD_BYTES = 256 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024


def encode_upload(payload):
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                     separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError('JSON upload exceeds 128 MiB before compression')
    headers = {'Content-Type': 'application/json'}
    if len(raw) >= THRESHOLD_BYTES:
        compressed = gzip.compress(raw, compresslevel=1, mtime=0)
        if len(compressed) * 10 <= len(raw) * 9:
            headers['Content-Encoding'] = 'gzip'
            return compressed, headers
    return raw, headers
