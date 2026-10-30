"""Selected-platform key evidence from Manager logs, never legacy QTG totals.

This is a retained-log view, not a provider billing ledger. Missing attribution
cannot be recovered from a key's present policy, its name or a model name.
"""
from __future__ import annotations

import datetime as dt
import math
import time

MAX_COUNT = 2**53 - 1


def key_usage(conn, days: int, realm: str, platform: str, *, now: float | None = None) -> list[dict]:
    if platform not in ('qoder', 'trae') or realm not in ('', 'cn', 'global'):
        raise ValueError('invalid selected key-usage scope')
    if type(days) is not int:
        raise ValueError('invalid selected key-usage window')
    days = max(1, min(90, days))
    today = dt.date.fromtimestamp(time.time() if now is None else now)
    start_day = today - dt.timedelta(days=days - 1)
    end_day = today + dt.timedelta(days=1)
    start = time.mktime(start_day.timetuple())
    end = time.mktime(end_day.timetuple())
    known_tokens = ("l.usage_complete=1 AND typeof(l.prompt_tokens)='integer' "
                    "AND l.prompt_tokens>=0 AND typeof(l.completion_tokens)='integer' "
                    "AND l.completion_tokens>=0")
    known_credit = "typeof(l.credit) IN ('integer','real') AND l.credit>=0"
    known_cache = ("typeof(l.cache_hit_tokens)='integer' AND l.cache_hit_tokens>=0 "
                   "AND typeof(l.cache_miss_tokens)='integer' AND l.cache_miss_tokens>=0 "
                   "AND (l.cache_hit_tokens>0 OR l.cache_miss_tokens>0)")
    speed_sample = (f"({known_tokens}) AND l.status>=200 AND l.status<300 "
                    "AND l.terminal_state='completed' AND l.completion_tokens>0 "
                    "AND typeof(l.latency_ms)='integer' AND l.latency_ms>0 "
                    "AND (l.first_token_ms IS NULL OR (typeof(l.first_token_ms)='integer' "
                    "AND l.first_token_ms>=0)) "
                    "AND l.latency_ms>COALESCE(l.first_token_ms,0)")
    sql = f'''
        SELECT k.id AS key_id, k.name AS name, COUNT(*) AS requests,
          SUM(CASE WHEN {known_tokens} THEN 0 ELSE 1 END) AS token_unknown,
          SUM(CASE WHEN {known_tokens} THEN l.prompt_tokens ELSE 0 END) AS pt,
          SUM(CASE WHEN {known_tokens} THEN l.completion_tokens ELSE 0 END) AS ct,
          SUM(CASE WHEN {known_credit} THEN 0 ELSE 1 END) AS credit_unknown,
          SUM(CASE WHEN {known_credit} THEN l.credit ELSE 0 END) AS credit,
          SUM(CASE WHEN {known_cache} THEN l.cache_hit_tokens ELSE 0 END) AS hit,
          SUM(CASE WHEN {known_cache} THEN l.cache_miss_tokens ELSE 0 END) AS miss,
          SUM(CASE WHEN {known_cache} THEN 1 ELSE 0 END) AS cache_samples,
          SUM(CASE WHEN {speed_sample} THEN l.completion_tokens ELSE 0 END) AS speed_ct,
          SUM(CASE WHEN {speed_sample} THEN l.latency_ms-COALESCE(l.first_token_ms,0)
              ELSE 0 END) AS speed_ms,
          SUM(CASE WHEN {speed_sample} THEN 1 ELSE 0 END) AS speed_samples
        FROM request_logs l INNER JOIN api_keys k ON k.id=l.key_id
        WHERE l.ts>=? AND l.ts<? AND l.actual_platform=?
          AND l.protocol IN ('openai','anthropic','responses')
          {'AND l.realm=?' if realm else ''}
        GROUP BY k.id ORDER BY pt+ct DESC, k.id
    '''
    args = (start, end, platform) + ((realm,) if realm else ())
    # A single SELECT keeps names, deletion, observations and rate samples in
    # one SQLite snapshot. It does not open/migrate a database or write counters.
    result = []
    for raw in conn.execute(sql, args).fetchall():
        row = dict(raw)
        for field in ('requests', 'token_unknown', 'pt', 'ct', 'credit_unknown',
                      'hit', 'miss', 'cache_samples', 'speed_ct', 'speed_ms', 'speed_samples'):
            if type(row[field]) is not int or not 0 <= row[field] <= MAX_COUNT:
                raise ValueError('invalid selected key-usage evidence')
        total = row['pt'] + row['ct']
        if total > MAX_COUNT or row['hit'] + row['miss'] > MAX_COUNT:
            raise ValueError('selected key-usage evidence overflow')
        credit = row['credit']
        credit_known = (row['credit_unknown'] == 0 and type(credit) in (int, float)
                        and math.isfinite(credit) and 0 <= credit <= MAX_COUNT)
        result.append({
            'key_id': row['key_id'], 'name': row['name'] or '', 'requests': row['requests'],
            'prompt_tokens': row['pt'] if row['token_unknown'] == 0 else None,
            'completion_tokens': row['ct'] if row['token_unknown'] == 0 else None,
            'total_tokens': total if row['token_unknown'] == 0 else None,
            'token_unknown_requests': row['token_unknown'],
            'credit': round(credit, 6) if credit_known else None,
            'credit_unknown_requests': row['credit_unknown'],
            'cache_hit_tokens': row['hit'] if row['cache_samples'] else None,
            'cache_miss_tokens': row['miss'] if row['cache_samples'] else None,
            'cache_hit_rate': round(row['hit'] / (row['hit'] + row['miss']), 6)
                if row['cache_samples'] else None,
            'cache_samples': row['cache_samples'],
            'tokens_per_sec': round(row['speed_ct'] * 1000 / row['speed_ms'], 2)
                if row['speed_samples'] else None,
            'throughput_samples': row['speed_samples'],
            'source': 'manager_retained_request_logs', 'platform': platform, 'realm': realm,
            'from': start_day.isoformat(), 'to': today.isoformat(),
        })
    return result


def current_key_usage(days: int, realm: str, platform: str) -> list[dict]:
    from . import db

    with db._lock:
        return key_usage(db.connect(), days, realm, platform)
