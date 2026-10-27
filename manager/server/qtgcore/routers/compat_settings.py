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
@router.get("/users")
async def compat_users() -> List[Dict[str, Any]]:
    """qtg 只有一个管理员身份（管理令牌），没有多用户表。

    返回真实存在的这一个身份而不是空数组——空数组在界面上等于「系统里一个
    账号都没有」，与本部署的实际情况不符。
    """
    return [{"username": "admin", "role": "admin"}]


@router.post("/users")
async def compat_users_create(request: Request) -> JSONResponse:
    return _no("多用户管理", "qtg 的身份只有管理令牌（/opt/workbuddy-manager/engine/secrets/admin_token），没有用户表")


@router.patch("/users/{username}")
async def compat_users_update(request: Request, username: str) -> JSONResponse:
    return _no("多用户管理", "qtg 的身份只有管理令牌，改密码请更换 /opt/workbuddy-manager/engine/secrets/admin_token")


@router.delete("/users/{username}")
async def compat_users_delete(request: Request, username: str) -> JSONResponse:
    return _no("多用户管理", "qtg 的身份只有管理令牌，没有可删除的用户")


@router.post("/sessions/revoke")
async def compat_sessions_revoke(request: Request) -> Dict[str, Any]:
    """吊销全部会话：把「吊销纪元」推到当前时间之后，此前签发的 cookie 全部失效。

    不需要改签名密钥（`/opt/workbuddy-manager/engine/secrets` 对控制面只读），也不需要重启服务；
    前端拿到 200 后会自行 logout 回登录页。
    """
    rt = _rt(request)
    epoch = rt.credentials.revoke_sessions()
    rt.record_audit(action="sessions_revoke", actor="compat", result="ok",
                    detail={"epoch": epoch})
    return {"ok": True, "message": "已吊销全部会话，请重新登录"}
# ------------------------------------------------------------------ 管理面令牌
@router.get("/tokens")
async def compat_tokens() -> List[Dict[str, Any]]:
    """qtg 没有「管理面 API Token」这一层：控制面只认管理令牌会话。

    返回空数组是真实的（列表本来就是空的），创建/修改会如实 409。
    """
    return []


@router.post("/tokens")
async def compat_tokens_create(request: Request) -> JSONResponse:
    return _no("管理面 API Token", "控制面只认管理令牌会话；给下游调用请用「API 密钥」页的 sk-qtg-*")


@router.patch("/tokens/{tid}")
async def compat_tokens_update(request: Request, tid: int) -> JSONResponse:
    return _no("管理面 API Token", "控制面只认管理令牌会话")


@router.delete("/tokens/{tid}")
async def compat_tokens_delete(request: Request, tid: int) -> JSONResponse:
    return _no("管理面 API Token", "控制面只认管理令牌会话")


# ------------------------------------------------------------------ 代理池 / 线路
@router.get("/proxies")
async def compat_proxies() -> Dict[str, Any]:
    return {"routes": [], "default": ""}


@router.get("/proxy-routes")
async def compat_proxy_routes() -> Dict[str, Any]:
    return {"routes": [], "default": "", "schemes": ["http", "https", "socks5", "socks5h"],
            "bound_total": 0, "pool": _pool()}


@router.get("/proxy-routes/bindings")
async def compat_proxy_bindings() -> Dict[str, Any]:
    return {"accounts": [], "routes": [], "pool": _pool()}


@router.post("/proxy-routes")
async def compat_proxy_routes_create(request: Request) -> JSONResponse:
    return _no("出口代理池", "qtg 数据面直连 qoder-bff / trae-bff，不管理出口线路")


@router.put("/proxy-routes/{name}")
async def compat_proxy_routes_update(request: Request, name: str) -> JSONResponse:
    return _no("出口代理池", "qtg 数据面直连上游，不管理出口线路")


@router.delete("/proxy-routes/{name}")
async def compat_proxy_routes_delete(request: Request, name: str) -> JSONResponse:
    return _no("出口代理池", "qtg 数据面直连上游，不管理出口线路")


@router.post("/proxy-routes/import")
async def compat_proxy_routes_import(request: Request) -> JSONResponse:
    return _no("出口代理池", "qtg 数据面直连上游，不管理出口线路")


@router.post("/proxy-routes/probe")
async def compat_proxy_routes_probe(request: Request) -> JSONResponse:
    return _no("出口代理池", "qtg 没有可探测的代理线路")


@router.get("/settings/proxy")
async def compat_proxy_settings() -> Dict[str, Any]:
    return {
        "http_proxy": "", "update_proxy": "",
        "http_configured": False, "update_configured": False,
        "env_http_proxy": "", "env_update_proxy": "",
        "effective": "", "source": "none",
        "probe_url": "", "global_proxy_enabled": False,
    }


@router.put("/settings/proxy")
async def compat_proxy_settings_save(request: Request) -> JSONResponse:
    return _no("全局出口代理", "qtg 数据面直连上游；如确需走代理，请用 systemd 的 HTTP_PROXY 环境变量")


@router.get("/settings/proxy-pool")
async def compat_proxy_pool() -> Dict[str, Any]:
    return _pool()


@router.put("/settings/proxy-pool")
async def compat_proxy_pool_save(request: Request) -> JSONResponse:
    return _no("出口代理池", "qtg 数据面直连上游，不管理出口线路")


@router.post("/settings/proxy/probe")
async def compat_proxy_probe(request: Request) -> JSONResponse:
    return _no("出口代理探测", "qtg 没有可探测的代理线路")


# ------------------------------------------------------------------ 上游配置
@router.get("/settings/upstream")
async def compat_settings_upstream(request: Request) -> Dict[str, Any]:
    """WorkBuddy Manager 的「上游配置」是它的 qoder-hub config.json 表单。

    qtg 的上游是两个 Go BFF（源码已丢失），配置由 docker 环境与
    `/opt/workbuddy-manager/engine/secrets/` 固定，面板没有可写的配置文件——如实返回
    `available:false`，前端会显示原因并禁用保存，而不是拿默认值冒充配置。
    """
    return {
        "available": False,
        "reload_hint": "",
        "config_path": "",
        "error": "本部署的上游是 qoder-bff / trae-bff（Go 二进制，源码已丢失），"
                 "配置由 docker 环境与 /opt/workbuddy-manager/engine/secrets/ 固定，面板不提供在线编辑。"
                 "可调项：API 密钥页（数据面密钥）、/api/v1/actions/limits（限额与并发）。",
    }


@router.post("/settings/upstream")
async def compat_settings_upstream_save(request: Request) -> JSONResponse:
    return _no("在线编辑上游配置", "上游是 Go BFF，配置由 docker 环境与 secrets 固定；请改 /opt/workbuddy-manager/engine/secrets/ 后重启容器")

@router.get("/settings/model-map")
async def compat_model_map(request: Request) -> Dict[str, Any]:
    """模型别名映射（真实读写 `<data_dir>/model_map.json`，数据面按它改写 model）。"""
    from ..model_map import load

    return load(_rt(request))


@router.post("/settings/model-map")
async def compat_model_map_save(request: Request) -> Dict[str, Any]:
    from ..model_map import save

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        return {"detail": "映射表必须是 {别名: 真实模型} 的对象"}
    return save(_rt(request), body)




@router.get("/upstream/reload-state")
async def compat_reload_state() -> Dict[str, Any]:
    return {"running": False, "pending": False, "last_at": 0, "last_ok": None,
            "last_message": "", "restart_count": 0}


@router.post("/settings/upstash/test")
async def compat_upstash_test(request: Request) -> Dict[str, Any]:
    return {"ok": False, "message": "本部署不使用 Upstash：qtg 的状态存在本机 SQLite（/opt/workbuddy-manager/engine/var/qtg.db）"}


@router.post("/settings/upstash/reload")
async def compat_upstash_reload(request: Request) -> Dict[str, Any]:
    return {"ok": False, "message": "本部署不接 Upstash，也没有可重载的上游配置；"
                                    "如需重启网关请用 systemctl restart qtg.service"}


@router.get("/settings/github-token")
async def compat_github_token() -> Dict[str, Any]:
    return {"configured": False, "masked": "", "source": ""}


@router.put("/settings/github-token")
async def compat_github_token_save(request: Request) -> JSONResponse:
    return _no("GitHub Token", "本部署不做在线版本检测与自动更新，不需要 GitHub 凭证")


@router.get("/settings/github-mirror")
async def compat_github_mirror() -> Dict[str, Any]:
    return {"mirror": "", "stored": "", "env_mirror": "", "source": "", "presets": []}


@router.put("/settings/github-mirror")
async def compat_github_mirror_save(request: Request) -> JSONResponse:
    return _no("GitHub 镜像", "本部署不做在线版本检测与自动更新")


# ------------------------------------------------------------------ PostgreSQL 同步
def _pg_config() -> Dict[str, Any]:
    return {"enabled": False, "host": "", "port": 5432, "dbname": "", "user": "",
            "password": "", "sslmode": "prefer", "interval_minutes": 0,
            "keep_local_backup": True, "last_export_at": 0, "last_import_at": 0}


def _pg_status() -> Dict[str, Any]:
    return {"running": False, "kind": "", "ok": None, "step": "", "percent": 0,
            "tables_total": 0, "tables_done": 0, "rows": 0, "logs": [],
            "started_at": 0, "finished_at": 0}


@router.get("/settings/pg-sync")
async def compat_pg_sync() -> Dict[str, Any]:
    return {"config": _pg_config(), "status": _pg_status()}


@router.get("/settings/pg-sync/status")
async def compat_pg_sync_status() -> Dict[str, Any]:
    return _pg_status()


@router.post("/settings/pg-sync")
async def compat_pg_sync_save(request: Request) -> JSONResponse:
    return _no("PostgreSQL 同步", "qtg 的状态存本机 SQLite；库级备份用 /api/v1/actions/backup 或直接拷 /opt/workbuddy-manager/engine/var/qtg.db")


@router.post("/settings/pg-sync/export")
async def compat_pg_sync_export(request: Request) -> JSONResponse:
    return _no("PostgreSQL 导出", "qtg 没有 PostgreSQL 连接；本机备份请拷 /opt/workbuddy-manager/engine/var/qtg.db")


@router.post("/settings/pg-sync/import")
async def compat_pg_sync_import(request: Request) -> JSONResponse:
    return _no("PostgreSQL 恢复", "qtg 没有 PostgreSQL 连接")


@router.post("/settings/pg-sync/test")
async def compat_pg_sync_test(request: Request) -> JSONResponse:
    return _no("PostgreSQL 连通性测试", "qtg 没有 PostgreSQL 连接")


# ------------------------------------------------------------------ 系统更新
@router.get("/system/update-status")
async def compat_update_status(request: Request) -> Dict[str, Any]:
    from .. import __version__

    rt = _rt(request)
    return {
        "available": False, "in_container": False, "can_update_upstream": False,
        "running": False, "ok": None, "step": "", "logs": [],
        "version": __version__, "updater_found": False,
        "upstream_dir": rt.settings.qoder_data_dir,
    }


@router.delete("/system/update-status")
async def compat_update_status_clear(request: Request) -> JSONResponse:
    return _no("在线更新", "本部署的升级方式是 rsync + systemctl restart（见 HANDOVER 第 7 节）")


@router.get("/system/check-update")
async def compat_check_update(request: Request) -> Dict[str, Any]:
    from .. import __version__

    err = "本部署不做在线版本检测（没有 CHANGELOG / updater），升级请按 HANDOVER 第 7 节手动部署"
    return {
        "checked_at": 0, "cached": False, "has_any": False,
        "manager": {"current": __version__, "latest": "", "has_update": False,
                    "error": err, "stale": False, "url": "", "assets_ready": None, "repo": ""},
        "upstream": {"current": "", "latest": "", "has_update": False, "error": err,
                     "date": "", "subject": "", "repo": ""},
    }


@router.post("/system/update")
async def compat_start_update(request: Request) -> JSONResponse:
    return _no("在线更新", "本部署的升级方式是 rsync + systemctl restart（见 HANDOVER 第 7 节）")


@router.get("/system/update-package")
async def compat_update_package() -> Dict[str, Any]:
    return {"present": False}


@router.post("/system/update-package")
async def compat_update_package_upload(request: Request) -> JSONResponse:
    return _no("离线升级包", "本部署没有 updater；升级直接覆盖 /opt/workbuddy-manager/server 与 /opt/workbuddy-manager/web/out")


@router.delete("/system/update-package")
async def compat_update_package_delete(request: Request) -> JSONResponse:
    return _no("离线升级包", "本部署没有 updater")


@router.get("/system/changelog")
async def compat_changelog(request: Request) -> Dict[str, Any]:
    from .. import __version__

    return {
        "available": False,
        "error": "本部署没有随包的 CHANGELOG.md；变更记录见 /opt/workbuddy-manager/deploy/HANDOVER.md 第 12 节",
        "path": "", "versions": [], "current": __version__,
    }


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


@router.post("/proxy-routes/{name}/auto-assign")
async def compat_proxy_auto_assign(request: Request, name: str) -> JSONResponse:
    """代理线路自动绑定：本部署没有出口代理池，如实拒绝。"""
    return _no("代理线路自动绑定", "qtg 数据面直连上游，没有出口线路")
