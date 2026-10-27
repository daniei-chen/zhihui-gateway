"""WorkBuddy 设置页兼容层（/api/settings/*、/api/system/*、/api/users、/api/tokens、/api/proxy-*）。

qtg 不是 WorkBuddy Manager：这些设置项大多没有对应实现。本模块的原则：

* **读**：返回结构完整、内容真实的空值/默认值，页面能正常渲染、不崩；
* **写**：如实返回 409 + 具体原因，绝不假装成功。

注册顺序必须早于 `compat.py`——它的 `GET /{path:path}` 兜底会吃掉这里的 GET。
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["compat-settings"])


def _rt(request: Request):
    return request.app.state.rt


def _no(what: str, why: str = "") -> JSONResponse:
    detail = f"本部署不支持{what}"
    if why:
        detail += f"：{why}"
    return JSONResponse(status_code=409, content={"detail": detail})


def _pool() -> Dict[str, Any]:
    return {
        "enabled": False,
        "strategy": "priority",
        "interval_seconds": 300,
        "strategies": ["priority", "round_robin", "random", "least_connections"],
        "routes_total": 0,
        "routes_enabled": 0,
    }


# ------------------------------------------------------------------ 用户 / 会话








# ------------------------------------------------------------------ 管理面令牌








# ------------------------------------------------------------------ 代理池 / 线路


























# ------------------------------------------------------------------ 上游配置























# ------------------------------------------------------------------ PostgreSQL 同步
def _pg_config() -> Dict[str, Any]:
    return {"enabled": False, "host": "", "port": 5432, "dbname": "", "user": "",
            "password": "", "sslmode": "prefer", "interval_minutes": 0,
            "keep_local_backup": True, "last_export_at": 0, "last_import_at": 0}


def _pg_status() -> Dict[str, Any]:
    return {"running": False, "kind": "", "ok": None, "step": "", "percent": 0,
            "tables_total": 0, "tables_done": 0, "rows": 0, "logs": [],
            "started_at": 0, "finished_at": 0}














# ------------------------------------------------------------------ 系统更新
















# ------------------------------------------------------------------ 多上游（账号池分组）
@router.post("/upstreams")
async def compat_upstreams_create(request: Request) -> JSONResponse:
    return _no("新增上游账号池", "本部署固定两个上游（qoder-bff / trae-bff）；"
                                 "多账号池方案见 /opt/qoder-hub/docs/multi-upstream.md")


@router.patch("/upstreams/{uid}")
async def compat_upstreams_update(request: Request, uid: int) -> JSONResponse:
    return _no("编辑上游", "上游地址与凭据在 docker 环境与 /opt/workbuddy-manager/engine/secrets/ 里，面板不提供在线编辑")


@router.delete("/upstreams/{uid}")
async def compat_upstreams_delete(request: Request, uid: int) -> JSONResponse:
    return _no("删除上游", "两个上游是部署的一部分（systemd / docker），不能从面板删除")


@router.post("/upstreams/{uid}/probe")
async def compat_upstreams_probe(request: Request, uid: int) -> Dict[str, Any]:
    """探测上游可达性：直接问对应适配器的 /healthz。"""
    rt = _rt(request)
    name = {1: "Qoder", 2: "Trae"}.get(int(uid))
    if not name:
        return JSONResponse(status_code=404, content={"detail": "没有这个上游"})
    adapter = rt.qoder if int(uid) == 1 else rt.trae
    try:
        data = await adapter.healthz()
        ok = bool(data.get("ok")) if isinstance(data, dict) else bool(data)
        return {"ok": ok, "message": "上游可达" if ok else f"上游返回异常：{str(data)[:160]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"探测失败：{str(exc)[:160]}"}


