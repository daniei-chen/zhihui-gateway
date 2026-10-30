"""Qoder BFF（127.0.0.1:8791）适配器。

上游是自研 Go 控制面（源码已丢失，见 docs/ARCHITECTURE.md 的「已知缺口」），
因此这里对每个响应都做**防御式解析**：字段缺失一律给默认值，绝不因上游加/
减字段把整页打挂。契约以 `docs/UPSTREAM-CONTRACT.md` 记录的实测样本为准。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..errors import UpstreamError
from .base import BaseAdapter


class QoderAdapter(BaseAdapter):
    name = "qoder"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.qoder_base, settings)
        self.cfg = settings

    def _credentials(self) -> str:
        try:
            with open(self.cfg.qoder_password_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    async def authenticate(self) -> None:
        pwd = self._credentials()
        if not pwd:
            raise UpstreamError(f"Qoder 面板密码缺失: {self.cfg.qoder_password_file}")
        resp = await self._request(
            "POST", "/api/login", json_body={"password": pwd}, retry=False, auth=False
        )
        if resp.status_code != 200:
            self._authenticated = False
            raise UpstreamError(f"Qoder 登录失败: HTTP {resp.status_code} {resp.text[:160]}")

    # ---- 只读 ----
    async def healthz(self) -> Any:
        return await self._json("GET", "/api/healthz", auth=False, required=False)

    async def accounts(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/up/accounts") or {}

    async def scheduler(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/up/scheduler") or {}

    async def usage_analytics(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/up/usage/analytics", required=False) or {}

    async def usage(self, days: int = 14) -> Dict[str, Any]:
        days = max(1, min(90, int(days)))
        return await self._json("GET", "/api/usage", params={"days": days}) or {}

    async def credits_flow(self, days: int = 7) -> Dict[str, Any]:
        days = max(1, min(90, int(days)))
        return await self._json("GET", "/api/credits/flow", params={"days": days}, required=False) or {}

    async def settings(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/up/settings") or {}

    async def models(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/up/models") or {}

    async def logs(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/up/logs", required=False) or {}

    async def alerts(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/alerts", required=False) or {}

    async def backups(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/backups", required=False) or {}

    async def quotas(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/quotas", required=False) or {}

    async def audit(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/audit", required=False) or {}

    async def affinity(self) -> Dict[str, Any]:
        return await self._json("GET", "/api/scheduler/affinity", required=False) or {}

    # ---- 写操作 ----
    async def checkin(self) -> Dict[str, Any]:
        return await self._json("POST", "/api/up/accounts/checkin", json_body={}, retry=False) or {}

    async def refresh_account(self, uid: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {} if not uid else {"uid": uid}
        return await self._json("POST", "/api/up/accounts/refresh", json_body=body) or {}

    async def set_account(self, uid: str, enabled: bool) -> Dict[str, Any]:
        return await self._json(
            "POST", "/api/up/accounts/set", json_body={"uid": uid, "enabled": enabled}
        ) or {}

    async def import_pat(self, pat: str, realm: str = "cn") -> Dict[str, Any]:
        return await self._json(
            "POST", "/api/up/accounts/import/pat", json_body={"pat": pat, "realm": realm or "cn"}
        ) or {}

    async def save_keys(self, api_keys: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._json(
            "POST", "/api/up/settings/save", json_body={"api_keys": api_keys}
        ) or {}

    async def ack_alerts(self) -> Dict[str, Any]:
        return await self._json("POST", "/api/alerts/ack", json_body={}, required=False) or {}

    async def backup(self) -> Dict[str, Any]:
        return await self._json(
            "POST", "/api/backup", json_body={}, retry=False, required=False
        ) or {}

    async def reset_quotas(self) -> Dict[str, Any]:
        return await self._json("POST", "/api/quotas/reset", json_body={}, required=False) or {}

    async def oauth_start(self, realm: str = "cn") -> Dict[str, Any]:
        return await self._json(
            "POST",
            "/api/up/accounts/login/start",
            json_body={"platform": "CLI", "realm": realm or "cn"},
            retry=False,
        ) or {}

    async def oauth_poll(self, state: str) -> Dict[str, Any]:
        return await self._json(
            "GET", "/api/up/accounts/login/poll", params={"state": state}, retry=False
        ) or {}

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
        except Exception as exc:  # noqa: BLE001 - 健康检查必须永不抛
            return {
                "up": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "data": {},
                "last_error": str(exc)[:200],
            }
