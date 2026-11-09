"""Separate transport completion, confirmed rewards and already-claimed evidence."""
from __future__ import annotations

import math
from typing import Any

RESULT_KINDS = (
    "rewarded", "already", "no_reward", "completed", "skipped", "pending",
    "failed", "unknown",
)


def reward_amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def classify_result(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "").lower()
    if status in ("pending", "running", "submitted"):
        return "pending"
    if status in ("skipped", "unavailable", "disabled") or any(
        row.get(key) is True for key in ("skipped", "unavailable", "disabled")
    ):
        return "skipped"
    if row.get("ok") is not True:
        amount = reward_amount(row.get("earned_credit"))
        if amount is not None and amount > 0:
            return "unknown"
        return "failed" if row.get("ok") is False else "unknown"
    amount = reward_amount(row.get("earned_credit"))
    if status == "already" or row.get("already") is True:
        return "unknown" if amount is not None and amount > 0 else "already"
    if amount is not None:
        return "rewarded" if amount > 0 else "no_reward"
    if 'earned_credit' in row:
        return "unknown"
    return "completed" if status == "success" else "unknown"


def summarize_result(payload: Any) -> dict[str, Any]:
    """Only persist bounded counts, never account names, tokens or provider text."""
    counts = {name: 0 for name in RESULT_KINDS}
    rows = payload.get("results") if isinstance(payload, dict) else None
    valid = isinstance(rows, list) and len(rows) <= 10000
    for row in rows if valid else []:
        kind = classify_result(row) if isinstance(row, dict) else "unknown"
        counts[kind] += 1
    if not valid or not rows:
        counts["unknown"] += 1
    ok = valid and bool(rows) and not any(counts[name] for name in ("failed", "unknown", "pending"))
    if isinstance(payload, dict) and payload.get("ok") is False:
        ok = False
    terminal = sum(counts[name] for name in ("rewarded", "already", "no_reward", "completed", "skipped"))
    return {"ok": ok, "accounts": len(rows) if valid else 0,
            "partial": bool(terminal and not ok), **counts}


def validate_summary(value: Any) -> dict[str, Any]:
    """Validate persisted evidence before it can authorize another dispatch."""
    if not isinstance(value, dict) or set(value) != {*RESULT_KINDS, 'ok', 'accounts', 'partial'}:
        raise ValueError('invalid checkin summary')
    if type(value['ok']) is not bool or type(value['partial']) is not bool:
        raise ValueError('invalid checkin summary flags')
    if any(type(value[key]) is not int or not 0 <= value[key] <= 10000
           for key in (*RESULT_KINDS, 'accounts')):
        raise ValueError('invalid checkin summary counts')
    total = sum(value[key] for key in RESULT_KINDS)
    if value['accounts'] == 0:
        if total != 1 or value['unknown'] != 1 or value['ok']:
            raise ValueError('invalid empty checkin summary')
    elif total != value['accounts']:
        raise ValueError('inconsistent checkin summary counts')
    if value['ok'] and any(value[key] for key in ('failed', 'unknown', 'pending')):
        raise ValueError('unconfirmed successful checkin summary')
    terminal = sum(value[key] for key in ('rewarded', 'already', 'no_reward', 'completed', 'skipped'))
    if value['partial'] != bool(terminal and not value['ok']):
        raise ValueError('inconsistent partial checkin summary')
    return dict(value)


def confirmed_summary(value: Any) -> bool:
    summary = validate_summary(value)
    # An account-level error may have happened after the claim. A failed row
    # is not a provider guarantee that no side effect occurred.
    return not any(summary[key] for key in ('unknown', 'pending', 'partial', 'failed'))
