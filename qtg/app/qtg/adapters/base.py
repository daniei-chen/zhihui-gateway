"""适配器基类：把上游 BFF 的 HTTP 细节收敛到一处。

统一解决四类问题（旧 preview-agent 每个函数各写一遍，且都没处理）：

1. **会话**：登录一次、复用 Cookie；401 时自动重登一次再重试。
2. **超时与重试**：连接/读超时分离，指数退避，只对幂等请求重试。
3. **熔断**：连续失败达阈值后短路 cooldown 秒，避免一个挂掉的上游拖慢整页。
4. **可观测**：每次调用都记 upstream_requests_total / errors / latency。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

import httpx

from ..config import Settings
from ..errors import BreakerOpen, UpstreamError, UpstreamTimeout
from .. import metrics


class Breaker:
    def __init__(self, threshold: int, cooldown: float) -> None:
        self.threshold = max(1, threshold)
        self.cooldown = max(1.0, cooldown)
        self.failures = 0
        self.opened_at = 0.0
        self.trips = 0

    def allow(self) -> bool:
        if self.opened_at == 0.0:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            # 半开：放一个请求探路
            self.opened_at = 0.0
            self.failures = max(0, self.threshold - 1)
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold and self.opened_at == 0.0:
            self.opened_at = time.monotonic()
            self.trips += 1
            metrics.inc("qtg_breaker_open_total", {"upstream": self.name if hasattr(self, "name") else "?"})


class BaseAdapter:
    name = "base"

    def __init__(self, base_url: str, settings: Settings) -> None:
        self.base_url = base_url.rstrip("/")
        self.cfg = settings
        self._client: Optional[httpx.AsyncClient] = None
        self._auth_lock = asyncio.Lock()
        self._authenticated = False
        self.breaker = Breaker(settings.breaker_fail_threshold, settings.breaker_cooldown_sec)
        self.breaker.name = self.name  # type: ignore[attr-defined]
        self.last_error = ""

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._client is None:
            limits = httpx.Limits(max_connections=32, max_keepalive_connections=8)
            timeout = httpx.Timeout(self.cfg.upstream_timeout_sec, connect=5.0)
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=timeout, limits=limits,
                follow_redirects=False, headers={"Accept": "application/json"},
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._authenticated = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise UpstreamError(f"{self.name}: adapter 尚未启动（缺少 lifespan）")
        return self._client

    # ---- 鉴权（子类实现）----
    async def authenticate(self) -> None:  # pragma: no cover - 抽象
        self._authenticated = True

    def _credentials(self) -> str:  # pragma: no cover - 抽象
        return ""

    async def ensure_auth(self, force: bool = False) -> None:
        if self._authenticated and not force:
            return
        async with self._auth_lock:
            if self._authenticated and not force:
                return
            try:
                await self.authenticate()
                self._authenticated = True
            except UpstreamError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一翻译为上游错误
                self.breaker.failure()
                self.last_error = str(exc)[:200]
                raise UpstreamError(f"{self.name} 鉴权失败: {str(exc)[:200]}") from exc

    # ---- 请求 ----
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        retry: bool = True,
        auth: bool = True,
    ) -> httpx.Response:
        if not self.breaker.allow():
            raise BreakerOpen(f"{self.name} 上游熔断中（连续失败 {self.breaker.failures} 次）")
        if auth:
            await self.ensure_auth()

        attempts = 1 + (self.cfg.upstream_retries if retry else 0)
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                resp = await self.client.request(method, path, json=json_body, params=params)
                elapsed = time.monotonic() - started
                metrics.observe(
                    "qtg_upstream_request_duration_seconds",
                    elapsed,
                    {"upstream": self.name, "method": method},
                )
                metrics.inc("qtg_upstream_requests_total", {"upstream": self.name, "method": method})
                if resp.status_code == 401 and auth and attempt == 0:
                    # 会话过期：重登后重试一次
                    await self.ensure_auth(force=True)
                    continue
                if resp.status_code >= 500:
                    if retry and attempt + 1 < attempts:
                        await asyncio.sleep(self.cfg.upstream_backoff_sec * (2**attempt))
                        continue
                    # 2026-10-23 审计修复：原来这里无条件 breaker.success()，导致
                    # 持续返回 5xx 的上游永远不熔断，每个请求都吃满全部重试（~60s）。
                    metrics.inc("qtg_upstream_errors_total", {"upstream": self.name, "kind": "5xx"})
                    self.breaker.failure()
                    return resp
                self.breaker.success()
                return resp
            except (httpx.TimeoutException,) as exc:
                last_exc = exc
                metrics.inc("qtg_upstream_errors_total", {"upstream": self.name, "kind": "timeout"})
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.cfg.upstream_backoff_sec * (2**attempt))
                    continue
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                metrics.inc("qtg_upstream_errors_total", {"upstream": self.name, "kind": "connect"})
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.cfg.upstream_backoff_sec * (2**attempt))
                    continue
        self.breaker.failure()
        self.last_error = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown"
        if isinstance(last_exc, httpx.TimeoutException):
            raise UpstreamTimeout(f"{self.name} 上游超时（{self.cfg.upstream_timeout_sec}s）")
        raise UpstreamError(f"{self.name} 上游不可达: {self.last_error}")

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        retry: bool = True,
        auth: bool = True,
        required: bool = True,
    ) -> Any:
        """返回解析后的 JSON；required=False 时遇到 4xx/5xx 返回 None。"""
        try:
            resp = await self._request(
                method, path, json_body=json_body, params=params, retry=retry, auth=auth
            )
        except (UpstreamError, BreakerOpen):
            if required:
                raise
            return None
        if resp.status_code >= 400:
            if not required:
                return None
            detail = resp.text[:300].replace("\n", " ")
            raise UpstreamError(
                f"{self.name} {method} {path} -> HTTP {resp.status_code}: {detail}",
                code="upstream_status",
            )
        body = resp.text
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            if not required:
                return None
            raise UpstreamError(
                f"{self.name} {method} {path} 返回非 JSON（前 200 字符）: {body[:200]}"
            ) from exc

    async def health(self) -> Dict[str, Any]:
        raise NotImplementedError
