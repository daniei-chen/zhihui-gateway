"""模型中心：展示账号实际可用的模型及其细节。

与「上游配置 → 可用模型」的区别：那里只是上游 /v1/models 的简表；这里直连
腾讯模型接口，能给出**显示名、真实上下文、最大输出、推理档位**，并支持搜索、
按能力筛选与按系列分组。

**按版本（realm）区分**：国内版与国际版的模型几乎不重叠，两边的清单分开取、
分开缓存；不传 realm 默认国内版（与上游「裸名默认 CN」口径一致）。

只读接口。来源与缓存状态如实返回，前端据此标注（不把回退数据说成实时数据）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from .. import security
from ..services import modelcatalog

router = APIRouter(prefix='/api', tags=['models'])


def model_availability(*, days: int = 7, min_sample: int = 5) -> dict:
    """按近 N 天真实请求结果标注模型可用性。0.1.0 / U-6。

    背景：模型目录此前只回答「能不能列出来」，不回答「能不能调通」，运维无法
    从面板看出哪些模型真的在被成功调用。

    判定（刻意保守，不下无证据的结论）：
      * 样本 < `min_sample`                     → ``unknown``（样本不足以判断）
      * 成功率 == 0 且样本 >= `min_sample`      → ``degraded``（实测调不通）
      * 成功率 < 95%                            → ``degraded``（不稳定）
      * 其余                                    → ``verified``

    **必须排除探测流量**（0.1.0 复查修正）：magpie 一类的上游聚合器会枚举并试探
    本端模型，探到不存在/不支持的就留一条 503；若把这些算进可用性，会把「根本没
    被真实调用的模型」误判成 degraded。判据取两条：UA 形如 `magpie/…`，或来源
    为回环地址（本机聚合器探测）。

    只读。数据源是 `request_logs`（真实调用结果），不是上游自报的模型清单。
    """
    from .. import db
    since = int(time.time()) - max(1, int(days)) * 86400
    try:
        rows = db.query(
            'SELECT model, COUNT(*) AS n, '
            'SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) AS ok '
            'FROM request_logs WHERE model IS NOT NULL AND model<>\'\' AND ts>=? '
            "AND COALESCE(ua,'') NOT LIKE 'magpie/%' "
            "AND COALESCE(ip,'') NOT IN ('127.0.0.1','::1') "
            'GROUP BY model', (since,))
    except Exception:  # noqa: BLE001 - 统计失败不影响目录本身
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        total = int(row['n'] or 0)
        ok = int(row['ok'] or 0)
        if total < min_sample:
            state = 'unknown'
        elif ok == 0:
            state = 'degraded'
        elif ok / total < 0.95:
            state = 'degraded'
        else:
            state = 'verified'
        out[str(row['model'])] = {
            'state': state,
            'samples': total,
            'ok': ok,
            'success_rate': round(ok / total, 4) if total else None,
            'window_days': int(days),
        }
    return out


@router.get('/model-catalog')
async def model_catalog(
    realm: str = 'cn',
    force: bool = False,
    user: dict = Depends(security.current_user),
) -> dict:
    """指定版本的模型清单 + 统计 + 可用性标注。

    force=true 会**直连腾讯**重新拉取（跳过 5 分钟缓存）。它消耗账号池的调用
    额度、也有风控风险，因此只允许管理员触发；普通登录用户仍可用缓存版本。

    0.1.0 / U-6：每个模型附带 `availability`（verified / degraded / unknown）。
    注意**不改** `/v1/models`：那是 OpenAI 兼容端点、只透传上游原始响应，
    增加非标准字段会破坏客户端解析。
    """
    if force and user.get('role') != 'admin':
        force = False
    r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    data = await modelcatalog.catalog(r, force=force)
    models = data.get('models') or []
    availability = model_availability()
    annotated = []
    for item in models:
        merged = dict(item)
        model_id = str(item.get('id') or item.get('model') or '')
        merged['availability'] = availability.get(
            model_id, {'state': 'unknown', 'samples': 0, 'ok': 0,
                       'success_rate': None, 'window_days': 7})
        annotated.append(merged)
    return {
        **data,
        'models': annotated,
        'realm': r,
        'summary': modelcatalog.summarize(models),
    }
