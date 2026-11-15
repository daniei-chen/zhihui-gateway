"""Admin-only reconciliation of unknown upstream usage."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from .. import budget, security
from .. import budgetreview

router=APIRouter(prefix='/api/budgets',tags=['budgets'])

class Resolution(BaseModel):
    tokens: int = Field(ge=0)
    credit: float = Field(ge=0,allow_inf_nan=False)
    reason: str = Field(min_length=1,max_length=500)
    evidence: str = Field(min_length=8,max_length=500)


@router.get('/review')
def review(limit: int = 25, bucket: str = 'all', cursor: str | None = None,
           user=Depends(security.require_admin)):
    try:
        return budgetreview.page(limit=limit, bucket=bucket, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from None


@router.get('/review/evidence')
def review_evidence(request_id: str, user=Depends(security.require_admin)):
    try:
        return budgetreview.evidence(request_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from None

@router.get('/unresolved')
def unresolved(user=Depends(security.require_panel_reader)):
    """未结算预留 + unknown 分类汇总（U02）。

    返回结构向后兼容（`items` 原样保留），新增 `summary`：
      · by_class          六类互斥分类的计数
      · auto_reconcilable 完整上游回执与不可变用量事件均确认零用量的条数
      · needs_human       其余待核对请求，包括只有拒绝错误、没有完整用量证据的请求
      · review_buckets   按新增/历史、7 天以上、明确拒绝拆分的人工队列
      · token_lower_bound 预留估算之和（**不是**实际用量）
      · credit_lower_bound 恒为 0 —— 本地没有定价表，不编造金额
    """
    return budget.unresolved_summary()

class BatchResolution(BaseModel):
    """批量结清的入参。**故意不给「全库」选项**。"""
    request_ids: list[str] = Field(min_length=1, max_length=200)
    tokens: int = Field(default=0, ge=0)
    credit: float = Field(default=0, ge=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=8, max_length=500)
    dry_run: bool = False


@router.post('/resolve-batch')
def resolve_batch(body: BatchResolution, user=Depends(security.require_recent_session_admin)):
    """一次结清多条未决预留。

    ## 为什么需要它

    2026-11-15 实测：库里积压 **1486 条**未决预留。每一条都要单独的
    `POST /api/budgets/{request_id}/resolve` —— 那是一次一个人工操作。

    积压不会自己消失，而告警每天在报。逐条处理 1486 次不是「有耐心」，
    是**没人会真的去做**，于是账本永远不干净、告警永远在响。

    ## 边界

    | 项 | 值 | 理由 |
    | --- | --- | --- |
    | 单次上限 | **200 条** | 一次误操作不能改掉整段历史 |
    | `dry_run` | 支持，默认关 | **强烈建议先跑一次看清清单** |
    | 逐条独立 | 是 | 一条失败不影响其它 |
    | 已 `charged` 的行 | **跳过不结清** | 见下 |

    **不给「按条件全库批量」的选项**：那会把「逐条核对」变成「一键抹平」，
    而这本账存在的意义正是防止后一种。

    ## 复用单条路径的安全闸

    每个 id 走同一条 `budget.resolve(..., expect_charged_zero=True)`：
    扫描与结清之间若已有晚到的用量落库，那条会被**跳过并保留为 unknown**，
    而不是被「结清为零」抹掉已消耗的配额。

    所以本接口在并发下不会伪造账 —— 它只会少改，不会多改。
    """
    if len(body.reason.strip()) < 8 or len(body.evidence.strip()) < 8:
        raise ValueError('核销原因和凭证说明至少8字符')
    try:
        pending = {row['request_id']: row for row in budget.unresolved()}
        planned, unknown_ids, already_charged = [], [], []
        for request_id in body.request_ids:
            row = pending.get(request_id)
            if row is None:
                unknown_ids.append(request_id)
                continue
            if row.get('charged_tokens') or row.get('charged_credit'):
                already_charged.append(request_id)
                continue
            planned.append(request_id)

        if body.dry_run:
            return {'dry_run': True, 'would_resolve': planned,
                    'not_pending': unknown_ids, 'skipped_already_charged': already_charged}

        resolved, skipped = [], []
        with security.authorization_guard(user):
            for request_id in planned:
                changed = budget.resolve(
                    request_id, body.tokens, body.credit, user['username'], body.reason,
                    expect_charged_zero=True, human_evidence=body.evidence)
                (resolved if changed else skipped).append(request_id)
        return {'dry_run': False, 'resolved': resolved, 'skipped_race': skipped,
                'not_pending': unknown_ids, 'skipped_already_charged': already_charged}
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from None


@router.post('/{request_id}/resolve')
def resolve(request_id:str,body:Resolution,user=Depends(security.require_recent_session_admin)):
    try:
        if len(body.reason.strip()) < 8 or len(body.evidence.strip()) < 8:
            raise ValueError('核销原因和凭证说明至少8字符')
        with security.authorization_guard(user):
            return {'changed':budget.resolve(request_id,body.tokens,body.credit,user['username'],body.reason,
                                             human_evidence=body.evidence)}
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from None
