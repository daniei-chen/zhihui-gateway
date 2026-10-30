"""Bounded, scoped Qoder usage evidence without fabricated splits or totals."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import UpstreamError

MAX_COUNT = 2**53 - 1


def _bad():
    raise UpstreamError('Qoder usage evidence malformed or scope mismatch')


def _count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        _bad()
    return value


def _credit(value: Any) -> float:
    if type(value) not in (int, float):
        _bad()
    try:
        number = float(value)
    except OverflowError:
        _bad()
    if not math.isfinite(number) or not 0 <= number <= MAX_COUNT:
        _bad()
    return number


def credits_evidence(payload: Any, days: int, realm: str) -> dict[str, Any]:
    """Keep the engine calendar; consumption cannot prove income or net gain."""
    selected = {'': 'all', 'cn': 'cn', 'global': 'intl'}.get(realm)
    if (type(days) is not int or not 1 <= days <= 90 or selected is None
            or not isinstance(payload, dict) or payload.get('ok') is not True
            or payload.get('realm') != selected or type(payload.get('days')) is not int
            or payload['days'] != days or not isinstance(payload.get('series'), list)
            or len(payload['series']) != days):
        _bad()
    by_day = {}
    for item in payload['series']:
        if not isinstance(item, dict) or not isinstance(item.get('day'), str):
            _bad()
        try:
            date = dt.date.fromisoformat(item['day'])
        except ValueError:
            _bad()
        if date.isoformat() != item['day'] or date in by_day:
            _bad()
        amount = item.get('credit')
        # A malformed amount is unknown, not a successful zero-consumption day.
        try:
            expense = _credit(amount)
        except UpstreamError:
            expense = None
        by_day[date] = {'day': date.isoformat(), 'income': None,
                        'expense': round(expense, 6) if expense is not None else None, 'net': None}
    dates = sorted(by_day)
    if (any((right - left).days != 1 for left, right in zip(dates, dates[1:]))
            or abs((dates[-1] - dt.datetime.now(dt.timezone.utc).date()).days) > 1):
        _bad()
    daily = [by_day[day] for day in dates]

    def known_sum(rows):
        values = [row['expense'] for row in rows]
        total = sum(values) if values and all(value is not None for value in values) else None
        return {'income': None, 'net': None,
                'expense': round(total, 4) if total is not None
                and math.isfinite(total) and total <= MAX_COUNT else None}

    week_start = dates[-1] - dt.timedelta(days=6)
    return {'days': days, 'from': daily[0]['day'], 'to': daily[-1]['day'], 'daily': daily,
            'today': known_sum(daily[-1:]),
            'week': known_sum([by_day[day] for day in dates if day >= week_start]),
            'total': known_sum(daily), 'income_status': 'unavailable',
            'source': 'engine_usage_daily', 'realm': realm, 'calendar_day': daily[-1]['day']}


def hourly_window(day: str, timezone: str, calendar: Any, realm: str) -> dict[str, Any]:
    """Verify the configured engine calendar before selecting an epoch window."""
    daily_evidence(calendar, 1, realm)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        _bad()
    now = dt.datetime.now(zone)
    row = calendar['series'][0]
    midnight = dt.datetime.combine(now.date(), dt.time(), zone)
    if row['day'] != now.date().isoformat() or _credit(row.get('at')) != midnight.timestamp():
        _bad()
    try:
        date = dt.date.fromisoformat(day) if day else now.date()
        if date.isoformat() != (day or date.isoformat()) or not 1971 <= date.year <= 9998:
            raise ValueError('invalid day')
    except (ValueError, TypeError):
        raise ValueError('Invalid usage day') from None
    start = dt.datetime.combine(date, dt.time(), zone).timestamp()
    end = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(), zone).timestamp()
    if not 22 * 3600 <= end - start <= 26 * 3600:
        _bad()
    return {'day': date.isoformat(), 'since': start, 'until': math.nextafter(end, -math.inf),
            'end': end, 'timezone': timezone,
            'current_hour': now.hour if date == now.date() else None}


def hourly_evidence(payload: Any, window: dict[str, Any], realm: str) -> list[dict[str, Any]]:
    selected = {'': 'all', 'cn': 'cn', 'global': 'intl'}.get(realm)
    # Minute buckets also preserve half-hour DST changes; repeated local hours
    # are merged, rather than dropped by a fixed 24-epoch-hour assumption.
    if (selected is None or not isinstance(payload, dict) or payload.get('ok') is not True
            or payload.get('realm') != selected or type(payload.get('bucket_seconds')) is not int
            or payload['bucket_seconds'] != 60 or _credit(payload.get('since')) != window['since']
            or _credit(payload.get('until')) != window['until']
            or not isinstance(payload.get('series'), list)
            or len(payload['series']) > math.ceil((window['end'] - window['since']) / 60)):
        _bad()
    zone = ZoneInfo(window['timezone'])
    rows = [{'day': window['day'], 'hour': hour, 'requests': 0, 'failed': 0,
             'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'credit': 0.0,
             'current_hour': window['current_hour'], 'timezone': window['timezone']}
            for hour in range(24)]
    seen = set()
    for item in payload['series']:
        if not isinstance(item, dict):
            _bad()
        at = _credit(item.get('at'))
        offset = (at - window['since']) / 60
        if (at in seen or not window['since'] <= at < window['end'] or not offset.is_integer()):
            _bad()
        seen.add(at)
        local = dt.datetime.fromtimestamp(at, zone)
        if local.date().isoformat() != window['day']:
            _bad()
        target = rows[local.hour]
        for source, field in (('requests', 'requests'), ('errors', 'failed'),
                              ('prompt_tokens', 'prompt_tokens'), ('completion_tokens', 'completion_tokens'),
                              ('total_tokens', 'total_tokens')):
            target[field] = _count(target[field] + _count(item.get(source)))
        target['credit'] = _credit(target['credit'] + _credit(item.get('credit')))
    for field in ('requests', 'failed', 'prompt_tokens', 'completion_tokens', 'total_tokens'):
        _count(sum(row[field] for row in rows))
    _credit(sum(row['credit'] for row in rows))
    return rows


def daily_evidence(payload: Any, days: int, realm: str) -> dict[str, Any]:
    selected = {'': 'all', 'cn': 'cn', 'global': 'intl'}.get(realm)
    if (selected is None or not isinstance(payload, dict) or payload.get('ok') is not True
            or payload.get('realm') != selected or type(payload.get('days')) is not int
            or payload['days'] != days or payload.get('group_by') != 'model'
            or not isinstance(payload.get('series'), list) or len(payload['series']) != days
            or not isinstance(payload.get('groups'), list) or len(payload['groups']) > 10000):
        _bad()
    rows, previous = [], None
    for row in payload['series']:
        if not isinstance(row, dict) or not isinstance(row.get('day'), str):
            _bad()
        try:
            date = dt.date.fromisoformat(row['day'])
        except ValueError:
            _bad()
        if (date.isoformat() != row['day'] or previous is not None
                and (date - previous).days != 1):
            _bad()
        previous = date
        fields = {name: _count(row.get(name)) for name in
                  ('requests', 'errors', 'prompt_tokens', 'completion_tokens', 'total_tokens')}
        rows.append({'day': row['day'], **fields, 'credit': _credit(row.get('credit'))})
    # Engine-local calendar days may differ from the Manager at midnight.
    # Preserve the engine's labels instead of refilling on a different calendar.
    utc_today = dt.datetime.now(dt.timezone.utc).date()
    if not rows or abs((previous - utc_today).days) > 1:
        _bad()
    models, ids = [], set()
    for group in payload['groups']:
        if (not isinstance(group, dict) or not isinstance(group.get('id'), str)
                or not group['id'] or len(group['id']) > 1024 or group['id'] in ids):
            _bad()
        ids.add(group['id'])
        models.append({'name': group['id'], 'requests': _count(group.get('requests')),
                       'failed': _count(group.get('errors')), 'total_tokens': _count(group.get('total_tokens')),
                       'prompt_tokens': None, 'completion_tokens': None,
                       'credit': _credit(group.get('credit')), 'cache_hit_rate': None,
                       'tokens_per_sec': None, 'throughput_samples': 0})
    for daily_field, group_field in (('requests', 'requests'), ('errors', 'failed'),
                                     ('total_tokens', 'total_tokens')):
        total = sum(row[daily_field] for row in rows)
        if total > MAX_COUNT or total != sum(row[group_field] for row in models):
            _bad()
    total_credit = sum(row['credit'] for row in rows)
    if not math.isfinite(total_credit) or total_credit > MAX_COUNT or not math.isclose(
            total_credit, sum(row['credit'] for row in models), rel_tol=1e-8, abs_tol=1e-6):
        _bad()
    return {'daily': [{'day': row['day'], 'requests': row['requests'],
                       'prompt_tokens': row['prompt_tokens'], 'completion_tokens': row['completion_tokens'],
                       'total_tokens': row['total_tokens'], 'credit': row['credit'], 'failed': row['errors']}
                      for row in rows],
            'by_model': sorted(models, key=lambda row: (-row['total_tokens'], row['name'])),
            'days': days, 'realm': realm, 'source': 'engine_usage_daily'}


def summary_evidence(daily: dict[str, Any], snapshot: Any, realm: str) -> dict[str, Any]:
    selected = {'': 'all', 'cn': 'cn', 'global': 'intl'}.get(realm)
    if (not isinstance(snapshot, dict) or snapshot.get('realm') != selected
            or snapshot.get('ok') is False or not isinstance(snapshot.get('by_model'), dict)
            or len(snapshot['by_model']) > 10000):
        _bad()
    rows = daily['daily']
    today = rows[-1]
    total_requests = _count(snapshot.get('requests'))
    total_tokens = _count(snapshot.get('total_tokens'))
    total_credit = _credit(snapshot.get('credit'))
    candidates = []
    for name, row in snapshot['by_model'].items():
        if not isinstance(name, str) or not name or len(name) > 1024 or not isinstance(row, dict):
            _bad()
        candidates.append((name, _count(row.get('requests'))))
    if sum(count for _, count in candidates) != total_requests:
        _bad()
    week_credit = sum(row['credit'] for row in rows)
    week_tokens = sum(row['total_tokens'] for row in rows)
    week_requests = sum(row['requests'] for row in rows)
    if (week_tokens > MAX_COUNT or week_requests > MAX_COUNT
            or not math.isfinite(week_credit) or week_credit > MAX_COUNT):
        _bad()
    return {'today_requests': today['requests'], 'today_tokens': today['total_tokens'],
            'today_credit': today['credit'], 'week_requests': week_requests,
            'week_tokens': week_tokens, 'week_credit': week_credit,
            'total_requests': total_requests, 'total_tokens': total_tokens, 'total_credit': total_credit,
            'top_model': min(candidates, key=lambda row: (-row[1], row[0]))[0] if candidates else None,
            'stats_health': {'ok': True, 'detail': ''}, 'usage_health': {'ok': True, 'detail': ''},
            'source': 'engine_local_usage', 'calendar_day': today['day'], 'realm': realm}
