"""引擎侧数据的 Manager 侧入口（QTG 适配器契约，U3 阶段 2）。

## 定位

这是「Manager 合成统一入口」的第一步：把 QTG 报的**引擎侧真实数据**（账号运行
态、模型目录、真实用量、上游健康）暴露给 Manager 的调用方，并显式标注它属于
哪个权威。

口径（与 U3 报告一致，别混）：
* `/api/engine/*` —— 引擎侧（QTG 聚合的上游 BFF / 适配器真实数据）；
* `/api/stats/*` —— Manager 自己的请求账本（`request_logs` / `usage_daily`）；
* `/api/accounts` —— Manager 库里的账号副本，**不是**引擎运行态。

两者不可相加，也不可互相替代。

## 为什么不直接往 /api/accounts 里塞

那个响应的形状已经被前端和若干脚本消费；追加新段是破坏性变更（前端可能严格
校验字段，也可能把未知字段当账号渲染）。所以先开独立端点：**先能读，再谈合**。
合并时机由前端改造与 ADR 决定，不由后端单方面推动。

## 降级语义

`qtgadapter.fetch_overview()` 保证「不可用一定有原因」；本层原样透出，且
**不把 unavailable 变成 5xx**——读不到引擎数据不是服务故障，是依赖未就绪。
用 200 + `available=false` 表达，调用方才有机会区分「依赖没配」和「服务坏了」。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from .. import qtgadapter, security

router = APIRouter(prefix='/api/engine', tags=['engine'])


@router.get('/overview')
async def overview(days: int = Query(default=14, ge=1, le=90),
                   user: dict = Depends(security.current_user)
                   ) -> dict[str, Any]:
    """引擎侧总览（来自 QTG 的 `/api/v1/adapter/*` 契约）。

    * `available=false` 时 `reason` 一定是机器可读的原因码
      （`adapter_not_configured` / `token_file_missing` / `all_endpoints_failed` …）；
    * `available=true` 也可能带 `degraded`：说明部分端点失败，其余数据仍可用；
    * `contract_version` 与 Manager 期望不符时，`degraded` 里会出现
      `contract_version_mismatch`，此时字段可能对不上，调用方要自己判。
    """
    payload = await qtgadapter.fetch_overview(days=days)
    payload['days'] = days
    payload['ledger_authority'] = '/api/stats/*'
    payload['note'] = ('engine-side data via QTG adapter contract; '
                       'Manager ledger totals are a separate authority')
    return payload
