"""Interpret legacy status collisions without rewriting original request facts."""
import json


def compatibility_rules(value):
    try:
        rules = json.loads(value or '[]')
    except (TypeError, ValueError):
        return []
    return [rule[:64] for rule in rules[:70] if isinstance(rule, str)] if isinstance(rules, list) else []


def displayed_status(row):
    record = dict(row)
    raw, wire = record.get('status'), record.get('http_status')
    if (raw == 502 and type(wire) is int and 400 <= wire < 500
            and record.get('terminal_state') == 'upstream_error'):
        return wire
    return raw
