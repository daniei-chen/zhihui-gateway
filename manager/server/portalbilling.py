"""Fail closed until the actual provider path enforces a verifiable hard cap.

No current WorkBuddy contract bounds credit charged for an arbitrary request.
max_tokens, a balance snapshot, a timeout, and cancelling SSE are not such a
contract. There is intentionally no environment/config/UI bypass here. Offline
accounting fixtures may mock this guard; that does not establish a real cap.
"""

MESSAGE = '单次积分费用上限尚未获得上游强制保障；为禁止透支，用户计费调用暂未开放，积分保留'
CODE = 'portal_hard_cap_unverified'


def dispatch_ready():
    return False


def require_verified_cap():
    from .budget import BudgetRejected
    raise BudgetRejected(MESSAGE, CODE, status=503)
