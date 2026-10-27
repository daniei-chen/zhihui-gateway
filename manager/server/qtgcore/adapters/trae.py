"""Trae BFF（127.0.0.1:8792）适配器。

与 Qoder 侧同样采取防御式解析。注意 Trae 的写入接口语义比较特殊：
`POST /api/accounts` 用 `{action: "refresh"|"enable"|"disable"|"add"|..., uid}` 一个
入口承载多种动作，这里在适配器层就把它拆成具名方法，避免把「动作字符串」
泄漏到 API 层。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..errors import UpstreamError
from .base import BaseAdapter


class TraeAdapter(BaseAdapter):
    name = "trae"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.trae_base, settings)
        self.cfg = settings

    def _credentials(self) -> str:
        try:
            with open(self.cfg.trae_token_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    async def authenticate(self) -> None:
        token = self._credentials()
        if not token:
            raise UpstreamError(f"Trae admin token 缺失: {self.cfg.trae_token_file}")
        resp = await self._request(
            "POST", "/api/login", json_body={"password": token}, retry=False, auth=False
        )
        if resp.status_code != 200:
            self._authenticated = False
            raise UpstreamError(f"Trae 登录失败: HTTP {resp.status_code} {resp.text[:160]}")

    # ---- 只读 ----
    async def healthz(self) -> Any:
        return await self._json("GET", "/api/healthz", auth=False, required=False)

    async def overview(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/overview") or {}

    async def usage(self, days: int = 14) -> Dict[str, Any]:
        days = max(1, min(90, int(days)))
        return await self._json("GET", "/api/usage", params={"days": days}) or {}

    async def keys(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/keys") or {}

    async def models(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/models") or {}

    async def logs(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/logs", required=False) or {}

    async def settings(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/settings") or {}

    # ---- 写操作 ----
    async def checkin(self) -> Dict[str, Any]:
        return await self._json(
            "POST",
            "/api/checkin",
            json_body={"scope": "all", "skip_checked_in": True, "skip_expired": True},
            retry=False,
        ) or {}

    async def _account_action(self, action: str, **extra: Any) -> Dict[str, Any]:
        body = {"action": action, **{k: v for k, v in extra.items() if v not in (None, "")}}
        return await self._json("POST", "/api/accounts", json_body=body, retry=False) or {}

    async def refresh_account(self, uid: str = "") -> Dict[str, Any]:
        return await self._account_action("refresh", uid=uid)

    async def set_account(self, uid: str, enabled: bool) -> Dict[str, Any]:
        return await self._account_action("enable" if enabled else "disable", uid=uid)

    async def add_account(self, name: str, jwt: str) -> Dict[str, Any]:
        return await self._account_action("add", name=name, jwt=jwt)

    async def save_keys(self, keys: List[Dict[str, Any]], auth_disabled: Optional[bool] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"keys": keys}
        if auth_disabled is not None:
            body["auth_disabled"] = bool(auth_disabled)
        return await self._json("POST", "/api/keys", json_body=body) or {}

    async def oauth_login_url(self) -> Dict[str, Any]:
        return await self._json("POST", "/api/up/oauth_get_login_url", json_body={}, required=False) or {}

    async def oauth_finish(self, callback_url: str, name: str = "") -> Dict[str, Any]:
        return await self._json(
            "POST", "/api/up/oauth_login", json_body={"callback_url": callback_url, "name": name}
        ) or {}

    async def fetch_remaining_credits(self, user_id: str) -> Dict[str, Any]:
        return await self._json(
            "POST", "/api/up/fetch_remaining_credits", json_body={"user_id": user_id},
            required=False,
        ) or {}

    async def pool_set(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._json("POST", "/api/up/pool_set", json_body=payload, required=False) or {}

    async def health(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            data = await self.healthz()
            ok = bool((data or {}).get("ok"))
            return {
                "up": ok,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "data": data if isinstance(data, dict) else {},
                "last_error": self.last_error,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "up": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "data": {},
                "last_error": str(exc)[:200],
            }
