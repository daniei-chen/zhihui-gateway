import base64
import json
import time

from . import budget, db, providerevents


BUCKETS = ('all', 'new_1h', 'older_1h', 'older_7d', 'upstream_rejected', 'archived_pending')


def _decode(cursor):
    if not cursor:
        return None
    try:
        if len(cursor) > 256:
            raise ValueError('cursor too long')
        decoded = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        stamp, request_id = decoded
        if (type(stamp) is not int or not 0 <= stamp <= 9223372036854775807
                or not isinstance(request_id, str) or not providerevents.IDENTITY.fullmatch(request_id)):
            raise ValueError('invalid cursor')
        return stamp, request_id
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('invalid review cursor') from exc


def _encode(stamp, request_id):
    return base64.urlsafe_b64encode(json.dumps([stamp, request_id], separators=(',', ':')).encode()).decode().rstrip('=')


def page(*, limit=25, bucket='all', cursor=None, now=None):
    if bucket not in BUCKETS:
        raise ValueError('invalid review bucket')
    now = int(time.time()) if now is None else int(now)
    limit = max(1, min(100, int(limit)))
    position = _decode(cursor)
    clauses, arguments = ["state='unknown'"], []
    if bucket == 'new_1h':
        clauses.append('created_at>=?')
        arguments.append(now - 3600)
    elif bucket in ('older_1h', 'older_7d'):
        clauses.append('created_at<?')
        arguments.append(now - (3600 if bucket == 'older_1h' else 7 * 86400))
    if position is not None:
        clauses.append('(created_at,request_id)>(?,?)')
        arguments.extend(position)
    rows = [dict(row) for row in db.query(
        'SELECT * FROM budget_reservations WHERE ' + ' AND '.join(clauses) +
        ' ORDER BY created_at,request_id LIMIT 501', tuple(arguments))]
    logs = budget._logs_for([row['request_id'] for row in rows[:500]])
    items, scanned, last = [], 0, None
    for row in rows[:500]:
        scanned += 1
        last = (row['created_at'], row['request_id'])
        record = logs.get(row['request_id'])
        classification = budget._classify(row, record)
        archived = budget._is_archived(row) and classification in ('no_request_log', 'client_cancelled', 'upstream_rejected', 'unexplained')
        if bucket == 'upstream_rejected' and classification != bucket:
            continue
        if bucket == 'archived_pending' and not archived:
            continue
        items.append({'request_id': row['request_id'], 'key_id': row['key_id'],
                      'created_at': row['created_at'], 'age_seconds': max(0, now - row['created_at']),
                      'state': row['state'], 'class': classification, 'archived_pending': archived,
                      'reserved_tokens_estimate': row['reserved_tokens'],
                      'charged_tokens': row['charged_tokens'], 'charged_credit': row['charged_credit'],
                      'has_request_log': record is not None,
                      'has_provider_usage': bool(record is not None and (
                          record['credit'] is not None or record['usage_event_count'] > 0)),
                      'wallet_frozen': None})
        if len(items) >= limit:
            break
    more = last is not None and (scanned < len(rows) or len(rows) > 500)
    return {'items': items, 'bucket': bucket, 'limit': limit, 'scanned_rows': scanned,
            'next_cursor': _encode(*last) if more else None,
            'usage_unknown_is_zero': False, 'bulk_resolution_supported': False,
            'scope': 'main-supplier-usage-not-customer-wallet', 'sampled_at': now}


def evidence(request_id):
    if not isinstance(request_id, str) or not providerevents.IDENTITY.fullmatch(request_id):
        raise ValueError('invalid request_id')
    reservation = db.query_one('SELECT * FROM budget_reservations WHERE request_id=?', (request_id,))
    if reservation is None:
        raise LookupError('budget reservation not found')
    events = db.query('SELECT event_id,ts,event_type,provenance,terminal_state,counted,'
                      'prompt_tokens,completion_tokens,credit_units FROM usage_events '
                      'WHERE request_id=? ORDER BY ts,event_id LIMIT 101', (request_id,))
    ended = db.query_one('SELECT key_id,owner,ended_at FROM request_lifetime_ends_v2 WHERE request_id=?', (request_id,))
    return {'request_id': request_id, 'key_id': reservation['key_id'], 'state': reservation['state'],
            'scope': 'main-supplier-usage-not-customer-wallet', 'wallet_frozen': None,
            'transport_end': None if ended is None else {
                'ended_at': ended['ended_at'],
                'matches_reservation': ended['key_id'] == reservation['key_id'] and ended['owner'] == reservation['owner']},
            'usage_events': [dict(event) for event in events[:100]],
            'usage_events_truncated': len(events) > 100,
            'provider': providerevents.report(request_id),
            'resolution_requirements': ['recent-interactive-admin-session', 'reason-and-evidence-minimum-eight-characters',
                                        'transport-ended-or-owner-proven-dead', 'no-spool-or-usage-conflict'],
            'can_infer_zero_from_cancellation': False, 'sampled_at': int(time.time())}
