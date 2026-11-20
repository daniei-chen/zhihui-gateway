"""账号扩展动作（`/api/accounts/{file}/test|clear-cooling|proxy|weight|move`）。

授权由 Manager 的 accounts + services/platformauth 显式分流，不经过兼容层兜底。
原则与 `compat_settings.py` 一致：能真实做到的做到（测活、清冷却），
做不到的（代理/权重/分组——qoder-bff 是单账号池、账号 JSON 里没有这些字段）
如实 409，绝不假装成功。

注册顺序必须早于 `compat.py`（它的 GET 兜底会吃掉这里的 GET）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..errors import BadRequest
from ..service import all_accounts, build_aggregate
# U28：`_rt` / `_no` 原先在本文件里各有一份拷贝（与其它 compat 路由逐字节相同）。
# 现在统一从 `_shared` 引入并**在此继续导出**，`compat_accounts_ext._rt` 仍可用。
from ._shared import _no, _rt

router = APIRouter(prefix="/api", tags=["compat-accounts-ext"])


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _split(file: str) -> tuple[str, str]:
    platform, _, uid = file.partition(":")
    return platform or "qoder", uid


# 平台显示名。拒绝文案里不能再写死别的平台的内部名称 ——
# 这些路由是**三平台共用**的，原来豆包用户点「设置权重」会看到
# 「qoder-bff 的 AccountPool 没有 weight 概念」，而 Trae 用户点「清除冷却」
# 会看到「Trae 的冷却状态在 aiwork-server 的 SQLite 里」（在 Trae 页面说 Trae
# 还算对，但在豆包页面就是串平台）。
_PLATFORM_LABEL = {
    "workbuddy": "WorkBuddy",
    "trae": "Trae",
    "doubaowork": "豆包工作",
    "qoder": "Qoder",
}


def _plat(request: Request) -> str:
    """从请求里取平台；取不到就返回空串（文案退回通用说法）。"""
    try:
        from ..identity import platform_of
        return platform_of(request) or ""
    except Exception:  # noqa: BLE001
        return ""


def _label(plat: str) -> str:
    return _PLATFORM_LABEL.get(plat, plat or "本平台")


def _unsupported(request: Request, what: str, why_by_platform: Dict[str, str],
                 default_why: str) -> JSONResponse:
    """按平台给出「不支持」的具体原因。

    每个平台的上游实现不同（豆包是 doubaowork2api、Trae 是 aiwork-server、
    WorkBuddy 是 workbuddy2api），同一个操作在不同平台不可用的**理由也不同**。
    统一用一句话会把别的平台的内部细节暴露给当前用户。
    """
    plat = _plat(request)
    why = why_by_platform.get(plat) or default_why
    return _no(what, why)


_UID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _account_path(rt, uid: str) -> Path:
    """把 uid 解析成账号 JSON 路径，并做包含性校验（K18）。

    `{file:path}` 直接来自 URL：uid 可能是 `../../etc/passwd` 或绝对路径。
    这里只允许正常字符，且 `resolve()` 后必须仍在 accounts 目录内；
    越界一律 400，不做"尽力而为"的字符串清洗。
    """
    if not _UID_RE.match(uid or ""):
        raise BadRequest("账号 ID 非法")
    accounts = Path(rt.settings.qoder_data_dir) / "accounts"
    target = (accounts / f"{uid}.json").resolve()
    try:
        target.relative_to(accounts.resolve())
    except ValueError as exc:
        raise BadRequest("账号 ID 非法") from exc
    return target


# ------------------------------------------------------------------ 单账号动作
def _restart_container(name: str) -> tuple[bool, str]:
    """重启上游容器（清空内存态冷却）。qtg 以 root 运行，可访问 docker.sock。"""
    import subprocess

    try:
        p = subprocess.run(["docker", "restart", name], capture_output=True,
                           text=True, timeout=40)
        if p.returncode == 0:
            return True, (p.stdout or "").strip()[:100]
        return False, (p.stderr or p.stdout or "").strip()[:200]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


_STATE_LABEL = {"ready": "可用", "cooling": "冷却中", "disabled": "已停用",
                "expired": "已过期", "low": "额度不足"}


@router.post("/accounts/{file:path}/test")
async def compat_account_test(request: Request, file: str) -> Dict[str, Any]:
    """测活：用最近一次聚合结果判断该账号当前是否可被调度。

    这不是「发一次真实调用」——那会消耗额度且可能触发风控；BFF 每次聚合时
    本来就会问上游账号状态，这里读的就是那份结果。
    """
    rt = _rt(request)
    platform, uid = _split(file)
    agg = await build_aggregate(rt, force=True)
    for a in all_accounts(agg):
        if a.platform == platform and a.uid == uid:
            state = str(a.state or "")
            ok = state == "ready"
            return {
                "ok": ok,
                "message": f"{_STATE_LABEL.get(state, state or '未知')}"
                           f"；额度 {a.credits_remain if a.credits_remain is not None else '—'}"
                           f"；到期 {a.expires_iso or '—'}",
            }
    return JSONResponse(status_code=404, content={"detail": "未找到该账号"})


@router.post("/accounts/{file:path}/clear-cooling")
async def compat_clear_cooling(request: Request, file: str) -> Dict[str, Any]:
    """强制清除冷却：把账号 JSON 的 cooldownUntil / lastError 清零。

    qoder-bff 在加载账号池时读这两个字段，所以**下次加载**才生效
    （bff 自身没有 reload 接口，重启 qoder-proxy 容器会立即生效）。
    """
    rt = _rt(request)
    platform, uid = _split(file)
    if platform != "qoder":
        # 原来这里写死「Trae 的冷却状态在 aiwork-server 的 SQLite 里」，
        # 但豆包账号也会走到这条分支 —— 于是豆包页面显示的是 Trae 的内部实现。
        return _unsupported(
            request, "清除冷却",
            {
                "doubaowork": "豆包工作的冷却状态在 doubaowork2api 进程内存里，"
                               "控制面不直接改它；请在上游侧清除或等自动冷却。",
                "trae": "Trae 的冷却状态在 aiwork-server 内部，控制面不直接改它；"
                        "请在上游侧清除或等自动冷却。",
                "workbuddy": "WorkBuddy 的冷却状态由 workbuddy2api 自己维护，"
                             "控制面不直接改它；请在上游侧清除或等自动冷却。",
            },
            "该平台的冷却状态由上游自己维护，控制面不直接改它。")
    path = _account_path(rt, uid)
    if not path.exists():
        return JSONResponse(status_code=404, content={"detail": "未找到该账号文件"})
    try:
        from ...services import credentiallease
        from ...services.filelock import file_lock
        from ...services.tencent import _atomic_write_json
        # Same cross-process credential lease as refresh; read only AFTER it is
        # acquired. Preserve freshly rotated tokens and private owner/group.
        async with credentiallease.acquire(path):
            with file_lock(path, writing=True):
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError('invalid credential object')
                had = data.get("cooldownUntil") or 0
                data["cooldownUntil"] = 0
                data["lastError"] = ""
                _atomic_write_json(path, data)
    except Exception as exc:  # noqa: BLE001
        # K18：业务失败不自动重启无关容器。重启是运维动作，必须显式执行，
        # 否则一次"清冷却失败"会把 Qoder 数据面一起打断。
        return JSONResponse(status_code=409, content={
            "detail": f"账号文件不可写（{str(exc)[:100]}）；本部署不在业务失败时自动重启容器，"
                      f"如确需清空内存冷却，请运维执行 docker restart qoder-proxy",
            "manual_action": "docker restart qoder-proxy",
        })
    rt.record_audit(action="account_clear_cooling", actor="compat", result="ok",
                    detail={"file": file, "had": had})
    return {"ok": True, "message": f"已清除冷却（原 cooldownUntil={had}）；上游下次加载账号池时生效"}


@router.put("/accounts/{file:path}/proxy")
async def compat_account_set_proxy(request: Request, file: str) -> JSONResponse:
    return _unsupported(
        request, "账号代理绑定",
        {
            "doubaowork": "豆包工作的账号在上游 doubaowork2api 里维护，没有按账号绑代理的字段；"
                           "需要改出口请在上游侧配置。",
            "trae": "Trae 的账号由 aiwork-server 管理，没有按账号绑代理的字段；"
                    "需要改出口请在 aiwork 侧配置。",
            "workbuddy": "WorkBuddy 的账号文件里没有 proxy 字段，数据面直连上游；"
                         "如需分组出口，请在「设置 → 代理」里配置。",
        },
        "当前平台的账号模型里没有 proxy 字段，无法按账号绑定代理。")


@router.put("/accounts/{file:path}/weight")
async def compat_account_set_weight(request: Request, file: str) -> JSONResponse:
    return _unsupported(
        request, "账号权重",
        {
            "doubaowork": "豆包工作的选号由上游 doubaowork2api 决定，控制面不参与打分，"
                           "因此没有可设置的权重。",
            "trae": "Trae 的选号策略固定在 aiwork-server 内部，控制面不参与打分，"
                    "因此没有可设置的权重。",
        },
        "当前平台的上游不暴露账号权重，选号策略固定在上游内部。")


@router.post("/accounts/{file:path}/move")
async def compat_account_move(request: Request, file: str) -> JSONResponse:
    return _unsupported(
        request, "账号分组",
        {
            "doubaowork": "豆包工作的账号没有自定义分组，只有上游的默认池。",
            "trae": "Trae 的账号没有自定义分组，只有国内 / 国际两个版本维度。",
        },
        "当前平台的账号没有自定义分组。")


# ------------------------------------------------------------------ 备注 / 强制冷却
@router.put("/accounts/{file:path}/note")
async def compat_account_note(request: Request, file: str) -> Dict[str, Any]:
    """账号备注：存在本端（`<data_dir>/account_notes.json`），按 uid 关联。"""
    rt = _rt(request)
    _, uid = _split(file)
    # 2026-11-20 修：原来不校验 uid 就 `data[uid] = note`。
    # 实测 `PUT /api/accounts/trae:../../etc/passwd/note` 返回
    # `{"ok":true,"uid":"","note":"x"}` —— **假成功**：既没写对任何账号，
    # 又在备注表里留下一个空字符串 key。用户看到「保存成功」，
    # 刷新后备注却不在了（因为它挂在一个不存在的 uid 上）。
    # 空 uid / 非法字符一律 400，让调用方知道这条请求根本没生效。
    if not uid:
        raise HTTPException(status_code=400, detail="账号标识为空，无法保存备注")
    if len(uid) > 256 or not all(ch.isalnum() or ch in "_-.:@" for ch in uid):
        raise HTTPException(status_code=400, detail="账号标识含非法字符，无法保存备注")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    note = str((body or {}).get("note") or "")[:200] if isinstance(body, dict) else ""
    p = Path(rt.settings.data_dir) / "account_notes.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    if note:
        data[uid] = note
    else:
        data.pop(uid, None)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return {"ok": True, "uid": uid, "note": note}


@router.post("/accounts/{file:path}/force-cooling")
async def compat_account_force_cooling(request: Request, file: str) -> JSONResponse:
    """强制设定冷却：状态在上游进程内存里，控制面只能清空，不能设值。"""
    return _unsupported(
        request, "手动设定冷却",
        {
            "doubaowork": "豆包工作的冷却状态在 doubaowork2api 进程内存里，控制面只能清除、"
                           "不能设值；请等上游自动冷却，或点「清除冷却」。",
            "trae": "Trae 的冷却状态在 aiwork-server 内部，控制面只能清除、不能设值；"
                    "请等上游自动冷却，或点「清除冷却」。",
        },
        "冷却状态在上游进程内存里，控制面只能清除、不能设值。")


# ------------------------------------------------------------------ 前端方法别名
# 前端有几处 HTTP 方法与 qtg 既有实现不一致（WorkBuddy 原版用 POST，qtg 用 PUT 等）。
# 补一层转发而不是改前端：前端已构建部署，别名最稳。
@router.get("/accounts/{file:path}/credits")
async def compat_account_credits_get(request: Request, file: str) -> Dict[str, Any]:
    from .compat import _account_credits

    return await _account_credits(request, file, force=False)


@router.post("/accounts/{file:path}/disabled")
async def compat_account_disabled_post(request: Request, file: str) -> Dict[str, Any]:
    from .compat import compat_account_disabled

    return await compat_account_disabled(request, file)
