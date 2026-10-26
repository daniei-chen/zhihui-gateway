"""内部只读 API 的版本、能力探测与 QTG 读通道的数据端点（K26）。

QTG 进程需要读 Manager 侧的数据（账号、统计、日志、任务、审计、设置、按天×模型
用量…）。历史做法是直接只读打开 Manager 的 SQLite —— 这要求两个进程共享文件系统与
DB 布局，是跨进程耦合。

本端点让 QTG **先探测再使用**：
  - `version` 是读取契约的整数版本，QTG 只在**已知版本**上工作；未知版本必须显式
    失败，不得按旧语义猜；
  - `capabilities` 声明哪些读取族可用（某族不可用时 QTG 必须报错，不得回退直读 DB）；
  - `timezone` / `credit_unit` / `timestamp_unit` 是**口径声明**，避免调用方猜。

鉴权：与其它内部端点一致，走 `current_user`（允许服务令牌）。QTG 用 `scope=read`
的只读服务令牌访问（见 security.current_user 的 scope=read 白名单）；不额外开豁免，
避免成为匿名信息泄露点。

契约版本历史
------------
* v1（2026-10-24）：版本/能力探测 + 既有只读端点（stats/logs/tasks/checkin/audit/
  accounts/models/settings/keys）；
* v2（2026-10-25，R1-A）：新增 `usage` 族（`/api/read-api/usage-matrix`、
  `/api/read-api/usage-totals`），把 QTG 最后两个"组合读"（按天×模型用量、全表拆分）
  搬进契约；`scope=read` 可读 `/api/audit-logs`（`require_admin_or_read_scope`）。
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException

from .. import db, security

router = APIRouter(prefix='/api/read-api', tags=['read-api'])

# 读取契约版本。**只增不改语义**：字段含义变更必须升版本，并保留旧版本字段一个周期，
# 让 QTG 能按版本分支而不是靠猜。
READ_API_VERSION = 2

# 能力表：键是读取族，值是该族当前是否可用。
# 可用性只反映「本部署是否提供该族数据」，不反映数据是否为空——
# 「没有数据」与「读不到」必须能被调用方区分（否则会静默显示 0/空）。
CAPABILITIES = {
    'stats': True,        # /api/stats/{summary,daily,hourly,credits,by-model,by-key,upstream}
    'keys': True,         # /api/keys
    'logs': True,         # /api/logs
    'tasks': True,        # /api/task-logs
    'checkin': True,      # /api/checkin-logs
    'audit': True,        # /api/audit-logs
    'accounts': True,     # /api/accounts
    'models': True,       # /api/models
    'settings': True,     # /api/settings/*
    'usage': True,        # /api/read-api/usage-matrix|usage-totals（v2 起）
}

# 族 → 端点清单（调用方据此校验自己依赖的端点是否在契约内）
ENDPOINTS = {
    'stats': ['/api/stats/summary', '/api/stats/daily', '/api/stats/hourly',
              '/api/stats/credits', '/api/stats/by-model', '/api/stats/by-key',
              '/api/stats/upstream'],
    'keys': ['/api/keys'],
    'logs': ['/api/logs'],
    'tasks': ['/api/task-logs'],
    'checkin': ['/api/checkin-logs'],
    'audit': ['/api/audit-logs'],
    'accounts': ['/api/accounts'],
    'models': ['/api/models'],
    'settings': ['/api/settings/panel', '/api/settings/upstream',
                 '/api/settings/model-map'],
    'usage': ['/api/read-api/usage-matrix', '/api/read-api/usage-totals'],
}

# 日期参数只接受 YYYY-MM-DD：这些端点的调用方是程序（QTG），形态错了要显式 400，
# 不要静默回落到"今天"——那会让对账看起来"一致"而实际查了另一个区间。
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


@router.get('/version')
def read_api_version(user: dict = Depends(security.current_user)) -> dict:
    """返回读取契约版本与口径声明。

    口径声明刻意放在这里（而不是散落在各端点文档）：QTG 必须用同一套口径做归一化，
    否则统计页会出现「同一指标两处不同数」这种最难排查的问题。
    """
    return {
        'version': READ_API_VERSION,
        'capabilities': dict(CAPABILITIES),
        'endpoints': {k: list(v) for k, v in ENDPOINTS.items()},
        # 聚合日边界用本机时区（与 /api/stats 的 _since 实现一致）
        'timezone': time.strftime('%Z') or 'local',
        # 额度是浮点（不是分、不是整数 token）；token 是整数
        'credit_unit': 'float',
        'timestamp_unit': 'epoch_seconds',
        # 分页：offset/limit；limit 上限由各端点自定，stats 族的 days 上限 3650
        'pagination': {'style': 'offset_limit', 'max_days': 3650},
        'min_qtg_version': '0.9',
    }


@router.get('/health')
def read_api_health(user: dict = Depends(security.current_user)) -> dict:
    """轻量健康探针：QTG 用它判断 Manager 是否可读，而不是拿业务请求试错。"""
    return {'ok': True, 'version': READ_API_VERSION, 'ts': int(time.time())}


@router.get('/usage-matrix')
def usage_matrix(since: str, user: dict = Depends(security.current_user)) -> dict:
    """按天 × 模型的用量原始行（QTG `usage()` 的读通道，v2）。

    **为什么给"原始行"而不是聚合结果**：QTG 现有的 sqlite 读法就是逐行取
    `usage_daily` 再在 Python 里累加（按天的 requests/tokens/credit + 当天出现过的
    模型列表）。本端点返回**同一批行**，QTG 两种读模式共用同一段映射代码，切换时
    逐位对账一致（parity 测试就是这么比的）。若在这里换成 GROUP BY 聚合，
    同一 (day, model) 多行时的顺序/合并语义就会漂移，且"再聚合一次"没有收益。

    **边界语义刻意与 QTG 现有 sqlite 实现完全一致**（这是可对账的前提，不是笔误）：

    * rows：`day >= since`，**无上界**（QTG 的读法也不设上界）；
    * failures：`ts >= strftime('%s', since || ' 00:00:00')` —— SQLite 把这种
      字符串当 **UTC** 解释，即实际边界是当地 08:00；分组用 `+8 hours` 的本地日期。
      QTG 现有实现正是这么写的，这里镜像它，保证切换零差异。
      统一到本地零点（`db.day_start_ts`）属于**改默认读法**的批次（R2），
      需要同时改 sqlite 分支并重跑对账，不能只改一边。

    `since` 只接受 `YYYY-MM-DD`；形态不对直接 400（不回落今天）。
    """
    if not isinstance(since, str) or not _DATE_RE.match(since.strip()):
        raise HTTPException(status_code=400, detail='since 必须是 YYYY-MM-DD')
    since = since.strip()
    rows = db.query(
        'SELECT day, model, requests, prompt_tokens, completion_tokens, credit, realm '
        'FROM usage_daily WHERE day >= ? ORDER BY day',
        (since,),
    )
    # 与 QTG 的 usage() 同口径：status >= 400 或 status = 0（0 = 上游未回状态），
    # 不按 realm 过滤、不要求 key_id 非空（QTG 现有实现如此）。
    failure_rows = db.query(
        "SELECT date(ts, 'unixepoch', '+8 hours') AS day, "
        "       SUM(CASE WHEN status >= 400 OR status = 0 THEN 1 ELSE 0 END) AS n "
        "  FROM request_logs"
        " WHERE ts >= strftime('%s', ?)"
        " GROUP BY day",
        (f'{since} 00:00:00',),
    )
    return {
        'version': READ_API_VERSION,
        'since': since,
        'rows': [
            {
                'day': r['day'],
                'model': r['model'],
                'requests': r['requests'],
                'prompt_tokens': r['prompt_tokens'],
                'completion_tokens': r['completion_tokens'],
                'credit': r['credit'],
                'realm': r['realm'],
            }
            for r in rows
        ],
        'failures': {str(r['day']): int(r['n'] or 0)
                     for r in failure_rows if r['day']},
    }


@router.get('/usage-totals')
def usage_totals(user: dict = Depends(security.current_user)) -> dict:
    """全表用量拆分（QTG `usage_analytics()` 的读通道，v2）。

    与 QTG 现有 sqlite 读法同口径：`usage_daily` **全表**（不分时段、不分 realm），
    给出 requests / prompt_tokens / completion_tokens / credit。`/api/stats/summary`
    只有 `total_tokens`（prompt 与 completion 已相加），无法无损表达 analytics，
    所以单独开这个只读端点而不是硬凑。
    """
    row = db.query_one(
        'SELECT SUM(requests) AS requests, '
        '       SUM(prompt_tokens) AS prompt_tokens, '
        '       SUM(completion_tokens) AS completion_tokens, '
        '       SUM(credit) AS credits '
        'FROM usage_daily'
    )
    return {
        'version': READ_API_VERSION,
        'requests': row['requests'] if row is not None else None,
        'prompt_tokens': row['prompt_tokens'] if row is not None else None,
        'completion_tokens': row['completion_tokens'] if row is not None else None,
        'credits': row['credits'] if row is not None else None,
    }
