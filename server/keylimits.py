"""Strict numeric limits shared by HTTP models and internal key writers."""
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
