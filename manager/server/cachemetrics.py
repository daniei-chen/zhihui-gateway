"""Request cache incidence, distinct from token-weighted cache efficiency."""
from __future__ import annotations


def request_rates(query, since: int, realm: str | None, dimension: str,
                  *, normalize_models: bool = True) -> dict:
    model = "CASE WHEN model LIKE 'cn:%' THEN substr(model, 4) ELSE model END" if normalize_models else 'model'
    group = {'model': model, 'key': 'key_id'}[dimension]
    rows = query(
        f'SELECT {group}, SUM(CASE WHEN cache_hit_tokens>0 THEN 1 ELSE 0 END), COUNT(*) '
        'FROM request_logs WHERE ts>=? '
        "AND protocol IN ('openai','anthropic','responses') "
        "AND typeof(cache_hit_tokens)='integer' AND cache_hit_tokens>=0 "
        "AND typeof(cache_miss_tokens)='integer' AND cache_miss_tokens>=0 "
        'AND (cache_hit_tokens>0 OR cache_miss_tokens>0) '
        + ('AND realm=? ' if realm else '') + f'GROUP BY {group}',
        (since,) + ((realm,) if realm else ()),
    )
    return {(row[0] or '未知') if dimension == 'model' else row[0]: {
        'cache_hit_requests': int(row[1]),
        'cache_observed_requests': int(row[2]),
        'cache_request_hit_rate': round(row[1] / row[2], 6),
    } for row in rows}


UNKNOWN_REQUEST_RATE = {
    'cache_hit_requests': 0, 'cache_observed_requests': 0, 'cache_request_hit_rate': None,
}
