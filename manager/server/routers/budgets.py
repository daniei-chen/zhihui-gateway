"""Admin-only reconciliation of unknown upstream usage."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from .. import budget, security

router=APIRouter(prefix='/api/budgets',tags=['budgets'])

class Resolution(BaseModel):
    tokens: int = Field(ge=0)
    credit: float = Field(ge=0,allow_inf_nan=False)
    reason: str = Field(min_length=1,max_length=500)

@router.get('/unresolved')
def unresolved(user=Depends(security.require_admin)):
    """未结算预留 + unknown 分类汇总（U02）。

    返回结构向后兼容（`items` 原样保留），新增 `summary`：
      · by_class          六类互斥分类的计数
      · auto_reconcilable 完整上游回执与不可变用量事件均确认零用量的条数
      · needs_human       其余待核对请求，包括只有拒绝错误、没有完整用量证据的请求
      · token_lower_bound 预留估算之和（**不是**实际用量）
      · credit_lower_bound 恒为 0 —— 本地没有定价表，不编造金额
    """
    return budget.unresolved_summary()

@router.post('/{request_id}/resolve')
def resolve(request_id:str,body:Resolution,user=Depends(security.require_admin)):
    try:
        return {'changed':budget.resolve(request_id,body.tokens,body.credit,user['username'],body.reason)}
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from None
