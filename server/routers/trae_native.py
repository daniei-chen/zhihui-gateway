"""trae 原生 Anthropic 直通（0.1.1）。

平台 = trae 时，AIwork 的 OpenAI 端点（`/v1/chat/completions`）不透传 tools
（实测 `tool_choice: "required"` 仍不返回工具调用），而它原生的 Anthropic 端点
(`/v1/messages`) 能正确返回 `tool_use`。翻译链会让工具调用彻底丢失，因此这里
绕过 `to_openai_request`，把原始 Anthropic 请求直接转发给上游。

预算/预扣已由调用方用转换后的 OpenAI payload 完成（gateway._prepare_forward_async），
本模块只负责传输、日志与错误形状。
"""
from __future__ import annotations

import json
import time

from fastapi.responses import Response, StreamingResponse

from .. import config
from . import gateway
from .. import deadline as deadline_mod


def _err(message, status=500, err_type='api_error', code=None):
    err = {'type': err_type, 'message': message}
    if code:
        err['code'] = code
    body = {'type': 'error', 'error': err}
    return Response(content=json.dumps(body, ensure_ascii=False),
                    status_code=status, media_type='application/json')


async def trae_native_messages(request, body, key, ip, model, mapped, ua,
                               upstream, stream, session_key):
    """把原始 Anthropic 请求直接转发到 trae 引擎的 /v1/messages。"""
    url = str(upstream.get('base_url') or '').rstrip('/') + '/v1/messages'
    if not url.startswith('http'):
        return _err('trae 上游地址缺失', 502)
    raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
    headers = {
        'content-type': 'application/json',
        'x-api-key': str(upstream.get('api_key') or ''),
        'anthropic-version': request.headers.get('anthropic-version') or '2023-06-01',
        'accept': 'text/event-stream' if stream else 'application/json',
    }
    budgets = deadline_mod.request_budgets(config.UPSTREAM_TIMEOUT)
    t = deadline_mod.transport_timeouts(budgets)
    started = time.time()
    client = config.http_client(t['timeout'], connect=t['connect'])
    if not stream:
        try:
            resp = await client.post(url, content=raw, headers=headers)
        finally:
            await client.aclose()
        latency = int((time.time() - started) * 1000)
        ok = resp.status_code < 400
        gateway._record(key, ip, model, mapped or '', resp.status_code, 0, 0, latency, ua,
                        None if ok else resp.text[:200], False, session_key=session_key)
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get('content-type', 'application/json'))
    try:
        req = client.build_request('POST', url, content=raw, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise
    if resp.status_code >= 400:
        text = await resp.aread()
        await resp.aclose()
        await client.aclose()
        gateway._record(key, ip, model, mapped or '', resp.status_code, 0, 0,
                        int((time.time() - started) * 1000), ua,
                        text[:200].decode('utf-8', 'replace'), True, session_key=session_key)
        return Response(content=text, status_code=resp.status_code,
                        media_type=resp.headers.get('content-type', 'application/json'))

    async def gen():
        try:
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            gateway._record(key, ip, model, mapped or '', resp.status_code, 0, 0,
                            int((time.time() - started) * 1000), ua, None, True,
                            session_key=session_key)

    return StreamingResponse(gen(), status_code=resp.status_code,
                             media_type=resp.headers.get('content-type', 'text/event-stream'))
