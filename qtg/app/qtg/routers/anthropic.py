"""Anthropic Messages API 兼容层（/v1/messages）。

Claude Code 这类客户端把 API 格式选成 Anthropic 时会打 `/v1/messages`。
qoder2 引擎**原生实现**了该协议（`qoder_proxy.py:10078` 有 `/v1/messages`
与 `/v1/messages/count_tokens`），workbuddy manager 也有自己的实现，
但 qtg 之前没把这个端点暴露出来 —— 直连 `:8799` 可用，经 qtg 返回 405。

这里**只做透传，不转换协议**：Anthropic 的 thinking / tool_use / stop_reason
语义与 OpenAI 不同，硬转必丢东西。所以请求体原样转给支持该协议的上游。

支持范围：qoder2（原生）。其他平台明确报错，不假装支持。
"""
from __future__ import annotations

import json
import re
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from ..errors import BadRequest, UpstreamError
from .proxy import PLATFORMS, _auth_key, _rt, _upstream_key

router = APIRouter(tags=["anthropic"])

# 目前原生支持 Anthropic 协议的上游
_ANTHROPIC_PLATFORMS = ("qoder",)


async def _read_body(request: Request, limit_mb: int):
    """读请求体，先按 Content-Length 前置拒绝（与 chat/completions 同一套）。"""
    limit_bytes = limit_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit_bytes:
        raise BadRequest(
            f"请求体 {int(declared)/1048576:.1f}MB 超过上限 {limit_mb}MB",
            code="body_too_large",
        )
    raw = await request.body()
    if len(raw) > limit_bytes:
        raise BadRequest("请求体超过上限", code="body_too_large")
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise BadRequest(f"请求体不是合法 JSON: {exc}") from exc


def _resolve_model(raw_key, body) -> str:
    """解析模型：允许 `qoder/xxx` 前缀；其他平台前缀明确拒绝。"""
    model = str(body.get("model") or "")
    upstream = model
    if "/" in model:
        prefix, rest = model.split("/", 1)
        if prefix in PLATFORMS and rest:
            if prefix not in _ANTHROPIC_PLATFORMS:
                raise BadRequest(
                    f"Anthropic /v1/messages 目前只接 qoder 平台（收到 {prefix}）；"
                    "workbuddy 的 Anthropic 兼容在它自己的面板（:7864）",
                    code="anthropic_platform",
                )
            upstream = rest
    if "qoder" not in (getattr(raw_key, "platforms", None) or []):
        raise BadRequest("该密钥无权访问 qoder 平台", code="platform_not_allowed")
    return upstream


async def _forward(rt, key, body, path: str, upstream_model: str):
    """把 Anthropic 请求透传给 qoder2，并按 qtg 的流水口径记账。"""
    if (rt.settings.qoder_engine or "bff") != "qoder2":
        raise BadRequest(
            "Anthropic /v1/messages 需要 qoder2 引擎（当前 QTG_QODER_ENGINE 不是 qoder2）",
            code="anthropic_unavailable",
        )
    body = dict(body or {})
    body["model"] = upstream_model
    stream = bool(body.get("stream"))

    url = rt.settings.qoder2_base + path
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    uk = _upstream_key(rt, "qoder")
    if uk:
        headers["Authorization"] = f"Bearer {uk}"

    started = time.monotonic()
    client = httpx.AsyncClient(timeout=rt.settings.proxy_timeout_sec)
    try:
        req = client.build_request("POST", url, json=body, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        rt.store.record_request(
            key=key, platform="qoder", model=upstream_model,
            status=502, stream=stream, error=str(exc)[:200],
        )
        raise UpstreamError(f"qoder2 上游不可达: {exc}") from exc

    if resp.status_code >= 400:
        err_body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        rt.store.record_request(
            key=key, platform="qoder", model=upstream_model, status=resp.status_code,
            stream=stream, duration_ms=int((time.monotonic() - started) * 1000),
            error=err_body[:200].decode("utf-8", "replace"),
        )
        return Response(
            content=err_body, status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    if not stream:
        payload = await resp.aread()
        await resp.aclose()
        await client.aclose()
        pt = ct = 0
        try:
            d = json.loads(payload or b"{}")
            u = d.get("usage") or {}
            pt = int(u.get("input_tokens") or 0)
            ct = int(u.get("output_tokens") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        rt.store.record_request(
            key=key, platform="qoder", model=upstream_model, status=200, stream=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=pt, completion_tokens=ct,
        )
        rt.store.touch_key(key.id)
        return Response(
            content=payload, media_type="application/json",
            headers={"x-qtg-platform": "qoder", "x-qtg-upstream-model": upstream_model},
        )

    async def gen():
        ttft = None
        tail = bytearray()
        try:
            async for chunk in resp.aiter_bytes():
                if ttft is None and chunk:
                    ttft = int((time.monotonic() - started) * 1000)
                tail.extend(chunk)
                if len(tail) > 8192:
                    del tail[:-4096]
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            txt = bytes(tail).decode("utf-8", "replace")
            m = re.search(r'"input_tokens"\s*:\s*(\d+)', txt)
            n = re.search(r'"output_tokens"\s*:\s*(\d+)', txt)
            rt.store.record_request(
                key=key, platform="qoder", model=upstream_model, status=200, stream=True,
                duration_ms=int((time.monotonic() - started) * 1000), ttft_ms=ttft,
                prompt_tokens=int(m.group(1)) if m else 0,
                completion_tokens=int(n.group(1)) if n else 0,
            )
            rt.store.touch_key(key.id)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={
            "x-qtg-platform": "qoder",
            "x-qtg-upstream-model": upstream_model,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages（透传到原生支持该协议的上游）。"""
    rt = _rt(request)
    key = await _auth_key(request)
    body = await _read_body(request, rt.settings.proxy_max_body_mb)
    upstream_model = _resolve_model(key, body)
    return await _forward(rt, key, body, "/v1/messages", upstream_model)


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    """Anthropic token 预估（同样透传）。"""
    rt = _rt(request)
    key = await _auth_key(request)
    body = await _read_body(request, rt.settings.proxy_max_body_mb)
    upstream_model = _resolve_model(key, body)
    return await _forward(rt, key, body, "/v1/messages/count_tokens", upstream_model)
