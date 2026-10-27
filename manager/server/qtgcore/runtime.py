"""进程级运行时：装配适配器、审计、凭证、缓存。

用 lifespan 管理 `httpx.AsyncClient` 的创建/关闭，避免每个请求新建连接池
（旧 preview-agent 是「每次请求重新登录 + 新连接」，在上游侧表现为频繁
登录与连接抖动）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

from . import metrics
from .adapters import (Qoder2Adapter, QoderAdapter, Trae2Adapter,
                       TraeAdapter, WorkBuddyAdapter)
from .audit import AuditLog
from .cache import cache
from .config import Settings
from .identity import IdentityResolver, audit_actor, current_audit_identity
from .security import Auth, CredentialStore, split_csv
from .store import Store

log = logging.getLogger("qtg.runtime")


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # qoder 数据面二选一（QTG_QODER_ENGINE）：
        #   bff    → 旧 Go 控制面，源码已丢失，仅作回滚
        #   qoder2 → qoder2api-hub，MIT 可升级，自带排队/指纹/Anthropic
        if (settings.qoder_engine or "bff") == "qoder2":
            self.qoder = Qoder2Adapter(settings)
        else:
            self.qoder = QoderAdapter(settings)
        # trae 侧同理（QTG_TRAE_ENGINE）：
        #   bff    → 旧 Go 薄封装（源码丢失，仅回滚）
        #   aiwork → 直连 aiwork-server，Rust 实现、镜像可升级
        if (settings.trae_engine or "bff") == "aiwork":
            self.trae = Trae2Adapter(settings)
        else:
            self.trae = TraeAdapter(settings)
        # workbuddy（三合一第三平台）：它自己的反代与面板都在跑，
        # qtg 只读合并，`wb_engine=off` 可随时摘掉。
        self.wb = WorkBuddyAdapter(settings) if (settings.wb_engine or "on") != "off" else None
        self.audit = AuditLog(settings.audit_path)
        self.store = Store(settings.db_path)
        self.credentials = CredentialStore(
            Path(settings.admin_token_file),
            Path(settings.session_secret_file),
            extra_files=[f.strip() for f in settings.extra_password_files.split(",") if f.strip()],
            revoke_file=Path(settings.data_dir) / "session_revoke",
            internal_service_token_file=Path(settings.internal_service_token_file),
            identity_signing_key_file=(Path(settings.identity_signing_key_file)
                                       if settings.identity_signing_key_file else None),
        )
        self.auth = Auth(
            self.credentials,
            read_requires_auth=settings.read_requires_auth,
            ttl=settings.session_ttl_sec,
            allowed_origins=settings.allowed_origins,
            trusted_proxies=split_csv(settings.trusted_edge_peers),
        )
        # 身份断言解析（K05）：与 Auth 共用同一份凭证存储，避免两套口径。
        self.identity = IdentityResolver(
            self.credentials,
            audience=settings.identity_audience,
            max_ttl=settings.identity_max_ttl_sec,
            trusted_peers=split_csv(settings.trusted_edge_peers),
        )

    async def startup(self) -> None:
        self.settings.ensure_dirs()
        await self.qoder.start()
        await self.trae.start()
        if self.wb is not None:
            await self.wb.start()
        log.info(
            # 2026-10-27 审计修正：此前 trae 位置打的是 settings.trae_base（BFF 的
            # 默认值 127.0.0.1:8792，该服务已不存在），而实际生效的是
            # trae_engine 开关选中的 adapter（当前 aiwork → 127.0.0.1:8080）。
            # 日志与事实不符会把排障引向错误方向，这里改成打印 adapter 自己的地址。
            "qtg 启动：qoder=%s(%s) trae=%s(%s) wb=%s data=%s",
            self.qoder.name,
            getattr(self.qoder, "base_url", "?"),
            self.trae.name,
            getattr(self.trae, "base_url", "?"),
            (self.wb.name if self.wb else "off"),
            self.settings.data_dir,
        )

    async def shutdown(self) -> None:
        await self.qoder.close()
        await self.trae.close()
        if self.wb is not None:
            await self.wb.close()
        cache.invalidate()
        log.info("qtg 停止")

    async def health_snapshot(self) -> List[dict]:
        q = await self.qoder.health()
        t = await self.trae.health()
        out = [
            {"name": self.qoder.name, **q},
            {"name": self.trae.name, **t},
        ]
        if self.wb is not None:
            w = await self.wb.health()
            out.append({"name": self.wb.name, **w})
        return out

    def record_audit(self, **kwargs) -> dict:
        context = current_audit_identity()
        if context is not None:
            identity, request_id = context
            # Compatibility handlers historically hard-coded actor=admin/compat.
            # The verified request identity is authoritative for every action.
            kwargs['actor'] = audit_actor(identity)
            detail = dict(kwargs.get('detail') or {})
            detail['identity'] = identity.assertion()
            kwargs['detail'] = detail
            if request_id:
                kwargs['request_id'] = request_id
        rec = self.audit.append(**kwargs)
        metrics.set_gauge("qtg_audit_records", self.audit.count())
        return rec
