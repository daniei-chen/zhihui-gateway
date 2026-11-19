"""进程级运行时：装配适配器、审计、凭证、缓存。

用 lifespan 管理 `httpx.AsyncClient` 的创建/关闭，避免每个请求新建连接池
（旧 preview-agent 是「每次请求重新登录 + 新连接」，在上游侧表现为频繁
登录与连接抖动）。
"""
from __future__ import annotations

import logging
import asyncio
import os
from pathlib import Path
from typing import List, Optional

from . import metrics
from .adapters import (DoubaoWorkAdapter, Qoder2Adapter, QoderAdapter,
                       Trae2Adapter, TraeAdapter, WorkBuddyAdapter)
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
        # qoder 数据面三选一（QTG_QODER_ENGINE）：
        #   off    → **彻底摘掉 qoder**（默认值，2026-11-19 起）
        #   bff    → 旧 Go 控制面，源码已丢失，仅作回滚
        #   qoder2 → qoder2api-hub，MIT 可升级，自带排队/指纹/Anthropic
        #
        # 为什么默认摘掉：qoder 官方**已不再送积分**，这个平台对「免费额度聚合」
        # 这件事不再有产出，留着只会让面板多一个永远 0 的平台、让聚合多一路
        # 必然失败的探针。它的位置由豆包工作接管（见 `UPSTREAM_IDS`）。
        #
        # 摘掉的方式是**不创建适配器**（`self.qoder = None`），不是删代码：
        # `build_aggregate` 会按 `q is not None` 跳过它，下游拿到的就是
        # 「这个平台不存在」，而不是「这个平台一直报错」。
        qoder_engine = (settings.qoder_engine or "off").strip().lower()
        if qoder_engine == "off":
            self.qoder = None
        elif qoder_engine == "qoder2":
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
        # 豆包工作（第四平台）：对接 doubaowork2api 反代引擎。
        # 默认 off —— 新增平台不得改变现网行为，`doubaowork_engine=on` 才挂载。
        self.dbw = (DoubaoWorkAdapter(settings)
                    if (settings.doubaowork_engine or "off") == "on" else None)
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
        # qoder 是可摘除的（`qoder_engine=off` → None）：这里必须判空，
        # 否则 `None.start()` 会让整个 qtgcore 控制面启动失败，
        # 四个平台的管理面一起不可用（2026-11-19 实际踩到）。
        if self.qoder is not None:
            await self.qoder.start()
        await self.trae.start()
        if self.wb is not None:
            await self.wb.start()
        if self.dbw is not None:
            await self.dbw.start()
        log.info(
            # 2026-10-27 审计修正：此前 trae 位置打的是 settings.trae_base（BFF 的
            # 默认值 127.0.0.1:8792，该服务已不存在），而实际生效的是
            # trae_engine 开关选中的 adapter（当前 aiwork → 127.0.0.1:8080）。
            # 日志与事实不符会把排障引向错误方向，这里改成打印 adapter 自己的地址。
            "qtg 启动：qoder=%s(%s) trae=%s(%s) wb=%s dbw=%s data=%s",
            (self.qoder.name if self.qoder else "off"),
            (getattr(self.qoder, "base_url", "-") if self.qoder else "-"),
            self.trae.name,
            getattr(self.trae, "base_url", "?"),
            (self.wb.name if self.wb else "off"),
            (self.dbw.name if self.dbw else "off"),
            self.settings.data_dir,
        )

    async def shutdown(self) -> None:
        if self.qoder is not None:
            await self.qoder.close()
        await self.trae.close()
        if self.wb is not None:
            await self.wb.close()
        if self.dbw is not None:
            await self.dbw.close()
        cache.invalidate()
        log.info("qtg 停止")

    async def health_snapshot(self) -> List[dict]:
        async def probe(adapter):
            try:
                result = await asyncio.wait_for(adapter.health(), timeout=6)
                if not isinstance(result, dict) or type(result.get('up')) is not bool:
                    raise ValueError('invalid health contract')
                return {**result, 'name': adapter.name}
            except Exception:
                return {'name': adapter.name, 'up': False, 'state': 'unknown',
                        'error': 'health_probe_unavailable', 'latency_ms': None}
        # 用 getattr：单测里有夹具用 SimpleNamespace 只提供 qoder/trae/wb 调
        # health_snapshot（未绑定方法），直接 `self.dbw` 会 AttributeError。
        # 与 service.py 的 `getattr(rt, 'dbw', None)` 同口径。
        _dbw = getattr(self, 'dbw', None)
        # qoder 可为 None（`qoder_engine=off`）：不能直接放进待探测列表，
        # 否则 `probe(None)` 会 AttributeError。与下面 wb/dbw 同口径。
        _qoder = getattr(self, 'qoder', None)
        adapters = ([_qoder] if _qoder is not None else []) + [self.trae] + (
            [self.wb] if getattr(self, 'wb', None) is not None else []
        ) + ([_dbw] if _dbw is not None else [])
        return list(await asyncio.gather(*(probe(adapter) for adapter in adapters)))

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
