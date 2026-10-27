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
from ..identity import platform_of

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
    """按域名区分平台：qoder.kaogong.art 只服务 Qoder，trae.kaogong.art 只服务 Trae。

    这样「URL 分开」就有实际语义：两个域各用各的密钥与模型，直连 127.0.0.1 时不限制。
    """
    # 显式头优先：nginx 把 /qoder/v1、/trae/v1 的前缀翻译成这个头，
    # 于是同一个域名下也能按路径区分平台——模型名重复时这是唯一可靠的区分方式。
    _hdr = platform_of(request)
    if _hdr in PLATFORMS:
        return _hdr
    host = (request.headers.get("host") or "").lower()
    # 第三个平台：workbuddy 自己的域名（workbuddy.kaogong.art）或
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


