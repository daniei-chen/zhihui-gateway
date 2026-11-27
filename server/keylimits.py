"""Strict numeric limits shared by HTTP models and internal key writers.

**0 = 不限额度**（`validate` 接受 0 并原样返回），但这条契约只在调用方
**显式**给 0 时成立 —— 不配上限的密钥一直是项目的正常形态，不能禁掉。

额度也可能是**算出来的**（红包把总额拆成 N 份）。那种场合的 0 不是「不限额」，
而是「这一份没分到钱」：顺着 0 的语义走下去，一份残渣就变成一把无限额度的
密钥。算出来的额度必须走 `require_positive`，0 一律拒绝。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


LIMITS = {
    'quota': (9_000_000_000_000_000, True),
    'daily_token_quota': (9_000_000_000_000_000, True),
    'max_ips': (1_000_000, True),
    'quota_credit': (9_000_000_000_000, False),
    'daily_credit_quota': (9_000_000_000_000, False),
}


def validate(name: str, value: object) -> int | float:
    maximum, integral = LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f'{name} requires a finite non-negative number; use explicit 0 for unlimited')
    if isinstance(value, str) and len(value) > 128:
        raise ValueError(f'{name} has an oversized numeric representation')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{name} requires a finite non-negative number') from None
    if (not number.is_finite() or number < 0 or number > maximum
            or (integral and number != number.to_integral_value())):
        raise ValueError(f'{name} is outside its supported non-negative range')
    if integral:
        return int(number)
    if number == 0:
        return 0.0
    if number < Decimal('0.000001'):
        raise ValueError(f'{name} must be at least one credit micro-unit or explicit 0')
    units = number * 1_000_000
    result = float(number)
    if units != units.to_integral_value() or Decimal(str(result)) * 1_000_000 != units:
        raise ValueError(f'{name} must be representable in whole credit micro-units')
    return result


def require_positive(name: str, value: object) -> int | float:
    """按 `validate` 归一化，但**不接受 0**。

    用于「额度是算出来的」那条路径（见模块头）：红包拆出来的一份金额如果是 0，
    那不是「不限额度」，而是「这份没分到钱」—— 必须当场失败，而不是让调用方拿着
    一个恰好等于 0 的计算结果落到 `validate` 的「显式 0 = 不限」语义上。
    调用方**显式**要求不限额时继续用 `validate`。
    """
    result = validate(name, value)
    if not result:
        raise ValueError(
            f'{name} must be a positive amount; only an explicit 0 means unlimited')
    return result
