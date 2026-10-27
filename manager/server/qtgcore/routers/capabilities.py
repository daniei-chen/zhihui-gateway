"""能力表端点（K29）。

`GET /api/v1/capabilities`：把 `capabilities.CAPABILITIES` 原样导出，并对每条
「不支持 / 暂不可用」给出 `error` 与 `evidence`。前端与验收脚本据此区分
「真的没有数据」与「本部署没有这个能力」。

设计约束：

* **默认不打上游**（`live=0`）：能力表是静态核对结果，探针/报表不该因为上游抖动
  而变成 502。需要实时来源状态时显式 `?live=1`，此时返回多来源状态数组，
  部分失败用 `partial` 表达（来源 / 时间 / 错误三者齐全）。
* 不在本端点做写操作、不写业务账本。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from .. import capabilities as caps
from ..identity import audit_actor, audit_detail, identity_of
from ..service import now_iso

router = APIRouter(prefix="/api/v1", tags=["capabilities"])


async def _live_sources(request: Request) -> list:
    """取三个平台适配器的健康状态，逐来源记录 来源/时间/错误。"""
    rt = request.app.state.rt
    out: list = []
    adapters = [("qoder", rt.qoder), ("trae", rt.trae)]
    if getattr(rt, "wb", None) is not None:
        adapters.append(("workbuddy", rt.wb))
    for name, adapter in adapters:
        started = now_iso()
        try:
            health = await adapter.health()
            out.append(caps.source_status(name, bool(health.get("up")), at=started,
                                          error=health.get("last_error", ""), detail=health))
        except Exception as exc:  # noqa: BLE001 - 单点失败不拖垮整表
            out.append(caps.source_status(name, False, at=started,
                                          error=f"{type(exc).__name__}: {exc}"[:300]))
    return out


@router.get("/capabilities")
async def capabilities(request: Request,
                       live: int = Query(default=0, ge=0, le=1,
                                         description="1=附带实时来源状态（会打上游）"),
                       state: str = Query(default="", description="只看某个能力状态")) -> dict:
    identity = identity_of(request)
    sources = await _live_sources(request) if live else []
    payload = caps.capabilities_payload(sources=sources)
    if live:
        # 部分失败语义：ok/partial/sources 三者一起给，不能只给 200
        report = caps.partial_report(payload["capabilities"], sources)
        payload["partial"] = report["partial"]
        payload["ok"] = report["ok"]
        payload["sources_ok"] = report["sources_ok"]
        payload["sources_total"] = report["sources_total"]
    if state:
        payload["capabilities"] = [c for c in payload["capabilities"] if c["state"] == state]
        payload["counts"]["rows"] = len(payload["capabilities"])
    payload["generated_at"] = now_iso()
    payload["requested_by"] = audit_actor(identity)
    payload["identity"] = audit_detail(identity)["identity"]
    return payload


@router.get("/capabilities/{cap_id}")
async def capability_detail(request: Request, cap_id: str) -> dict:
    index = caps.capability_index()
    cap = index.get(cap_id)
    if cap is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"没有这个能力: {cap_id}")
    identity = identity_of(request)
    return {
        "generated_at": now_iso(),
        "capability": cap.as_dict(),
        "requested_by": audit_actor(identity),
    }
