"""与 Manager 身份域协作：退出时让 Manager 的 wb_session 一并生效。

K03（2026-10-23 S0）：qoder/trae 平台下前端调用 `/api/logout` 会按平台头落到 QTG，
原实现只删 `qtg_session`，而浏览器里的 `wb_session` 仍然有效 —— 用户以为退出了，
实际仍以原身份登录（`/api/me` 依旧 200）。这里把退出请求转发给 Manager，
并把 Manager 返回的 `Set-Cookie`（删除 wb_session）合并进 QTG 的响应。

边界：
* 只转发 `wb_session`，避免把 `qtg_session` 带给 Manager；
* Manager 的 `/api/logout` 只清当前浏览器 cookie，不吊销其他设备（那是
  `/api/sessions/revoke` 的语义），所以这里不要求登录态；
* 转发失败不抛异常：调用方仍删本地 qtg_session，并把结果标记为 degraded，
  不允许"假装完全成功"。
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("qtg.manager_session")

WB_COOKIE = "wb_session"
DEFAULT_TIMEOUT_SEC = 3.0


def wb_cookie_header(cookies: dict[str, str] | None) -> str:
    """从请求 Cookie 中只挑出 wb_session；没有则返回空串。"""
    value = (cookies or {}).get(WB_COOKIE) or ""
    return f"{WB_COOKIE}={value}" if value else ""


async def forward_logout(
    panel_base: str,
    cookie_header: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> tuple[bool, list[str]]:
    """把退出请求转发给 Manager。

    Returns:
        (ok, set_cookie_headers)：ok=False 表示 Manager 不可达或返回错误，
        调用方应把本次退出标记为 degraded；set_cookie_headers 是要合并回
        浏览器的 Set-Cookie 头（通常是删除 wb_session 的那条）。
    """
    if not panel_base or not cookie_header:
        return False, []
    url = panel_base.rstrip("/") + "/api/logout"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers={"cookie": cookie_header})
    except Exception as exc:  # noqa: BLE001 - 网络异常不应把退出变成 500
        log.warning("manager logout forward failed: %s", exc)
        return False, []
    if resp.status_code >= 400:
        log.warning("manager logout returned %s", resp.status_code)
        return False, []
    return True, resp.headers.get_list("set-cookie")
