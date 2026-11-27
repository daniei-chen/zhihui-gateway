"""HTTP error envelopes shared by admission, protocol routes and handlers.

An HTTP error is JSON before a stream starts. Once output has started, the
protocol's terminal SSE event owns the failure; this module never retries or
reopens a request. Anthropic's native shape and status types are documented at
https://platform.claude.com/docs/en/api/errors.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from starlette.responses import JSONResponse


def error_type(status: int, protocol: str = 'openai') -> str:
    common = {401: 'authentication_error', 403: 'permission_error',
              429: 'rate_limit_error'}
    if status in common:
        return common[status]
    if protocol == 'anthropic':
        native = {402: 'billing_error', 404: 'not_found_error',
                  409: 'conflict_error', 413: 'request_too_large',
                  504: 'timeout_error', 529: 'overloaded_error'}
        if status in native:
            return native[status]
    return 'api_error' if status >= 500 else 'invalid_request_error'


def retry_after_seconds(headers: object) -> int | None:
    """Accept the existing provider contract: integer seconds in [0, 7200].

    HTTP dates, duplicate/comma-separated values and unbounded values are not
    interpreted. An absent or invalid delay is unknown, not zero.
    """
    try:
        raw = headers.get('retry-after')
        if raw is None:
            raw = headers.get('Retry-After')
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            return None
        value = int(str(raw).strip())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    except Exception:  # Broken third-party header facades must not hide an error.
        return None
    return value if 0 <= value <= 7200 else None


def response(payload: object, status: int, *, protocol: str = 'openai',
             headers: Mapping[str, str] | None = None,
             request_id: str | None = None,
             upstream_request_id: str | None = None) -> JSONResponse:
    """Normalize an error without dropping its business code or HTTP status.

    A valid Retry-After header is authoritative when an upstream body disagrees.
    Both channels then carry the same bounded delay. Body-only provider advice
    remains body-only; it is not permission for the gateway to replay a request.
    """
    body = dict(payload) if isinstance(payload, dict) else {}
    original = body.get('error')
    error = dict(original) if isinstance(original, dict) else {}
    message = error.get('message')
    if not isinstance(message, str) or not message:
        message = (original if isinstance(original, str) and original else
                   body.get('message') or body.get('detail') or
                   (payload if isinstance(payload, str) and payload else
                    f'上游 HTTP {status}'))
        message = str(message)
    error['message'] = message[:4096]
    kind = error.get('type')
    if not isinstance(kind, str) or not kind:
        kind = error_type(status, protocol)
    elif protocol == 'anthropic':
        # OpenAI-specific categories have no native Messages equivalent. Keep
        # business codes/hints, and use the native category for the same status.
        if kind in ('server_error', 'insufficient_quota') or (
                kind == 'invalid_request_error' and status in (404, 413)):
            kind = error_type(status, protocol)
    error['type'] = kind
    body['error'] = error
    if protocol == 'anthropic':
        body['type'] = 'error'
    if request_id:
        body['request_id'] = request_id
    if upstream_request_id:
        body['upstream_request_id'] = upstream_request_id
    outgoing = {key: value for key, value in (headers or {}).items()
                if key.lower() not in ('content-length', 'content-type')}
    wait = retry_after_seconds(outgoing)
    outgoing = {key: value for key, value in outgoing.items()
                if key.lower() != 'retry-after'}
    if wait is not None:
        outgoing['Retry-After'] = str(wait)
        error['retry_after_seconds'] = wait
    elif 'retry_after_seconds' in error:
        delay = error['retry_after_seconds']
        if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 7200:
            error.pop('retry_after_seconds')
    return JSONResponse(body, status_code=status, headers=outgoing)


def adapt(original: JSONResponse, *, protocol: str,
          request_id: str | None = None) -> JSONResponse:
    """Translate a local rejection while retaining status, IDs and headers."""
    return response(json.loads(original.body), original.status_code,
                    protocol=protocol, headers=original.headers,
                    request_id=request_id)


def protocol_for_path(path: str) -> str:
    """Use complete path components; include base paths and platform aliases."""
    parts = path.rstrip('/').split('/')
    if parts[-1:] == ['messages'] or parts[-2:] == ['messages', 'count_tokens']:
        return 'anthropic'
    return 'openai'
