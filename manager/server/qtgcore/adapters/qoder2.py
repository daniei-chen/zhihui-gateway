"""Qoder2 引擎适配器（shuishuipingan/qoder2api-hub，默认 http://127.0.0.1:8799）。

为什么要有它
------------
旧数据面是 `qoder-bff`（Go，**源码已丢失**）：不可升级、不可修 bug、没有
Anthropic 协议、没有排队重试、没有设备指纹。`qoder2api-hub` 是同一支血脉的
成熟实现（MIT、120+ commits、自带 COSY 签名 / 设备指纹 / 排队等待 / 熔断降权 /
前缀缓存），用它可以一次性补上审计里 B 域缺的十几项机制。

设计约束
--------
* **对外形状与 `QoderAdapter` 完全一致**：qtg 的 `service.py` / `compat_*.py`
  一行都不用改，切换到本引擎只是换一个类。
* 面板鉴权用 `POST /panel/login` 换 token，再以 `X-Panel-Token` 请求头带过去
  （旧 BFF 是 Cookie 会话）。token 过期由基类的 401 重登逻辑兜住。
* 形状差异只在 `usage()` 里做转换：qoder2api 给的是 `series[]`（含
  prompt/completion/reasoning/cached 拆分），qtg 内部要的是 `buckets[]` + `by_model[]`。

回滚
----
配置项 `QTG_QODER_ENGINE=bff|qoder2` 决定用哪个类（见 runtime.py），改回 `bff`
重启即回滚，两个引擎可以同时在线。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..errors import UpstreamError
from .base import BaseAdapter


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Qoder2Adapter(BaseAdapter):
    name = "qoder2"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.qoder2_base, settings)
        self.cfg = settings
        self._panel_token = ""

    # ---- 鉴权 ----
    def _credentials(self) -> str:
        path = self.cfg.qoder2_panel_password_file
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    async def authenticate(self) -> None:
        pwd = self._credentials()
        if not pwd:
            raise UpstreamError(
                f"Qoder2 面板密码缺失: {self.cfg.qoder2_panel_password_file}"
            )
        resp = await self._request(
            "POST", "/panel/login", json_body={"password": pwd}, retry=False, auth=False
        )
        if resp.status_code != 200:
            raise UpstreamError(
                f"Qoder2 登录失败: HTTP {resp.status_code} {resp.text[:160]}"
            )
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(f"Qoder2 登录响应不是 JSON: {resp.text[:160]}") from exc
        token = str(body.get("token") or "")
        if not token:
            raise UpstreamError("Qoder2 登录响应里没有 token")
        self._panel_token = token
        # 之后所有请求自动带面板 token（httpx 的默认 headers 会合并到每个请求）
        self.client.headers["X-Panel-Token"] = token
        self.client.headers.pop("Cookie", None)

    # ---- 只读 ----
    async def healthz(self) -> Any:
        return await self._json("GET", "/health", auth=False, required=False)

    async def accounts(self) -> Dict[str, Any]:
        """`GET /accounts` → `{"accounts": [...]}`，字段与旧 BFF 高度同源
        （uid/nickname/realm/enabled/expiresAt/inCooldown/credits{remain,size,used}），
        `service.map_qoder_account()` 直接可解析。"""
        return await self._json("GET", "/accounts") or {}

    async def scheduler(self) -> Dict[str, Any]:
        return await self._json("GET", "/scheduler", required=False) or {}

    async def usage_analytics(self) -> Dict[str, Any]:
        return await self._json("GET", "/usage/analytics", required=False) or {}

    async def usage(self, days: int = 14) -> Dict[str, Any]:
        """把 qoder2api 的时间序列转成 qtg 内部认识的形状。

        返回：`{"buckets": [{label, requests, tokens, credit}],
                "by_model": [{model, requests, tokens, credit}],
                "points": {...}, "days": N}`

        `label` 用 `MM/DD`（qtg 的 `_month_day_to_date` 按这个格式猜年份）。
        """
        days = max(1, min(90, int(days)))
        daily = await self._json(
            "GET", "/usage/daily", params={"days": days}, required=False
        ) or {}
        buckets: List[Dict[str, Any]] = []
        for row in daily.get("series") or []:
            day = str(row.get("day") or "")
            label = day[5:].replace("-", "/") if len(day) >= 10 else str(row.get("label") or "")
            buckets.append(
                {
                    "label": label,
                    "requests": _i(row.get("requests")),
                    "tokens": _i(row.get("total_tokens")),
                    "credit": round(_f(row.get("credit")), 6),
                    # 额外字段：qtg 的兼容统计层可以直接用，不必再算
                    "prompt_tokens": _i(row.get("prompt_tokens")),
                    "completion_tokens": _i(row.get("completion_tokens")),
                    "errors": _i(row.get("errors")),
                }
            )

        agg = await self._json(
            "GET", "/usage", params={"range": "month"}, required=False
        ) or {}
        by_model: List[Dict[str, Any]] = []
        raw_models = agg.get("by_model") or {}
        if isinstance(raw_models, dict):
            for name, v in raw_models.items():
                if not isinstance(v, dict):
                    continue
                by_model.append(
                    {
                        "model": str(name),
                        "requests": _i(v.get("requests")),
                        "tokens": _i(v.get("total_tokens")),
                        "credit": round(_f(v.get("credit")), 6),
                        "prompt_tokens": _i(v.get("prompt_tokens")),
                        "completion_tokens": _i(v.get("completion_tokens")),
                    }
                )
        elif isinstance(raw_models, list):
            for v in raw_models:
                if not isinstance(v, dict):
                    continue
                by_model.append(
                    {
                        "model": str(v.get("model") or v.get("name") or ""),
                        "requests": _i(v.get("requests")),
                        "tokens": _i(v.get("total_tokens") or v.get("tokens")),
                        "credit": round(_f(v.get("credit")), 6),
                    }
                )
        by_model.sort(key=lambda m: -m["requests"])
        return {"buckets": buckets, "by_model": by_model, "points": {}, "days": days}

    async def credits_flow(self, days: int = 7) -> Dict[str, Any]:
        """按天积分流水。

        旧 BFF 的 `/api/credits/flow` 给的是 `{daily:[{day,income,expense}]}`；
        qoder2 没有同形状接口，但 `/usage/daily` 的每个时间桶里带 `credit`
        （当天真实消耗），这里转成同一形状，`service.usage_payload()` 直接用。
        另外附上 `/usage/remaining`（每账号余额 + 预计可用天数）供别的调用方取。
        """
        days = max(1, min(90, int(days)))
        daily = await self._json(
            "GET", "/usage/daily", params={"days": days}, required=False
        ) or {}
        rows: List[Dict[str, Any]] = []
        for s in daily.get("series") or []:
            day = str(s.get("day") or "")[:10]
            if not day:
                continue
            rows.append(
                {
                    "day": day,
                    "income": 0.0,
                    "expense": round(_f(s.get("credit")), 6),
                }
            )
        remaining = await self._json("GET", "/usage/remaining", required=False) or {}
        return {"daily": rows, "remaining": remaining}

    async def settings(self) -> Dict[str, Any]:
        return await self._json("GET", "/settings", required=False) or {}

    async def models(self) -> Dict[str, Any]:
        """`GET /v1/models` 已带 capabilities / aliases / realm / 描述。"""
        return await self._json("GET", "/v1/models", auth=False, required=False) or {}

    async def logs(self) -> Dict[str, Any]:
        return await self._json("GET", "/logs", required=False) or {}

    # ---- 智能体（客户端一键配置）----
    async def agents(self) -> Dict[str, Any]:
        """qoder2 本机客户端检测 + 接入状态（Claude Code / Codex / Crush / DSH…）。"""
        return await self._json("GET", "/agents", required=False) or {"clients": []}

    async def agents_apply(self, client_id: str) -> Dict[str, Any]:
        """把某个客户端指向网关。qoder2 只接受本机（loopback）调用——qtg 正是 loopback。"""
        return await self._json(
            "POST", "/agents/apply", json_body={"client": client_id}, retry=False, required=False
        ) or {"ok": False, "error": "上游拒绝或未实现"}

    async def agents_restore(self, client_id: str) -> Dict[str, Any]:
        return await self._json(
            "POST", "/agents/restore", json_body={"client": client_id}, retry=False, required=False
        ) or {"ok": False, "error": "上游拒绝或未实现"}

    async def alerts(self) -> Dict[str, Any]:
        """qoder2api 没有独立告警列表，把 `/health` 的 risk 信号翻译成告警项。

        旧 BFF 的 `/api/alerts` 也是同一类东西（队列/限流/拒绝），这里保持相同语义：
        只报「有信号」的项，避免前端出现永远为空的告警页。
        """
        h = await self.healthz() or {}
        risk = h.get("risk") or {}
        rows: List[Dict[str, Any]] = []
        labels = {
            "queue": ("排队", "上游队列拥堵，请求正在等待"),
            "rate": ("限流", "上游返回 429，已进入退避"),
            "rejected": ("拒绝", "上游 403 直接拒绝（多为风控）"),
            "version_gate": ("版本门", "客户端版本过低，需要升级"),
        }
        for key, (title, desc) in labels.items():
            n = _i(risk.get(key))
            if n > 0:
                rows.append(
                    {
                        "id": f"risk-{key}",
                        "time": "",
                        "level": "warn" if key != "rejected" else "error",
                        "title": f"{title} × {n}",
                        "message": desc,
                        "ack": False,
                    }
                )
        last = risk.get("last")
        if isinstance(last, dict) and last.get("kind"):
            rows.append(
                {
                    "id": "risk-last",
                    "time": str(last.get("at") or ""),
                    "level": "info",
                    "title": f"最近信号：{last.get('kind')}",
                    "message": str(last.get("detail") or "")[:200],
                    "ack": False,
                }
            )
        return {"alerts": rows, "unread": len(rows)}

    async def backups(self) -> Dict[str, Any]:
        return {"backups": {}}

    async def quotas(self) -> Dict[str, Any]:
        return await self._json("GET", "/settings", required=False) or {}

    async def audit(self) -> Dict[str, Any]:
        return {"records": []}

    async def affinity(self) -> Dict[str, Any]:
        return await self._json("GET", "/scheduler", required=False) or {}

    # ---- 写操作 ----
    async def checkin(self) -> Dict[str, Any]:
        return await self._json("POST", "/accounts/checkin", json_body={}) or {}

    async def refresh_account(self, uid: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {} if not uid else {"uid": uid}
        return await self._json("POST", "/accounts/refresh", json_body=body) or {}

    async def set_account(self, uid: str, enabled: bool) -> Dict[str, Any]:
        return await self._json(
            "POST", "/accounts/set", json_body={"uid": uid, "enabled": enabled}
        ) or {}

    async def import_pat(self, pat: str, realm: str = "cn") -> Dict[str, Any]:
        return await self._json(
            "POST", "/accounts/import/pat", json_body={"pat": pat, "realm": realm or "cn"}
        ) or {}

    async def save_keys(self, api_keys: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._json(
            "POST", "/settings/save", json_body={"api_keys": api_keys}
        ) or {}

    async def ack_alerts(self) -> Dict[str, Any]:
        return {"ok": True}

    async def backup(self) -> Dict[str, Any]:
        return await self._json("POST", "/accounts/export", json_body={}, required=False) or {}

    async def reset_quotas(self) -> Dict[str, Any]:
        return await self._json("POST", "/settings/save", json_body={}, required=False) or {}

    async def oauth_start(self, realm: str = "cn") -> Dict[str, Any]:
        return await self._json(
            "POST", "/accounts/login/start", json_body={"platform": "CLI", "realm": realm or "cn"},
            retry=False,
        ) or {}

    async def oauth_poll(self, state: str) -> Dict[str, Any]:
        return await self._json(
            "GET", "/accounts/login/poll", params={"state": state}, retry=False
        ) or {}

    # ---- 健康 ----
    async def health(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            h = await self.healthz() or {}
        except Exception as exc:  # noqa: BLE001
            return {
                "up": False,
                "latency_ms": None,
                "last_error": str(exc)[:200],
                "accounts": 0,
                "engine": "qoder2",
            }
        latency = int((time.monotonic() - started) * 1000)
        return {
            "up": bool(h.get("ok")),
            "latency_ms": latency,
            "last_error": "",
            "accounts": _i(h.get("accounts")),
            "accounts_ready": _i(h.get("accounts_ready")),
            "engine": "qoder2",
            "version": str(h.get("version") or ""),
            "realm": str(h.get("realm") or ""),
        }
