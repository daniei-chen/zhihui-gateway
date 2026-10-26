"""数据面：对外提供统一的 OpenAI 兼容入口。

设计要点（对照 WorkBuddy 的数据面取舍）：

* **一个 base URL + 一个 key**：客户端只认 qtg 的 `sk-qtg-*`，由 qtg 决定打到
  Qoder 还是 Trae；上游凭据只存在本机 0600 文件里，不下发。
* **显式前缀优先**：`qoder/Auto`、`trae/glm-5.3` 直接指定平台；不带前缀时按
  模型目录匹配，多个平台都有则按「余额 → 平台顺序」挑，并把理由写进流水。
* **SSE 原样透传**：不在网关侧重组分片，避免破坏客户端解析；只在尾部采信
  上游 usage（不自算，见 WorkBuddy 第 12 条）。
* **每个请求落一条流水**：时间/平台/模型/状态/token/耗时/首字/路由理由，
  用于按 key 与按模型对账。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import metrics
from ..errors import BadRequest, ConfigMissing, Forbidden, RateLimited, Unauthorized, UpstreamError
from ..model_map import resolve as resolve_model
from ..store import KeyRow

router = APIRouter(tags=["data-plane"])

PLATFORMS = ("qoder", "trae", "workbuddy")

# 进程内在途计数：用于每 key 并发上限。单进程部署够用；
# 多副本时要换成 Redis（与 WorkBuddy 的已知短板一致，已写入文档）。
_inflight: Dict[str, int] = {}
_inflight_lock = asyncio.Lock()


async def _acquire_slot(key: KeyRow) -> None:
    if not key.max_concurrency:
        return
    async with _inflight_lock:
        cur = _inflight.get(key.id, 0)
        if cur >= key.max_concurrency:
            raise RateLimited(
                f"该密钥并发已达上限 {key.max_concurrency}（在途 {cur}）", code="concurrency_limit"
            )
        _inflight[key.id] = cur + 1


async def _release_slot(key: KeyRow) -> None:
    if not key.max_concurrency:
        return
    async with _inflight_lock:
        cur = _inflight.get(key.id, 0)
        _inflight[key.id] = max(0, cur - 1)
_AUTH_HINT = "请使用管理端「密钥」页创建的 sk-qtg-* 密钥；上游凭据由网关保管。"


# ------------------------------------------------------------------ 基础
def _rt(request: Request):
    return request.app.state.rt


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


def _host_platform(request: Request) -> str:
    """按域名区分平台：qoder.example.com 只服务 Qoder，trae.example.com 只服务 Trae。

    这样「URL 分开」就有实际语义：两个域各用各的密钥与模型，直连 127.0.0.1 时不限制。
    """
    # 显式头优先：nginx 把 /qoder/v1、/trae/v1 的前缀翻译成这个头，
    # 于是同一个域名下也能按路径区分平台——模型名重复时这是唯一可靠的区分方式。
    _hdr = (request.headers.get("x-qtg-platform") or "").strip().lower()
    if _hdr in PLATFORMS:
        return _hdr
    host = (request.headers.get("host") or "").lower()
    # 第三个平台：workbuddy 自己的域名（workbuddy.example.com）或
    # workbuddy2api 面板域名。没配这个域名时走下面两个分支，
    # 直连 127.0.0.1 则返回 "" 不限制——三平台都能用。
    if "workbuddy" in host or "codebuddy" in host:
        return "workbuddy"
    if "trae" in host:
        return "trae"
    if "qoder" in host:
        return "qoder"
    return ""


def _client_ip(request: Request) -> str:
    """取真实来源 IP：nginx 会写 X-Real-IP / X-Forwarded-For。"""
    xr = (request.headers.get("x-real-ip") or "").strip()
    if xr:
        return xr
    xf = (request.headers.get("x-forwarded-for") or "").strip()
    if xf:
        return xf.split(",")[0].strip()
    return request.client.host if request.client else ""


def _ip_allowed(allow: List[str], ip: str) -> bool:
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip)
    except Exception:  # noqa: BLE001
        return False
    for net in allow:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _auth_key(request: Request) -> KeyRow:
    rt = _rt(request)
    if not rt.settings.proxy_enabled:
        raise Forbidden("数据面已关闭（QTG_PROXY_ENABLED=0）")
    plain = _bearer(request)
    if not plain:
        raise Unauthorized("缺少 API Key（Authorization: Bearer sk-qtg-...）")
    key = rt.store.verify_key(plain)
    if key is None:
        raise Unauthorized(f"API Key 无效或已禁用。{_AUTH_HINT}")
    # 过期检查：前端填的有效期以前被静默丢弃（审计 P1），现在真的生效。
    if getattr(key, "expires_at", 0):
        now = time.time()
        if now > key.expires_at:
            raise Forbidden(
                f"该密钥已于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(key.expires_at))} 过期",
                code="key_expired",
            )
    ip = _client_ip(request)
    if key.ip_allowlist and not _ip_allowed(key.ip_allowlist, ip):
        raise Forbidden(
            f"该密钥不允许从 {ip} 访问（白名单：{', '.join(key.ip_allowlist[:5])}）",
            code="ip_not_allowed",
        )
    if key.max_ips and not rt.store.register_key_ip(key.id, ip, key.max_ips):
        raise Forbidden(
            f"该密钥最多允许 {key.max_ips} 个来源 IP（{ip} 已超出，最近 24h 内）",
            code="ip_limit",
        )
    return key


def _upstream_key(rt, platform: str) -> str:
    """数据面用的上游密钥（不同引擎的「密钥」不是同一个东西）：
    * qoder + bff     → qoder-bff 的 API Key
    * qoder + qoder2  → qoder2 的 Key（它关了鉴权时可以不填）
    * trae  + bff     → trae-bff 的 Key
    * trae  + aiwork  → aiwork-server 的 API Key（**必须**，它强制鉴权）
    """
    if platform == "workbuddy":
        w = getattr(rt, "wb", None)
        return w.api_key() if w is not None else ""
    if platform == "qoder":
        path = rt.settings.qoder_api_key_file
    elif (rt.settings.trae_engine or "bff") == "aiwork":
        path = rt.settings.aiwork_api_key_file
    else:
        path = rt.settings.trae_api_key_file
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _base(rt, platform: str) -> str:
    """数据面转发目标。

    qoder 侧跟随 `QTG_QODER_ENGINE`（见 config._engine_default）：
      * qoder2 → 新引擎（:8799，自带 COSY 签名 / 设备指纹 / 排队重试 / 熔断）
      * bff    → 旧 Go 控制面（:8791，仅回滚用）
    两个引擎都实现了 OpenAI 兼容的 `/v1/*`，所以这里只需要换 base。
    """
    if platform == "workbuddy":
        # workbuddy2api(:7863) 自身就是 OpenAI 兼容反代，直接转发；
        # 它管着自己的账号池、排队、冷却，qtg 不做二次调度。
        return rt.settings.wb_api_base
    if platform == "qoder":
        if (rt.settings.qoder_engine or "bff") == "qoder2":
            return rt.settings.qoder2_base
        return rt.settings.qoder_base
    # trae 侧同理（QTG_TRAE_ENGINE）：
    #   aiwork → aiwork-server(:8080)，Rust 实现、自带 trae OAuth/指纹/签到
    #   bff    → 旧 trae-bff(:8792)，源码丢失，仅回滚
    if (rt.settings.trae_engine or "bff") == "aiwork":
        return rt.settings.aiwork_base
    return rt.settings.trae_base


async def _catalog(rt) -> Dict[str, List[str]]:
    """各平台可用模型 id 列表（带缓存；上游 key 缺失时返回空）。"""
    async def load() -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for platform in PLATFORMS:
            token = _upstream_key(rt, platform)
            if not token:
                out[platform] = []
                continue
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        _base(rt, platform) + "/v1/models",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                if resp.status_code != 200:
                    out[platform] = []
                    continue
                data = resp.json()
                items = data.get("data") if isinstance(data, dict) else data
                ids = []
                for m in items or []:
                    if isinstance(m, dict) and m.get("id"):
                        ids.append(str(m["id"]))
                out[platform] = ids
            except Exception:  # noqa: BLE001 - 目录失败不影响路由前缀模式
                out[platform] = []
        return out

    from ..cache import cache

    return await cache.get_or_set("proxy.catalog", rt.settings.proxy_model_cache_sec, load)


async def _platform_credits(rt) -> Dict[str, float]:
    """平台可用余额，用于多平台同名模型的选择理由。失败则空（不影响主流程）。"""
    try:
        from ..service import build_aggregate, summarize

        agg = await build_aggregate(rt)
        return {
            "qoder": summarize(agg.qoder_accounts).credits,
            "trae": summarize(agg.trae_accounts).credits,
            "workbuddy": summarize(agg.wb_accounts).credits,
        }
    except Exception:  # noqa: BLE001
        return {}


async def _route(rt, key: KeyRow, model: str) -> Tuple[str, str, str]:
    """返回 (platform, upstream_model, reason)。"""
    if not model:
        raise BadRequest("缺少 model 字段")
    if key.models and model not in key.models:
        # 允许带前缀写法
        stripped = model.split("/", 1)[-1]
        if stripped not in key.models:
            raise BadRequest(
                f"该密钥无权访问模型 {model}（允许：{', '.join(key.models[:8])}）", code="model_not_allowed"
            )

    if "/" in model:
        prefix, rest = model.split("/", 1)
        if prefix in PLATFORMS and rest:
            if prefix not in key.platforms:
                raise BadRequest(f"该密钥无权访问平台 {prefix}", code="platform_not_allowed")
            return prefix, rest, "prefix"

    cat = await _catalog(rt)
    hits = [p for p in PLATFORMS if model in (cat.get(p) or []) and p in key.platforms]
    if len(hits) == 1:
        return hits[0], model, "catalog"
    if len(hits) > 1:
        credits = await _platform_credits(rt)
        hits.sort(key=lambda p: (-credits.get(p, 0.0), PLATFORMS.index(p)))
        return hits[0], model, f"catalog:balance({credits.get(hits[0], 0):.0f})"
    # 目录不可用（上游 key 未配置）时，按密钥允许的平台顺序兜底
    allowed = [p for p in PLATFORMS if p in key.platforms]
    if len(allowed) == 1:
        return allowed[0], model, "fallback:single-platform"
    raise BadRequest(
        f"无法确定模型 {model} 属于哪个平台：请确认三枚上游 API Key 都已配置，"
        f"或使用带前缀写法（qoder/{model}、trae/{model}、workbuddy/{model}）",
        code="model_unroutable",
    )


def _extract_usage(obj: Any) -> Dict[str, int]:
    if not isinstance(obj, dict):
        return {}
    u = obj.get("usage") or {}
    if not isinstance(u, dict):
        return {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    cached = int(
        u.get("prompt_cache_hit_tokens")
        or u.get("cached_tokens")
        or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    return {"prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached}


def _extract_usage_from_sse(tail: bytes) -> Dict[str, int]:
    """从 SSE 尾部找最后一个非空 usage。"""
    found: Dict[str, int] = {}
    for line in tail.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        u = _extract_usage(obj)
        if u:
            found = u
    return found


# ------------------------------------------------------------------ 端点
@router.get("/v1/models")
async def list_models(request: Request) -> dict:
    rt = _rt(request)
    key = await _auth_key(request)
    cat = await _catalog(rt)
    host_plat = _host_platform(request)
    host_plat = _host_platform(request)
    if host_plat and host_plat not in key.platforms:
        raise Forbidden(
            f"该密钥不能用于 {host_plat} 平台（密钥只绑 {', '.join(key.platforms)}）",
            code="platform_mismatch",
        )
    data: List[dict] = []
    for platform in PLATFORMS:
        if platform not in key.platforms:
            continue
        if host_plat and platform != host_plat:
            continue
        for mid in cat.get(platform) or []:
            data.append({
                "id": f"{platform}/{mid}",
                "object": "model",
                "created": 0,
                "owned_by": platform,
                "platform": platform,
                "upstream_id": mid,
            })
    return {"object": "list", "data": data}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    rt = _rt(request)
    key = await _auth_key(request)

    limit_bytes = rt.settings.proxy_max_body_mb * 1024 * 1024
    # 先用 Content-Length **前置**拒绝，再读 body。
    # 旧实现是「先把整个 body 读进内存再比上限」—— 几个 32MB 并发就能把单进程
    # 内存推到几百 MB（审计点名的内存洞）。这里 Content-Length 缺失/伪造时才
    # 落到后面的读后校验兜底。
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit_bytes:
        raise BadRequest(
            f"请求体 {int(declared)/1048576:.1f}MB 超过上限 {rt.settings.proxy_max_body_mb}MB"
            "（QTG_PROXY_MAX_BODY_MB 可调）",
            code="body_too_large",
        )
    raw = await request.body()
    if len(raw) > limit_bytes:
        raise BadRequest(
            f"请求体 {len(raw)/1048576:.1f}MB 超过上限 {rt.settings.proxy_max_body_mb}MB"
            "（QTG_PROXY_MAX_BODY_MB 可调）",
            code="body_too_large",
        )
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise BadRequest(f"请求体不是合法 JSON: {exc}") from exc

    model = str(body.get("model") or "")
    model = resolve_model(rt, model)  # 设置 → 模型映射（别名 → 真实模型）
    stream = bool(body.get("stream"))
    try:
        platform, upstream_model, reason = await _route(rt, key, model)
    except Exception as exc:  # noqa: BLE001 - 路由失败同样要留痕
        rt.store.record_request(
            key=key, platform="", model=model, status=getattr(exc, "status_code", 400),
            stream=bool(body.get("stream")), error=f"{getattr(exc, 'code', 'route')}: {exc}"[:200],
        )
        raise

    host_plat = _host_platform(request)
    if host_plat and platform != host_plat:
        raise BadRequest(
            f"该域名只服务 {host_plat} 平台（{model} 属于 {platform}）；"
            f"请换用对应域名或去掉模型前缀",
            code="platform_mismatch",
        )
    if key.daily_limit:
        used = rt.store.key_usage_today(key.id)
        if used >= key.daily_limit:
            rt.store.record_request(key=key, platform=platform, model=upstream_model, status=429,
                                    stream=stream, route_reason=reason, error="daily_limit")
            raise RateLimited(
                f"该密钥今日已用 {used} tokens，达到上限 {key.daily_limit}", code="daily_limit"
            )
    if key.monthly_limit:
        used_m = rt.store.key_usage_month(key.id)
        if used_m >= key.monthly_limit:
            rt.store.record_request(key=key, platform=platform, model=upstream_model, status=429,
                                    stream=stream, route_reason=reason, error="monthly_limit")
            raise RateLimited(
                f"该密钥本月已用 {used_m} tokens，达到上限 {key.monthly_limit}", code="monthly_limit"
            )
    await _acquire_slot(key)

    upstream_key = _upstream_key(rt, platform)
    if not upstream_key:
        # 失败也要落流水：否则「网关报错了但流水里查不到」无法对账
        rt.store.record_request(
            key=key, platform=platform, model=upstream_model, status=503,
            stream=stream, route_reason=reason, error="missing_upstream_key",
        )
        raise ConfigMissing(
            f"未配置 {platform} 上游 API Key：请在「密钥 → 上游凭据」填写并保存（"
            f"{rt.settings.qoder_api_key_file if platform == 'qoder' else rt.settings.trae_api_key_file}）",
            code="missing_upstream_key",
        )

    body["model"] = upstream_model
    url = _base(rt, platform) + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    # 会话粘性：把客户端会话映射到上游字段，提升前缀缓存命中（WorkBuddy 第 11 条）
    session_id = request.headers.get("x-session-id") or ""
    if session_id and not body.get("conversation_id"):
        body["conversation_id"] = session_id

    started = time.monotonic()
    client = httpx.AsyncClient(timeout=rt.settings.proxy_timeout_sec)
    try:
        req = client.build_request("POST", url, json=body, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        rt.store.record_request(key=key, platform=platform, model=upstream_model, status=504,
                                stream=stream, route_reason=reason, error="upstream timeout")
        await _release_slot(key)
        raise UpstreamError(f"{platform} 上游超时", code="upstream_timeout") from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        rt.store.record_request(key=key, platform=platform, model=upstream_model, status=502,
                                stream=stream, route_reason=reason, error=str(exc)[:200])
        await _release_slot(key)
        raise UpstreamError(f"{platform} 上游不可达: {exc}") from exc

    if resp.status_code >= 400:
        err_body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        duration = int((time.monotonic() - started) * 1000)
        rt.store.record_request(key=key, platform=platform, model=upstream_model,
                                status=resp.status_code, stream=stream, duration_ms=duration,
                                route_reason=reason, error=err_body[:200].decode("utf-8", "replace"))
        await _release_slot(key)
        metrics.inc("qtg_proxy_requests_total", {"platform": platform, "status": str(resp.status_code)})
        return Response(
            content=err_body,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={"x-qtg-platform": platform, "x-qtg-route": reason},
        )

    metrics.inc("qtg_proxy_requests_total", {"platform": platform, "status": "200"})

    if not stream:
        data = await resp.aread()
        await resp.aclose()
        await client.aclose()
        duration = int((time.monotonic() - started) * 1000)
        usage = {}
        try:
            usage = _extract_usage(json.loads(data))
        except Exception:
            pass
        rt.store.record_request(key=key, platform=platform, model=upstream_model, status=200,
                                stream=False, duration_ms=duration, route_reason=reason, **usage)
        rt.store.touch_key(key.id)
        await _release_slot(key)
        return Response(
            content=data,
            status_code=200,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={"x-qtg-platform": platform, "x-qtg-route": reason,
                     "x-qtg-upstream-model": upstream_model},
        )

    async def gen():
        tail = b""
        ttft = 0
        status = 200
        error = ""
        try:
            async for chunk in resp.aiter_bytes():
                if not ttft and chunk:
                    ttft = int((time.monotonic() - started) * 1000)
                if len(chunk) < 65536:
                    tail = (tail + chunk)[-16384:]
                yield chunk
        except Exception as exc:  # noqa: BLE001 - 客户端断开/上游中断
            status = 499
            error = str(exc)[:200]
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
            await client.aclose()
            usage = _extract_usage_from_sse(tail)
            rt.store.record_request(
                key=key, platform=platform, model=upstream_model, status=status,
                stream=True, duration_ms=int((time.monotonic() - started) * 1000),
                ttft_ms=ttft, route_reason=reason, error=error, **usage,
            )
            rt.store.touch_key(key.id)
            await _release_slot(key)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "x-qtg-platform": platform,
            "x-qtg-route": reason,
            "x-qtg-upstream-model": upstream_model,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/v1/usage")
async def data_plane_usage(request: Request, days: int = Query(default=1, ge=1, le=90)) -> dict:
    rt = _rt(request)
    key = await _auth_key(request)
    stats = rt.store.stats(days)
    return {
        "key": key.public(),
        "today_tokens": rt.store.key_usage_today(key.id),
        "window": stats,
    }


@router.get("/v1/health")
async def data_plane_health(request: Request) -> dict:
    rt = _rt(request)
    cat = await _catalog(rt)
    return {
        "ok": True,
        "proxy_enabled": rt.settings.proxy_enabled,
        "upstreams": {
            p: {
                "configured": bool(_upstream_key(rt, p)),
                "models": len(cat.get(p) or []),
                "base": _base(rt, p),
            }
            for p in PLATFORMS
        },
    }


# ------------------------------------------------------------------ OpenAI Responses API
# 客户端（cc-switch / Codex 一类）把「API 格式」选成 Responses 时会打 `/v1/responses`。
# qtg 上游只有 Chat Completions，这里做一层格式转换：request → chat，chat → response。
def _responses_to_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    """Responses 请求 → Chat Completions 请求（只做基础字段，工具调用暂不转换）。"""
    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    parts.append(c["text"])
                elif isinstance(c, str):
                    parts.append(c)
            return "\n".join(parts)
        return ""

    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, dict):
                messages.append({"role": str(item.get("role") or "user"),
                                 "content": _text_of(item.get("content"))})
            elif isinstance(item, str):
                messages.append({"role": "user", "content": item})
    if not messages:
        messages.append({"role": "user", "content": ""})
    chat: Dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    if body.get("max_output_tokens") is not None:
        chat["max_tokens"] = int(body["max_output_tokens"])
    for k in ("temperature", "top_p", "stop", "presence_penalty", "frequency_penalty"):
        if body.get(k) is not None:
            chat[k] = body[k]
    return chat


def _chat_to_response(chat: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Chat Completions 响应 → Responses 响应（非流式）。"""
    import uuid

    choice = (chat.get("choices") or [{}])[0]
    text = str((choice.get("message") or {}).get("content") or "")
    usage = chat.get("usage") or {}
    return {
        "id": f"resp_{chat.get('id') or uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [{
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "output_text": text,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }


@router.post("/v1/responses")
async def responses_api(request: Request):
    """OpenAI Responses API（`/v1/responses`）：转换到上游 Chat Completions。

    很多客户端（cc-switch / Codex 一类）把「API 格式」选成 Responses 时只会打这个
    端点；qtg 上游没有 Responses，这里做请求/响应（含流式）双向转换。
    """
    rt = _rt(request)
    key = await _auth_key(request)
    limit_bytes = rt.settings.proxy_max_body_mb * 1024 * 1024
    raw = await request.body()
    if len(raw) > limit_bytes:
        raise BadRequest(
            f"请求体 {len(raw)/1048576:.1f}MB 超过上限 {rt.settings.proxy_max_body_mb}MB",
            code="body_too_large",
        )
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise BadRequest(f"请求体不是合法 JSON: {exc}") from exc

    model = resolve_model(rt, str(body.get("model") or ""))
    stream = bool(body.get("stream"))
    try:
        platform, upstream_model, reason = await _route(rt, key, model)
    except Exception as exc:  # noqa: BLE001
        rt.store.record_request(
            key=key, platform="", model=model, status=getattr(exc, "status_code", 400),
            stream=stream, error=f"responses: {str(exc)[:160]}",
        )
        raise
    host_plat = _host_platform(request)
    if host_plat and platform != host_plat:
        raise BadRequest(
            f"该域名只服务 {host_plat} 平台（{model} 属于 {platform}）",
            code="platform_mismatch",
        )
    if key.daily_limit and rt.store.key_usage_today(key.id) >= key.daily_limit:
        raise RateLimited(f"该密钥今日已达上限 {key.daily_limit}", code="daily_limit")
    if key.monthly_limit and rt.store.key_usage_month(key.id) >= key.monthly_limit:
        raise RateLimited(f"该密钥本月已达上限 {key.monthly_limit}", code="monthly_limit")
    await _acquire_slot(key)

    upstream_key = _upstream_key(rt, platform)
    if not upstream_key:
        await _release_slot(key)
        rt.store.record_request(key=key, platform=platform, model=upstream_model, status=503,
                                stream=stream, route_reason=reason, error="missing_upstream_key")
        raise ConfigMissing(f"未配置 {platform} 上游 API Key", code="missing_upstream_key")

    chat_body = _responses_to_chat(body)
    chat_body["model"] = upstream_model
    url = _base(rt, platform) + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    started = time.monotonic()
    client = httpx.AsyncClient(timeout=rt.settings.proxy_timeout_sec)
    try:
        req = client.build_request("POST", url, json=chat_body, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        await _release_slot(key)
        rt.store.record_request(key=key, platform=platform, model=upstream_model, status=504,
                                stream=stream, route_reason=reason, error="upstream timeout")
        raise UpstreamError(f"{platform} 上游超时", code="upstream_timeout") from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        await _release_slot(key)
        rt.store.record_request(key=key, platform=platform, model=upstream_model, status=502,
                                stream=stream, route_reason=reason, error=str(exc)[:200])
        raise UpstreamError(f"{platform} 上游不可达: {exc}") from exc

    if resp.status_code >= 400:
        err = await resp.aread()
        await resp.aclose()
        await client.aclose()
        await _release_slot(key)
        rt.store.record_request(
            key=key, platform=platform, model=upstream_model, status=resp.status_code,
            stream=stream, route_reason=reason,
            error=err[:200].decode("utf-8", "replace"),
        )
        return JSONResponse(status_code=resp.status_code, content={"error": {
            "type": "upstream_error",
            "message": err[:400].decode("utf-8", "replace"),
        }})

    if not stream:
        data = await resp.aread()
        await resp.aclose()
        await client.aclose()
        duration = int((time.monotonic() - started) * 1000)
        usage: Dict[str, Any] = {}
        chat_json: Dict[str, Any] = {}
        try:
            chat_json = json.loads(data)
            usage = _extract_usage(chat_json)
        except Exception:  # noqa: BLE001
            pass
        if isinstance(chat_json.get("error"), dict):
            # 上游把错误塞在 200 的 body 里：**只记一条 502**，并且只释放一次槽位。
            # 旧实现先记 200、再记 502，两次 `_release_slot` —— 既是重复记账，
            # 也让并发计数被多减一次（审计点名）。
            msg = str((chat_json.get("error") or {}).get("message") or chat_json["error"])[:400]
            rt.store.record_request(key=key, platform=platform, model=upstream_model,
                                    status=502, stream=False, duration_ms=duration,
                                    route_reason=reason, error=msg)
            rt.store.touch_key(key.id)
            await _release_slot(key)
            return JSONResponse(status_code=502, content={"error": {
                "type": "upstream_error", "message": msg}})
        rt.store.record_request(key=key, platform=platform, model=upstream_model, status=200,
                                stream=False, duration_ms=duration, route_reason=reason, **usage)
        rt.store.touch_key(key.id)
        await _release_slot(key)
        out = _chat_to_response(chat_json, upstream_model)
        return JSONResponse(content=out, headers={
            "x-qtg-platform": platform, "x-qtg-route": reason,
            "x-qtg-upstream-model": upstream_model,
        })

    async def gen():
        import uuid

        used: Dict[str, Any] = {}
        resp_id = f"resp_{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        ttft = 0
        status = 200
        error = ""
        head = {"type": "response.created", "response": {
            "id": resp_id, "object": "response", "created_at": created,
            "status": "in_progress", "model": upstream_model}}
        yield (f"event: response.created\ndata: {json.dumps(head, ensure_ascii=False)}\n\n").encode()
        try:
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:  # noqa: BLE001
                    continue
                err_obj = obj.get("error")
                if isinstance(err_obj, dict):
                    status = 502
                    error = str(err_obj.get("message") or err_obj)[:300]
                    fail = {"type": "response.failed", "response": {
                        "id": resp_id, "status": "failed",
                        "error": {"code": "upstream_error", "message": error}}}
                    yield (f"event: response.failed\ndata: {json.dumps(fail, ensure_ascii=False)}\n\n").encode()
                    break
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if isinstance(delta, str) and delta:
                    if not ttft:
                        ttft = int((time.monotonic() - started) * 1000)
                    evt = {"type": "response.output_text.delta", "delta": delta}
                    yield (f"event: response.output_text.delta\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n").encode()
                if isinstance(obj.get("usage"), dict):
                    used = _extract_usage(obj)
        except Exception as exc:  # noqa: BLE001
            status = 499
            error = str(exc)[:200]
        finally:
            try:
                await resp.aclose()
            except Exception:  # noqa: BLE001
                pass
            await client.aclose()
            rt.store.record_request(
                key=key, platform=platform, model=upstream_model, status=status,
                stream=True, duration_ms=int((time.monotonic() - started) * 1000),
                ttft_ms=ttft, route_reason=reason, error=error, **used,
            )
            rt.store.touch_key(key.id)
            await _release_slot(key)
        done = {"type": "response.completed", "response": {
            "id": resp_id, "object": "response", "created_at": created,
            "status": "completed", "model": upstream_model,
            "usage": {
                "input_tokens": int(used.get("prompt_tokens") or 0),
                "output_tokens": int(used.get("completion_tokens") or 0),
                "total_tokens": int(used.get("total_tokens") or 0),
            }}}
        yield (f"event: response.completed\ndata: {json.dumps(done, ensure_ascii=False)}\n\n").encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "x-qtg-platform": platform, "x-qtg-route": reason,
    })
