"""写操作端点：全部要求管理凭证 + Origin 校验 + 审计留痕。

设计约束（来自旧 preview-agent 的事故复盘）：
1. **不允许任何写端点无鉴权**——包括「本机来源」也不可信；
2. 所有落盘用「临时文件 + os.replace」原子替换，避免半截 JSON；
3. 长耗时动作（备份）走线程池，不阻塞事件循环；
4. 每个动作写一条哈希链审计，失败也写（result=error）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field

from .. import metrics
from ..errors import BadRequest, UpstreamError
from ..models import ActionResult

router = APIRouter(prefix="/api/v1/actions", tags=["actions"])

Platform = Literal["qoder", "trae"]


# ---------------------------------------------------------------- 请求体
class CheckinRequest(BaseModel):
    platform: Literal["all", "qoder", "trae"] = "all"


class AccountActionRequest(BaseModel):
    uid: str = ""
    action: Literal["refresh", "enable", "disable", "refresh_all"] = "refresh"


class AddAccountRequest(BaseModel):
    platform: Platform
    name: str = ""
    credential: str = Field(min_length=1, max_length=8192)
    realm: str = ""


class KeysRequest(BaseModel):
    platform: Platform
    keys: List[Dict[str, Any]]
    auth_disabled: Optional[bool] = None


class LimitsRequest(BaseModel):
    enabled: bool = True
    daily_token_limit: int = Field(default=0, ge=0)
    daily_credit_limit: int = Field(default=0, ge=0)
    reserve_credits: int = Field(default=0, ge=0)
    model_daily_token_limit: Dict[str, int] = Field(default_factory=dict)


class NotifyRequest(BaseModel):
    enabled: bool = False
    bark_url: str = ""
    serverchan_sendkey: str = ""
    webhook_url: str = ""


class BackupRequest(BaseModel):
    target: Literal["qoder", "trae"]


class OAuthStartRequest(BaseModel):
    platform: Platform = "qoder"
    realm: str = "cn"


class OAuthFinishRequest(BaseModel):
    platform: Platform = "trae"
    callback: str = Field(min_length=1, max_length=8192)
    name: str = ""


# ---------------------------------------------------------------- 辅助
def _rt(request: Request):
    return request.app.state.rt


async def require_admin(request: Request) -> str:
    """路由级依赖：FastAPI 会在解析 body 之前执行它。

    这样「无凭证 + 非法 body」会得到 401 而不是 422，避免未授权方探测参数 schema。
    """
    return await _guard(request)


async def _guard(request: Request) -> str:
    rt = _rt(request)
    actor = await rt.auth.require_admin(request)
    rt.auth.check_origin(request)
    return actor


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _audit(request: Request, action: str, target: str, result: str, detail: Optional[dict] = None) -> None:
    rt = _rt(request)
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    rt.record_audit(
        action=action,
        actor="admin",
        target=target,
        result=result,
        detail=detail or {},
        request_id=request.headers.get("x-request-id", ""),
        ip=ip,
    )


def _done(request: Request, action: str, target: str, data: Any = None, message: str = "ok") -> ActionResult:
    metrics.inc("qtg_actions_total", {"action": action, "result": "ok"})
    _audit(request, action, target, "ok", {"message": message})
    return ActionResult(ok=True, action=action, target=target, message=message, data=data)


async def _fail(request: Request, action: str, target: str, exc: Exception) -> None:
    metrics.inc("qtg_actions_total", {"action": action, "result": "error"})
    _audit(request, action, target, "error", {"error": str(exc)[:300]})


# ---------------------------------------------------------------- 端点
@router.post("/checkin", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def checkin(request: Request, body: CheckinRequest = Body(default=CheckinRequest())) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    out: Dict[str, Any] = {}
    try:
        if body.platform in ("all", "qoder"):
            out["qoder"] = await rt.qoder.checkin()
        if body.platform in ("all", "trae"):
            out["trae"] = await rt.trae.checkin()
    except Exception as exc:
        await _fail(request, "checkin", body.platform, exc)
        raise
    return _done(request, "checkin", body.platform, out, "签到已执行")


@router.post("/accounts/{platform}/{action}", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def account_action(
    request: Request,
    platform: Platform,
    action: Literal["refresh", "enable", "disable", "refresh_all"],
    body: AccountActionRequest = Body(default=AccountActionRequest()),
) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    target = f"{platform}:{body.uid or '-'}:{action}"
    try:
        if platform == "qoder":
            if action == "refresh":
                data = await rt.qoder.refresh_account(body.uid)
            elif action == "refresh_all":
                acc = await rt.qoder.accounts()
                results = []
                for row in acc.get("accounts") or []:
                    uid = str(row.get("uid") or "")
                    if not uid:
                        continue
                    try:
                        results.append(await rt.qoder.refresh_account(uid))
                    except Exception as exc:  # noqa: BLE001
                        results.append({"uid": uid, "ok": False, "error": str(exc)[:160]})
                data = {"results": results}
            else:
                if not body.uid:
                    raise BadRequest("qoder 的 enable/disable 需要 uid")
                data = await rt.qoder.set_account(body.uid, action == "enable")
        else:
            if action == "refresh_all":
                ov = await rt.trae.overview()
                results = []
                for row in ov.get("accounts") or []:
                    uid = str(row.get("user_id") or "")
                    if not uid:
                        continue
                    try:
                        results.append(await rt.trae.refresh_account(uid))
                    except Exception as exc:  # noqa: BLE001
                        results.append({"uid": uid, "ok": False, "error": str(exc)[:160]})
                data = {"results": results}
            elif action == "refresh":
                data = await rt.trae.refresh_account(body.uid)
            else:
                if not body.uid:
                    raise BadRequest("trae 的 enable/disable 需要 uid")
                data = await rt.trae.set_account(body.uid, action == "enable")
    except Exception as exc:
        await _fail(request, "account", target, exc)
        raise
    return _done(request, "account", target, data, "账号操作完成")


@router.post("/accounts/add", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def add_account(request: Request, body: AddAccountRequest) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    target = f"{body.platform}:add:{body.name or '-'}"
    try:
        if body.platform == "qoder":
            data = await rt.qoder.import_pat(body.credential, body.realm or "cn")
        else:
            data = await rt.trae.add_account(body.name, body.credential)
    except Exception as exc:
        await _fail(request, "account_add", target, exc)
        raise
    return _done(request, "account_add", target, data, "账号已提交")


@router.post("/keys", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def save_keys(request: Request, body: KeysRequest) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    target = f"{body.platform}:keys:{len(body.keys)}"
    try:
        if body.platform == "qoder":
            payload = [
                {
                    "id": str(k.get("id") or ""),
                    "name": str(k.get("name") or ""),
                    "key": str(k.get("key") or ""),
                    "realm": str(k.get("realm") or ""),
                    "enabled": bool(k.get("enabled", True)),
                }
                for k in body.keys
            ]
            data = await rt.qoder.save_keys(payload)
        else:
            data = await rt.trae.save_keys(body.keys, body.auth_disabled)
    except Exception as exc:
        await _fail(request, "keys", target, exc)
        raise
    return _done(request, "keys", target, data, "密钥已保存")


@router.post("/limits", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def save_limits(request: Request, body: LimitsRequest) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    path = Path(rt.settings.limits_file)
    try:
        _atomic_json_write(path, body.model_dump())
    except Exception as exc:
        await _fail(request, "limits", str(path), exc)
        raise
    return _done(request, "limits", str(path), {"limits": body.model_dump()}, "额度护栏已保存")


@router.post("/notify", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def save_notify(request: Request, body: NotifyRequest) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    path = Path(rt.settings.notify_config_file)
    if body.enabled and not any([body.bark_url, body.serverchan_sendkey, body.webhook_url]):
        raise BadRequest("启用通知至少需要配置一个通道")
    try:
        cfg: Dict[str, Any] = {}
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        for k, v in body.model_dump().items():
            cfg[k] = v
        cfg.setdefault("min_interval_sec", 300)
        _atomic_json_write(path, cfg)
    except Exception as exc:
        await _fail(request, "notify", str(path), exc)
        raise
    return _done(request, "notify", str(path), {"notify": {k: ("***" if "url" in k or "key" in k else v) for k, v in body.model_dump().items()}}, "通知配置已保存")


@router.post("/backup", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def run_backup(request: Request, body: BackupRequest) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    script = (
        rt.settings.backup_script_trae if body.target == "trae" else rt.settings.backup_script_qoder
    )
    if not Path(script).exists():
        raise BadRequest(f"备份脚本不存在: {script}")

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", script], capture_output=True, text=True, timeout=900
        )

    try:
        proc = await asyncio.to_thread(_run)
    except Exception as exc:
        await _fail(request, "backup", body.target, exc)
        raise UpstreamError(f"备份执行失败: {exc}") from exc
    out = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    ok = proc.returncode == 0
    if not ok:
        await _fail(request, "backup", body.target, RuntimeError(out[-500:]))
        return ActionResult(
            ok=False, action="backup", target=body.target, message="备份失败", data={"output": out}
        )
    return _done(request, "backup", body.target, {"output": out}, "备份完成")


@router.post("/alerts/ack", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def ack_alerts(request: Request) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    try:
        data = await rt.qoder.ack_alerts()
    except Exception as exc:
        await _fail(request, "alerts_ack", "qoder", exc)
        raise
    return _done(request, "alerts_ack", "qoder", data, "告警已标记已读")


@router.post("/oauth/start", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def oauth_start(request: Request, body: OAuthStartRequest) -> ActionResult:
    await _guard(request)
    rt = _rt(request)
    target = f"{body.platform}:oauth_start"
    try:
        if body.platform == "qoder":
            data = await rt.qoder.oauth_start(body.realm)
        else:
            data = await rt.trae.oauth_login_url()
    except Exception as exc:
        await _fail(request, "oauth_start", target, exc)
        raise
    return _done(request, "oauth_start", target, data, "授权链接已生成")


@router.get("/oauth/poll", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def oauth_poll(
    request: Request,
    platform: Platform = Query(default="qoder"),
    state: str = Query(default=""),
) -> ActionResult:
    await _guard(request)
    if platform != "qoder":
        raise BadRequest("仅 qoder 支持轮询，trae 需提交回调链接")
    if not state:
        raise BadRequest("state 不能为空")
    rt = _rt(request)
    try:
        data = await rt.qoder.oauth_poll(state)
    except Exception as exc:
        await _fail(request, "oauth_poll", f"{platform}:{state[:16]}", exc)
        raise
    metrics.inc("qtg_actions_total", {"action": "oauth_poll", "result": "ok"})
    return ActionResult(ok=True, action="oauth_poll", target=state[:16], data=data)


@router.post("/oauth/finish", response_model=ActionResult, dependencies=[Depends(require_admin)])
async def oauth_finish(request: Request, body: OAuthFinishRequest) -> ActionResult:
    await _guard(request)
    if body.platform != "trae":
        raise BadRequest("qoder 使用轮询自动入池，无需提交回调")
    rt = _rt(request)
    try:
        data = await rt.trae.oauth_finish(body.callback, body.name)
    except Exception as exc:
        await _fail(request, "oauth_finish", "trae", exc)
        raise
    return _done(request, "oauth_finish", "trae", data, "Trae 账号已提交")
