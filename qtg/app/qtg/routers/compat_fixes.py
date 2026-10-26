"""契约修复与真数据接入（第 9 轮）。

覆盖 `compat.py` 里的同名 GET（本模块在 compat 之前注册）：

* 补全前端实际读取的字段——dashboard 靠 `auth_dir` 判断「这个分组有没有账号目录」，
  缺了它整页统计恒为 0；账号缺 `ttl_seconds` 时绿条按 90 天兜底算，30 天显示 1/3；
* 模型列表/目录用上游真实元数据（显示名、上下文、最大输出、能力标签），
  不再返回 `context_length: 0`；
* 按 `X-QTG-Platform` 请求头做平台隔离：密钥、模型、测试台都只返回当前平台的。

`X-QTG-Platform` 由前端请求拦截器写入（读 localStorage `qtg.platform`），
缺失时按 `qoder` 处理——这样即使旧前端也能工作。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import datetime as dt

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .. import manager_api
from ..service import all_accounts, build_aggregate

router = APIRouter(prefix="/api", tags=["compat-fixes"])

PLATFORMS = ("qoder", "trae", "workbuddy")

_SERIES = (
    ("auto", "Auto"), ("qwen", "Qwen"), ("deepseek", "DeepSeek"),
    ("glm", "智谱 GLM"), ("kimi", "Kimi"), ("minimax", "MiniMax"),
    ("hunyuan", "腾讯混元"), ("claude", "Claude"), ("gpt", "GPT"),
    ("doubao", "豆包"), ("gemini", "Gemini"),
)


def _rt(request: Request):
    return request.app.state.rt


def _to_epoch(v: Any) -> int:
    """把前端传的有效期转成 unix 秒。

    前端可能给：null / 空串（不过期）、数字（已是 epoch）、ISO 或 `YYYY-MM-DD`。
    以前这里**根本不读 expires_at**，用户填了也会被静默丢掉（审计 P1）。
    """
    if not v:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            naive = dt.datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
        return int(naive.replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
    return 0


def _platform(request: Request) -> str:
    raw = (request.headers.get("x-qtg-platform") or "").strip().lower()
    return raw if raw in PLATFORMS else "qoder"


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _series(mid: str) -> str:
    low = mid.lower()
    for key, label in _SERIES:
        if key in low:
            return label
    return (mid.split("-")[0].split(".")[0] or "其他")


def _ctx_best(m: Dict[str, Any]) -> int:
    """模型能支持的最大上下文。

    优先取 `context_windows` 的最大值（上游给的是多档：1M / 400K / 200K），
    它代表这个模型的真实能力上限；`context_length` 只是当前默认档
    （Qwen3.8-Max 的默认档是 180000，而它支持 1M）。
    """
    best = 0
    for x in (m.get("context_windows") or []):
        v = _i(x)
        if v > best:
            best = v
    if best > 0:
        return best
    return _i(m.get("context_length") or m.get("max_input_tokens"))


def _ctx_label(m: Dict[str, Any]) -> str:
    """最高档对应的显示标签（上游原样给的 "1M" / "400K" / "200K"）。

    用它而不是自己拿数字除 1024：200000/1024=195K 会把 200K 说小，
    1000000/1024/1024=0.95 会显示成 "1.0M"。
    """
    windows = [_i(x) for x in (m.get("context_windows") or [])]
    labels = [str(x) for x in (m.get("context_window_labels") or [])]
    if windows and labels:
        best = max(windows)
        try:
            idx = windows.index(best)
        except ValueError:
            idx = -1
        if 0 <= idx < len(labels):
            return labels[idx]
    # 上游只给了 context_length（trae 的 /v1/models 就是这样）：
    # 整千的值按 1000 进制给标签（128000 -> 128K、200000 -> 200K），
    # 只有真正的 2 的幂（131072）才按 1024，免得把 200K 说成 195K。
    n = _ctx_best(m)
    if n <= 0:
        return ""
    if n >= 1000000 and n % 1000000 == 0:
        return "%dM" % (n // 1000000)
    if n % 1000 == 0:
        return "%dK" % (n // 1000)
    if n >= 1024:
        return "%dK" % int(round(n / 1024))
    return str(n)


def _norm_models(raw: Dict[str, Any], platform: str) -> List[Dict[str, Any]]:
    """把上游两个 BFF 的模型结构拉平成同一份。

    qoder 的 `data[]` 有 name/description/capabilities/context_length/price_factor，
    没有推理档位与最大输出；trae 的 `data[]` 有 display/efforts/max_tokens/vendor/rate。
    差异就在这里抹平——界面只认一份字段。
    """
    out: List[Dict[str, Any]] = []
    for m in (raw or {}).get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        caps = m.get("capabilities") or {}
        # 推理档位：两个引擎都叫 `supported_efforts`（qoder2 的 /v1/models、
        # aiwork 的 /v1/models 都是这个字段），旧 BFF 时代 trae 叫 `efforts`。
        # 之前 qoder 分支写死 `efforts=[]`，导致测试台的「思考强度」下拉永远不出现。
        efforts = [
            str(x)
            for x in (
                m.get("supported_efforts")
                or m.get("reasoning_efforts")
                or m.get("efforts")
                or []
            )
            if str(x).strip()
        ]
        credits_override = ""
        if platform == "workbuddy":
            # workbuddy2api 的 /v1/models 与 qoder 同源但字段名略异：
            # supports_tool_call（单数）、credits 直接给 "x0.21"，没有 price_factor。
            name = str(m.get("name") or mid)
            vision = bool(m.get("supports_images") or m.get("supports_vision"))
            tools = bool(m.get("supports_tool_call") or m.get("supports_tool_calls"))
            reasoning = bool(m.get("supports_reasoning")) or bool(efforts)
            vendor = str(m.get("vendor") or m.get("owned_by") or "")
            desc = str(m.get("description") or "")
            factor = None
            credits_override = str(m.get("credits") or "")
            series = _series(mid)
        elif platform == "qoder":
            name = str(m.get("name") or mid)
            vision = bool(m.get("supports_vision") or caps.get("vision"))
            tools = bool(m.get("supports_tool_calls") or caps.get("tool_calls"))
            reasoning = bool(m.get("supports_reasoning") or caps.get("reasoning")) or bool(efforts)
            vendor = str(m.get("owned_by") or "")
            desc = str(m.get("description") or "")
            factor = m.get("price_factor")
            series = _series(mid)
        else:
            name = str(m.get("display") or m.get("name") or mid)
            vision = bool(m.get("supports_image"))
            tools = bool(m.get("supports_tool_calls"))
            reasoning = bool(m.get("supports_reasoning")) or bool(efforts)
            vendor = str(m.get("vendor") or "")
            desc = ""
            factor = m.get("rate")
            series = vendor or _series(mid)
        credits = credits_override or (f"x{factor}" if factor not in (None, "") else "")
        out.append({
            "id": mid,
            "name": name,
            "context_length": _ctx_best(m),
            "context_window_label": _ctx_label(m),
            "context_windows": [_i(x) for x in (m.get("context_windows") or [])],
            "context_window_default": str(m.get("context_window_default") or ""),
            "max_output_tokens": _i(m.get("max_tokens") or m.get("max_output_tokens")),
            "vision": vision,
            "tool_calls": tools,
            "reasoning": reasoning,
            "efforts": efforts,
            "vendor": vendor,
            "description": desc,
            "credits": credits,
            "series": series,
        })
    return out



async def _load_models(rt) -> Dict[str, Any]:
    """取两个平台的上游模型（单项失败只记原因，不抛）。"""
    result: Dict[str, Any] = {"qoder": [], "trae": [], "workbuddy": [], "errors": []}
    try:
        result["qoder"] = _norm_models(await rt.qoder.models(), "qoder")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"qoder 模型接口失败：{str(exc)[:160]}")
    try:
        result["trae"] = _norm_models(await rt.trae.models(), "trae")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"trae 模型接口失败：{str(exc)[:160]}")
    w = getattr(rt, "wb", None)
    if w is not None:
        try:
            result["workbuddy"] = _norm_models(await w.models(), "workbuddy")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"workbuddy 模型接口失败：{str(exc)[:160]}")
    return result


# ------------------------------------------------------------------ 上游 / 状态
@router.get("/upstreams")
async def fixed_upstreams(request: Request) -> Dict[str, Any]:
    """多上游列表：必须带 `auth_dir`——dashboard 用「有没有账号目录」筛分组，
    缺这个字段会让首页永远显示 0 个账号。"""
    from pathlib import Path

    rt = _rt(request)
    agg = await build_aggregate(rt)
    q, t = agg.qoder_accounts, agg.trae_accounts

    def row(idx: int, name: str, accounts, is_default: bool, base: str,
            auth_dir: str, container: str) -> Dict[str, Any]:
        return {
            "id": idx, "name": name, "base_url": base,
            "has_key": True, "api_key_masked": "已配置（/opt/qtg/secrets）",
            "note": "", "enabled": True, "is_default": is_default,
            "auth_dir": auth_dir, "container": container, "bound_keys": 0,
            "accounts": len(accounts),
            "healthy": len([a for a in accounts if a.state == "ready"]),
            "credits": round(sum(float(a.credits_remain or 0) for a in accounts), 2),
            "created_at": None, "updated_at": None,
        }

    w = list(agg.wb_accounts)
    all_items = [
        row(1, "Qoder", q, True, rt.settings.qoder_base,
            str(Path(rt.settings.qoder_data_dir) / "accounts"), "qoder-proxy"),
        row(2, "Trae", t, False, rt.settings.trae_base,
            str(Path(rt.settings.trae_data_dir) / "accounts"), "aiwork-server"),
        row(3, "WorkBuddy", w, False, rt.settings.wb_api_base,
            str(rt.settings.wb_auths_dir), "workbuddy2api"),
    ]
    plat = _platform(request)
    want = {"qoder": 1, "trae": 2, "workbuddy": 3}.get(plat, 1)
    items = [it for it in all_items if it["id"] == want]
    host = (request.headers.get("host") or "").split(":")[0]
    proto = request.headers.get("x-forwarded-proto") or "http"
    _seg = {"qoder": "/qoder/v1", "trae": "/trae/v1", "workbuddy": "/v1"}.get(plat, "/v1")
    public = f"{proto}://{host}{_seg}" if host else ""
    for it in items:
        it["public_url"] = public
    return {"items": items, "total": len(items)}


UPSTREAM_IDS = {1: "qoder", 2: "trae", 3: "workbuddy"}
UPSTREAM_NAMES = {1: "Qoder", 2: "Trae", 3: "WorkBuddy"}


def _rows_of(agg, upstream_id: Optional[int]):
    """按 `upstream_id` 取平台账号行；None = 三个平台全给。

    前端 dashboard 是逐组扇出的（每组带自己的 upstream_id），
    这里少一个分支就会让那一组永远空着。
    """
    if upstream_id == 1:
        return list(agg.qoder_accounts)
    if upstream_id == 2:
        return list(agg.trae_accounts)
    if upstream_id == 3:
        return list(agg.wb_accounts)
    return all_accounts(agg)


def _upstream_name(upstream_id: Optional[int]) -> str:
    return UPSTREAM_NAMES.get(upstream_id, "Qoder / Trae / WorkBuddy")


_PLAT_TO_UPSTREAM = {"qoder": 1, "trae": 2, "workbuddy": 3}


def _resolve_upstream_id(request: Request, upstream_id: Optional[int]) -> Optional[int]:
    """把「前端平台切换」翻译成 upstream_id 分组。

    规则：
      * 显式传了 `?upstream_id=N` → 以参数为准（dashboard 逐组扇出）；
      * 没传但带了 `X-QTG-Platform` → 取该平台对应的分组；
      * 都没有 → None（全量，dashboard 汇总用）。
    """
    if upstream_id is not None:
        return upstream_id
    raw = (request.headers.get("x-qtg-platform") or "").strip().lower()
    return _PLAT_TO_UPSTREAM.get(raw)


_CAP_MATRIX = {
    "qoder": {"readonly": False, "checkin": True, "refresh_account": True,
              "set_account": True, "add_account": True, "delete_account": False,
              "save_keys": True, "oauth": True, "agents": True, "alerts": True,
              "note": ""},
    "trae": {"readonly": False, "checkin": True, "refresh_account": True,
             "set_account": True, "add_account": True, "delete_account": False,
             "save_keys": False, "oauth": True, "agents": False, "alerts": False,
             "note": "API Key \u4e0e\u5ba2\u6237\u7aef\u914d\u7f6e\u5728 aiwork \u81ea\u5df1\u90a3\u8fb9\u7ef4\u62a4"},
    "workbuddy": {"readonly": True, "checkin": False, "refresh_account": False,
                  "set_account": False, "add_account": False, "delete_account": False,
                  "save_keys": False, "oauth": False, "agents": False, "alerts": True,
                  "note": "\u7531 workbuddy \u539f\u9762\u677f\u7ba1\u7406\uff1bqtg \u53ea\u8bfb\u5408\u5e76"},
}
_NAME = {"qoder": "Qoder", "trae": "Trae", "workbuddy": "WorkBuddy"}
_UPID = {"qoder": 1, "trae": 2, "workbuddy": 3}


@router.get("/platforms")
async def platform_capabilities(request: Request) -> Dict[str, Any]:
    """三平台能力矩阵（前端据此决定按钮可用性）。"""
    rt = _rt(request)
    items = []
    for pid in PLATFORMS:
        cap = dict(_CAP_MATRIX.get(pid) or {})
        readonly = bool(cap.pop("readonly", False))
        note = str(cap.pop("note", ""))
        if pid == "workbuddy" and getattr(rt, "wb", None) is None:
            note = "workbuddy \u5e73\u53f0\u5f53\u524d\u672a\u542f\u7528\uff08QTG_WB_ENGINE=off\uff09"
        items.append({"id": pid, "name": _NAME.get(pid, pid),
                      "upstream_id": _UPID.get(pid, 0), "readonly": readonly,
                      "capabilities": cap, "note": note})
    return {"platforms": items, "total": len(items)}


@router.get("/accounts")
async def fixed_accounts(request: Request, upstream_id: Optional[int] = None) -> Dict[str, Any]:
    """账号列表：支持 `upstream_id` 分组过滤（1=Qoder，2=Trae）。

    它是 dashboard 逐组扇出的关键——不按分组过滤的话，每一组都会返回全部账号，
    首页会把同一个账号算两遍。
    """
    from .compat import _map_account

    rt = _rt(request)
    agg = await build_aggregate(rt)
    upstream_id = _resolve_upstream_id(request, upstream_id)
    rows = _rows_of(agg, upstream_id)
    accounts = []
    for a in rows:
        item = _map_account(rt, a)
        item["ttl_seconds"] = _ttl_seconds(rt, a)
        accounts.append(item)
    up = all(h.up for h in agg.health) if agg.health else False
    return {
        "total": len(accounts),
        "accounts": accounts,
        "pool_synced": len(accounts),
        "pool_available": up,
        "upstream": {"id": upstream_id, "name": _upstream_name(upstream_id),
                     "is_default": upstream_id in (None, 1)},
        "accounts_dir": rt.settings.qoder_data_dir,
    }


@router.get("/status")
async def fixed_status(request: Request, upstream_id: Optional[int] = None) -> Dict[str, Any]:
    """池状态（`statusApi` 用）。结构对齐 `UpstreamStatus`。"""
    rt = _rt(request)
    agg = await build_aggregate(rt)
    rows = _rows_of(agg, upstream_id)
    by_realm: Dict[str, Dict[str, int]] = {}
    for a in rows:
        realm = "global" if str(a.realm).lower() in ("intl", "global") else "cn"
        slot = by_realm.setdefault(realm, {"total": 0, "healthy": 0, "cooling": 0, "disabled": 0})
        slot["total"] += 1
        if a.state == "ready":
            slot["healthy"] += 1
        elif a.state == "cooling":
            slot["cooling"] += 1
        elif a.state in ("disabled", "expired"):
            slot["disabled"] += 1
    return {
        "connected": all(h.up for h in agg.health) if agg.health else False,
        "accounts": [{"uid": a.uid, "platform": a.platform, "state": a.state,
                      "healthy": a.state == "ready", "cooling": a.state == "cooling",
                      "disabled": a.state in ("disabled", "expired")} for a in rows],
        "total": len(rows),
        "healthy": len([a for a in rows if a.state == "ready"]),
        "cooling": len([a for a in rows if a.state == "cooling"]),
        "disabled": len([a for a in rows if a.state in ("disabled", "expired")]),
        "realm_totals": by_realm,
        "error": "",
    }


# ------------------------------------------------------------------ 模型（真数据）
@router.get("/models")
async def fixed_models(request: Request, realm: str = Query(default="")) -> Dict[str, Any]:
    rt = _rt(request)
    plat = _platform(request)
    if realm in ("cn", "intl"):
        # 该版本下没有账号时不给模型列表：「有这些模型可用」是假象——
        # 上游 /v1/models 是按账号池整体返回的，不分版本。
        try:
            _agg = await build_aggregate(rt)
        except Exception:  # noqa: BLE001
            _agg = None
        if _agg is not None:
            _rows = {"qoder": _agg.qoder_accounts, "trae": _agg.trae_accounts,
                     "workbuddy": _agg.wb_accounts}.get(plat, [])
            _ok = False
            for _a in _rows:
                _got = str(getattr(_a, "realm", "") or "").strip().lower()
                if _got == realm or (not _got and realm == "cn"):
                    _ok = True
                    break
            if not _ok:
                return {"models": [], "source": "none", "count": 0,
                        "note": "该版本下没有账号"}

    data = await _load_models(rt)
    models = [
        {"id": r["id"], "name": r["name"],
         "context_length": r["context_length"],
         "max_output_tokens": r["max_output_tokens"] or None,
         "owned_by": plat}
        for r in data[plat]
    ]
    return {"models": models, "source": "dynamic" if models else "static", "count": len(models)}


def _source_label(plat: str) -> str:
    """模型目录的来源说明。三平台的数据来源不一样，别再用统一的「bff 透传」。"""
    return {
        "qoder": "qoder2api 引擎的模型接口（含多档上下文窗口与错峰价）",
        "trae": "aiwork-server 的 /v1/models（含最大输出与推理档位）",
        "workbuddy": "workbuddy2api 的 /v1/models（含最大输出与积分倍率）",
    }.get(plat, f"{plat} 平台模型")


@router.get("/model-catalog")
async def fixed_model_catalog(request: Request, realm: str = Query(default=""),
                              force: str = Query(default="false")) -> Dict[str, Any]:
    """模型目录：用上游真实元数据填满上下文 / 最大输出 / 能力标签。

    `source` 用 `tencent`：qoder-bff / trae-bff 的模型数据本身就是腾讯接口的透传，
    字段完整；标成 `upstream` 会让界面显示「未取到、只是简表」的误导提示。
    """
    rt = _rt(request)
    plat = _platform(request)
    if realm in ("cn", "intl"):
        # 该版本下没有账号时不该给出「这些模型可用」的假象：
        # Qoder / Trae 的 /v1/models 是按账号池整体返回的，不分版本。
        try:
            _agg = await build_aggregate(rt)
        except Exception:  # noqa: BLE001
            _agg = None
        if _agg is not None:
            _rows = {"qoder": _agg.qoder_accounts, "trae": _agg.trae_accounts,
                     "workbuddy": _agg.wb_accounts}.get(plat, [])
            _ok = False
            for _a in _rows:
                _got = str(getattr(_a, "realm", "") or "").strip().lower()
                if _got == realm or (not _got and realm == "cn"):
                    _ok = True
                    break
            if not _ok:
                return {
                    "models": [], "source": "none",
                    "source_label": "该版本下没有账号",
                    "via": plat, "errors": [], "cached": False, "cache_age": 0,
                    "summary": {"total": 0, "reasoning": 0, "large_context": 0,
                                "max_context": 0, "series": [], "unique_ids": 0},
                }

    data = await _load_models(rt)
    models: List[Dict[str, Any]] = []
    for i, r in enumerate(data[plat]):
        models.append({
            "id": r["id"],
            "name": r["name"],
            "context_length": r["context_length"],
            "context_window_label": r.get("context_window_label") or "",
            "context_windows": r.get("context_windows") or [],
            "context_window_default": r.get("context_window_default") or "",
            "max_output_tokens": r["max_output_tokens"],
            "efforts": r["efforts"],
            "description": r["description"],
            "credits": r["credits"],
            "vendor": r["vendor"] or plat,
            "tags": [k for k, on in (("vision", r["vision"]), ("tool_calls", r["tool_calls"]),
                                    ("reasoning", r["reasoning"])) if on],
            "is_default": i == 0,
            "supports_reasoning": r["reasoning"],
            "supports_tool_call": r["tool_calls"],
            "only_reasoning": False,
            "reasoning_summary": "",
            "default_effort": "",
            "supports_images": bool(r["vision"]),
            "native_modality": "multimodal" if r["vision"] else "text",
            "native_modality_source": "upstream",
            "series": r["series"],
            "platform": plat,
        })
    series = sorted({m["series"] for m in models})
    summary = {
        "total": len(models),
        "reasoning": len([m for m in models if m["supports_reasoning"]]),
        "large_context": len([m for m in models if m["context_length"] >= 131072]),
        "max_context": max([m["context_length"] for m in models], default=0),
        "series": series,
        "unique_ids": len({m["id"] for m in models}),
    }
    return {
        "models": models,
        "source": "tencent" if models else "none",
        "source_label": (_source_label(plat) if models else "上游模型接口未取到"),
        "via": plat,
        "errors": data["errors"],
        "cached": False,
        "cache_age": 0,
        "summary": summary,
    }


@router.get("/playground/models")
async def fixed_playground_models(request: Request, realm: str = Query(default="")) -> Dict[str, Any]:
    rt = _rt(request)
    plat = _platform(request)
    data = await _load_models(rt)
    models = [{"id": r["id"], "name": r["name"], "efforts": r.get("efforts") or [],
               "series": r["series"]}
              for r in data[plat]]
    return {
        "models": models,
        "source": "tencent" if models else "none",
        "source_label": (f"{plat} 平台模型（经 bff 透传）" if models else "上游模型接口未取到"),
    }


# ------------------------------------------------------------------ 账号 TTL（绿条比例）
def _ttl_seconds(rt, a) -> int:
    """Token 总有效期：绿条按 remain/ttl 算，缺 ttl 时前端按 90 天兜底，
    30 天的 Token 会显示成 1/3 满——这是「绿条不满」的直接原因。"""
    import json as _json
    from pathlib import Path

    if a.platform == "qoder":
        try:
            p = Path(rt.settings.qoder_data_dir) / "accounts" / f"{a.uid}.json"
            data = _json.loads(p.read_text(encoding="utf-8"))
            added = float(data.get("addedAt") or 0)
            if added and a.expires_at and float(a.expires_at) > added:
                return int(float(a.expires_at) - added)
        except Exception:  # noqa: BLE001
            pass
    return 30 * 86400


# ------------------------------------------------------------------ 密钥（平台隔离）
async def _wb_keys(rt) -> List[Dict[str, Any]]:
    """workbuddy 自己的 API Key（manager.db 的 api_keys 表）。

    它和 qtg 的 `sk-qtg-*` 是两套体系：qtg 的密钥走自己的 store，
    workbuddy 的密钥由 workbuddy2api 校验。三合一后按平台各自展示，
    并标 `readonly=True` —— qtg 不接管它的增删改。
    """
    w = getattr(rt, "wb", None)
    if w is None:
        return []
    try:
        data = await w.keys()
    except Exception:  # noqa: BLE001
        return []
    rows = ((data or {}).get("data") or {}).get("keys") or []
    out: List[Dict[str, Any]] = []
    for i, k in enumerate(rows):
        out.append({
            "id": k.get("id") or (i + 1),
            "seq": i + 1,
            "name": k.get("name") or "",
            "prefix": str(k.get("prefix") or "")[:12],
            "enabled": bool(k.get("enabled")),
            "expires_at": k.get("expires_at"),
            "max_ips": 0,
            "ip_allowlist": [],
            "models": k.get("models") or [],
            "realm": k.get("realm") or "",
            "created_at": k.get("created_at") or "",
            "daily_limit": 0,
            "monthly_limit": 0,
            "max_concurrency": 0,
            "quota": k.get("quota") or 0,
            "used_tokens": k.get("used_tokens") or 0,
            "quota_credit": k.get("quota_credit") or 0,
            "used_credit": k.get("used_credit") or 0,
            "last_used_at": k.get("last_used_at"),
            "platforms": ["workbuddy"],
            "source": "workbuddy",
            "readonly": True,
        })
    return out


@router.get("/keys")
async def fixed_keys(request: Request) -> List[Dict[str, Any]]:
    """密钥列表统一来自 Manager（K01-S2）。

    以前 qoder/trae 读 QTG 本地 `qtg_keys`、workbuddy 直读 Manager DB，两边
    各有一套 id 与字段。迁移后 Manager 是唯一权威，这里只做转发；
    Manager 不可达时抛错（前端显示错误），不返回空列表冒充"没有密钥"。
    """
    rt = _rt(request)
    keys = await manager_api.list_keys(
        rt.settings, request_id=manager_api.manager_request_id(request)
    )
    out: List[Dict[str, Any]] = []
    for k in keys:
        if not isinstance(k, dict):
            continue
        out.append({
            "id": k.get("id"),
            "name": k.get("name") or "",
            "prefix": k.get("prefix") or "",
            "enabled": bool(k.get("enabled")),
            "expires_at": k.get("expires_at") or None,
            "max_ips": k.get("max_ips") or 0,
            "ip_allowlist": k.get("ip_allowlist") or [],
            "models": k.get("models") or [],
            "realm": k.get("realm") or "",
            "quota": k.get("quota"),
            "used_tokens": k.get("used_tokens") or 0,
            "quota_credit": k.get("quota_credit") or 0,
            "used_credit": k.get("used_credit") or 0,
            "upstream_id": k.get("upstream_id"),
            "created_at": k.get("created_at"),
            "last_used_at": k.get("last_used_at"),
            "platforms": k.get("platforms") if isinstance(k.get("platforms"), list) else [],
            "source": "manager",
        })
    return out


@router.post("/keys")
async def fixed_keys_create(request: Request) -> Dict[str, Any]:
    """在 Manager 上创建密钥（K01-S2）。

    QTG 不再写自己的 `qtg_keys`：字段直接按 Manager 契约发送，
    前端此前被静默丢弃的 quota / quota_credit / realm / upstream_id 现在全部生效。
    Manager 不可用或未配置服务凭据时**明确报错**，不返回假 key。
    """
    from .compat import _body

    rt = _rt(request)
    plat = _platform(request)
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        # 与 Manager 的创建校验对齐（K29/N21）：空名称在入口拒绝，
        # 不再默认"未命名"——那会绕过 Manager 的必填校验造出无名列。
        return JSONResponse(status_code=422, content={"detail": "名称不能为空"})
    payload = {
        "name": name,
        "models": [str(m).strip() for m in (body.get("models") or []) if str(m).strip()],
        "ip_allowlist": [str(x).strip() for x in (body.get("ip_allowlist") or []) if str(x).strip()],
        "max_ips": int(body.get("max_ips") or 0),
        "quota": int(body.get("quota") or 0),
        "quota_credit": float(body.get("quota_credit") or 0),
        "platforms": body.get("platforms", [plat] if plat in ("workbuddy", "qoder", "trae")
                              else ["workbuddy", "qoder", "trae"]),
        "realm": str(body.get("realm") or ""),
        "upstream_id": body.get("upstream_id"),
        "expires_at": _to_epoch(body.get("expires_at")),
    }
    created = await manager_api.create_key(
        rt.settings, payload, request_id=manager_api.manager_request_id(request)
    )
    rt.record_audit(
        action="compat_key_create",
        actor="compat",
        result="ok",
        detail={
            "key_id": created.get("id"),
            "name": created.get("name"),
            "platform": plat,
            "via": "manager",
        },
    )
    out = dict(created)
    out.setdefault("platforms", [plat])
    out["source"] = "manager"
    return out


# ------------------------------------------------------------------ 本机导入 / 导出配置
@router.get("/keys/import-local/status")
async def fixed_import_status(request: Request) -> Dict[str, Any]:
    """本机一键导入（写客户端配置）：本部署不提供。

    但**结构必须完整**：前端 `importStatus?.clients.find(...)` 对 `clients`
    没有可选链，返回 `{}` 会直接抛 TypeError —— 这正是「创建密钥出错」的原因。
    """
    return {"enabled": False, "local_caller": False, "clients": []}


@router.get("/keys/import-local/detect")
async def fixed_import_detect(request: Request, client: str = Query(default="ccswitch")) -> Dict[str, Any]:
    return {"client": client if client in ("ccswitch", "zcode") else "ccswitch",
            "found": False, "exe": None, "source": ""}


@router.post("/keys/import-local")
async def fixed_import_local(request: Request) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": "本部署不提供「写入本机客户端配置」：控制面跑在服务器上，碰不到你电脑里的客户端" })


@router.post("/keys/export")
async def fixed_keys_export(request: Request) -> Dict[str, Any]:
    """导出客户端配置片段（cc-switch / ZCode）。

    面板只存密钥哈希、拿不回明文，所以这里只生成「结构」：
    调用方需自己把刚创建的明文 token 贴进客户端。
    """
    from .compat import _body

    rt = _rt(request)
    body = await _body(request)
    plat = _platform(request)
    data = await _load_models(rt)
    host = (request.headers.get("host") or "qoder.example.com").split(":")[0]
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
    return {
        "client": str(body.get("client") or "ccswitch"),
        "base_url": f"{scheme}://{host}" + {"qoder": "/qoder/v1", "trae": "/trae/v1"}.get(plat, "/v1"),
        "realm": str(body.get("realm") or "cn"),
        "models": [r["id"] for r in data[plat]],
        "name": str(body.get("providerName") or f"qtg-{plat}"),
        "app_type": str(body.get("app") or "claude"),
    }


# ------------------------------------------------------------------ 任务领取计划 / 红包
@router.get("/task-claim-schedule")
async def compat_task_claim_schedule() -> Dict[str, Any]:
    """「自动领奖时间」：本部署没有任务领奖调度器（签到由上游负责），如实返回未启用。"""
    return {"enabled": False, "hours": []}


@router.put("/task-claim-schedule")
async def compat_task_claim_schedule_save(request: Request) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": "本部署没有任务领奖调度器：签到/保活由 Qoder、Trae 各自的调度器负责"})


def _gone(what: str) -> JSONResponse:
    """410：本部署**没有**这个能力（不是"没数据"、也不是"没权限"）。

    K29 信封约定：`available=false` + `code=unsupported` + 可读 `detail`。
    只给 `detail` 时前端容易把它塞进通用错误提示，用户分不清"功能不存在"。
    """
    return JSONResponse(status_code=410, content={
        "available": False,
        "code": "unsupported",
        "detail": f"本部署没有{what}：那是 WorkBuddy 的营销功能，qtg 不实现",
    })



@router.get("/red-packets")
async def compat_red_packets_list(request: Request) -> JSONResponse:
    return _gone("红包 / 抽奖")


@router.get("/red-packets/{rid}")
async def compat_red_packet_detail(request: Request, rid: str) -> JSONResponse:
    return _gone("红包 / 抽奖")


@router.post("/red-packets")
async def compat_red_packet_create(request: Request) -> JSONResponse:
    return _gone("红包 / 抽奖")


@router.post("/red-packets/{rid}/revoke")
async def compat_red_packet_revoke(request: Request, rid: str) -> JSONResponse:
    return _gone("红包 / 抽奖")


@router.get("/claim/{code}")
async def compat_claim_get(request: Request, code: str) -> JSONResponse:
    return _gone("红包 / 抽奖")


@router.post("/claim/{code}")
async def compat_claim_draw(request: Request, code: str) -> JSONResponse:
    return _gone("红包 / 抽奖")


# ------------------------------------------------------------------ 告警中心
@router.get("/alerts")
async def fixed_alerts(request: Request, realm: str = Query(default="")) -> Dict[str, Any]:
    """告警中心数据源。

    以前 `/api/alerts` 没实现（走兼容层兜底返回 `{}`），前端只能显示空。这里把四类
    **真实信号**聚起来：

      1. 上游风控（qoder2 的 `/health.risk`：排队 / 限流 / 拒绝 / 版本门）
      2. 上游不可达（`/readyz` 的 health 结果）
      3. 账号类：3 天内到期 / 余额低于 50 / 冷却·禁用·过期
      4. 密钥 7 天内过期

    `realm` 非空时只过滤**账号类**告警（国内版 cn / 国际版 intl）；风控与上游类是
    全局信号，不随版本变化。
    """
    rt = _rt(request)
    plat = _platform(request)
    rows: List[Dict[str, Any]] = []
    agg = await build_aggregate(rt)
    cst = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime.now(cst).timestamp()

    # 1) 上游风控
    adapter = rt.qoder if plat == "qoder" else (rt.wb if plat == "workbuddy" else rt.trae)
    try:
        raw = await adapter.alerts()
        for a in (raw or {}).get("alerts") or []:
            if isinstance(a, dict):
                rows.append({**a, "kind": "risk"})
    except Exception:  # noqa: BLE001
        pass

    # 2) 上游不可达
    for h in agg.health:
        if not h.up:
            rows.append({
                "id": f"upstream-{h.name}", "time": "", "level": "error",
                "title": f"上游 {h.name} 不可达", "message": h.error or "健康检查失败",
                "ack": False, "kind": "upstream",
            })

    # 3) 账号类
    accounts = agg.qoder_accounts if plat == "qoder" else (agg.wb_accounts if plat == "workbuddy" else agg.trae_accounts)
    for a in accounts:
        if realm and plat == "qoder" and str(a.realm) != realm:
            continue
        tag = f"{a.uid[:8]}"
        if a.days_left is not None and 0 <= a.days_left <= 3:
            rows.append({
                "id": f"expiring-{a.uid}", "time": "", "level": "warn",
                "title": f"账号「{a.name}」{a.days_left} 天后到期",
                "message": f"积分 {a.credits_remain:g}，到期后需重新授权", "ack": False,
                "kind": "account", "target": tag,
            })
        if a.credits_remain < 50:
            rows.append({
                "id": f"lowcredits-{a.uid}", "time": "", "level": "warn",
                "title": f"账号「{a.name}」余额偏低",
                "message": f"剩余积分 {a.credits_remain:g}", "ack": False,
                "kind": "account", "target": tag,
            })
        if a.state in ("cooling", "disabled", "expired"):
            rows.append({
                "id": f"state-{a.uid}", "time": "", "level": "error" if a.state != "cooling" else "warn",
                "title": f"账号「{a.name}」状态异常：{a.state}",
                "message": (a.note or a.cooldown_until or "")[:160], "ack": False,
                "kind": "account", "target": tag,
            })

    # 4) 密钥即将过期
    for k in rt.store.list_keys():
        if plat not in (k.get("platforms") or list(PLATFORMS)):
            continue
        exp = int(k.get("expires_at") or 0)
        if exp and 0 < exp - now < 7 * 86400:
            days = int((exp - now) // 86400)
            rows.append({
                "id": f"keyexp-{k.get('id')}", "time": "", "level": "warn",
                "title": f"密钥「{k.get('name') or ''}」{days} 天后过期",
                "message": f"前缀 {k.get('masked') or ''}", "ack": False,
                "kind": "key",
            })

    level_rank = {"error": 0, "warn": 1, "info": 2}
    rows.sort(key=lambda r: level_rank.get(str(r.get("level")), 3))
    return {
        "alerts": rows,
        "unread": len([r for r in rows if not r.get("ack")]),
        "platform": plat,
        "realm": realm or "all",
    }


@router.post("/alerts/ack")
async def fixed_alerts_ack(request: Request) -> Dict[str, Any]:
    """标记已读：qtg 的告警是**实时派生**的（不是存库的事件），所以没有可持久化的
    已读状态；这里如实说明，而不是假装成功。"""
    return {
        "ok": False,
        "available": False,
        "detail": "qtg 的告警由实时状态派生，没有已读状态可保存；告警消失即代表问题已恢复",
    }


# ------------------------------------------------------------------ 智能体配置
@router.get("/agents")
async def fixed_agents(request: Request) -> Dict[str, Any]:
    """本机客户端一键配置（Claude Code / Codex / Crush / DSH…）。

    数据来自 qoder2 引擎的 `/agents`（只有它实现了本机客户端探测）；trae 平台
    没有等价能力，如实返回空并说明。
    """
    rt = _rt(request)
    plat = _platform(request)
    if plat != "qoder":
        return {
            "clients": [], "available": False,
            "note": "trae 引擎（aiwork-server）没有本机客户端配置能力；请用 qoder 平台或手动配置",
        }
    q = getattr(rt, "qoder", None)
    if not hasattr(q, "agents"):
        return {"clients": [], "available": False, "note": "当前 qoder 引擎不支持智能体配置"}
    try:
        data = await q.agents()
    except Exception as exc:  # noqa: BLE001
        return {"clients": [], "available": False, "note": f"读取失败：{str(exc)[:160]}"}
    clients = data.get("clients") or []
    return {"clients": clients, "available": True,
            "note": "" if clients else "上游没有返回可配置的客户端"}


@router.post("/agents/{cid}/apply")
async def fixed_agent_apply(request: Request, cid: str) -> Dict[str, Any]:
    rt = _rt(request)
    if _platform(request) != "qoder":
        return {"ok": False, "detail": "trae 平台不支持"}
    q = getattr(rt, "qoder", None)
    if not hasattr(q, "agents_apply"):
        return {"ok": False, "detail": "当前引擎不支持"}
    res = await q.agents_apply(cid)
    rt.record_audit(action="agent_apply", actor="compat", result="ok" if res.get("ok") else "error",
                    detail={"client": cid})
    return res


@router.post("/agents/{cid}/restore")
async def fixed_agent_restore(request: Request, cid: str) -> Dict[str, Any]:
    rt = _rt(request)
    if _platform(request) != "qoder":
        return {"ok": False, "detail": "trae 平台不支持"}
    q = getattr(rt, "qoder", None)
    if not hasattr(q, "agents_restore"):
        return {"ok": False, "detail": "当前引擎不支持"}
    res = await q.agents_restore(cid)
    rt.record_audit(action="agent_restore", actor="compat", result="ok" if res.get("ok") else "error",
                    detail={"client": cid})
    return res
