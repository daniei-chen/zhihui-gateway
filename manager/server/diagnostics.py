import argparse
from collections import Counter
import json
import re
from pathlib import Path
import sqlite3
import time


_KNOWN_CODES = {'11128', '11148', '11140', '11101', '11135', '12153', '14017',
                'channel_check_rejected', 'upstream_transport_uncertain', 'upstream_error',
                'client_cancelled', 'first_byte_timeout', 'stream_idle_timeout',
                'upstream_timeout', 'upstream_rejected', 'protocol_incomplete', 'rate_limit_exceeded',
                'provider_attempt_budget_exhausted'}
_ENUMS = {'terminal_state': {'completed', 'rejected', 'upstream_error', 'transport_error',
                            'protocol_incomplete', 'application_error', 'client_cancelled', 'interrupted'},
          'protocol': {'chat', 'responses', 'messages', 'anthropic', 'openai', 'other'},
          'traffic_class': {'inference', 'admin', 'probe', 'other'}}


def _metadata(name, value):
    if value is None:
        return None
    if name in ('status', 'http_status'):
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else 'other'
    if name in _ENUMS:
        return value if value in _ENUMS[name] else 'other'
    if name == 'error_code':
        return value if str(value) in _KNOWN_CODES else 'other'
    if name == 'release_id':
        return value if re.fullmatch(r'v\d{1,3}\.\d{1,3}\.\d{1,3}(?:\+(?:stable|audit|portal)\.\d{8}\.\d{1,4})?', str(value)) else 'other'
    return 'other'


def inspect(path: Path, *, minutes=60, max_rows=1000):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('an existing regular database is required')
    minutes = max(1, min(1440, int(minutes)))
    max_rows = max(1, min(5000, int(max_rows)))
    started = time.monotonic()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() - started > 2), 1000)
    try:
        connection.execute('PRAGMA query_only=ON')
        schema = int(connection.execute('PRAGMA user_version').fetchone()[0])
        available = {row[1] for row in connection.execute('PRAGMA table_info(request_outcomes)')}
        wanted = ('status', 'http_status', 'terminal_state', 'error_code', 'protocol', 'release_id', 'traffic_class')
        selected = [name for name in wanted if name in available]
        counts = Counter()
        sampled = 0
        if selected and 'ts' in available:
            rows = connection.execute('SELECT ' + ','.join(selected) + ' FROM request_outcomes '
                                      'WHERE ts>=? ORDER BY ts DESC LIMIT ?',
                                      (int(time.time()) - minutes * 60, max_rows))
            for row in rows:
                values = tuple(_metadata(name, row[name]) for name in selected)
                counts[values] += 1
                sampled += 1
        unknown = connection.execute("SELECT state,COUNT(*) count FROM budget_reservations "
                                     "WHERE state IN ('reserved','forwarded','unknown') GROUP BY state").fetchall()
        wal = path.with_name(path.name + '-wal')
        return {'schema_version': schema, 'sampled_at': int(time.time()), 'window_minutes': minutes,
                'sample_limit': max_rows, 'sampled_outcomes': sampled, 'possibly_truncated': sampled == max_rows,
                'groups': [{**dict(zip(selected, values)), 'count': count} for values, count in counts.most_common(64)],
                'historical_diagnoses': [
                    {'count': count, 'layers': classify(
                        http_status=dict(zip(selected, values)).get('http_status'),
                        error_code=dict(zip(selected, values)).get('error_code'),
                        terminal_state=dict(zip(selected, values)).get('terminal_state'))}
                    for values, count in counts.most_common(64)],
                'groups_truncated': len(counts) > 64,
                'unresolved_main_budget': {row['state']: row['count'] for row in unknown},
                'wallet_frozen': None, 'unknown_is_zero': False,
                'database_bytes': path.stat().st_size, 'wal_bytes': wal.stat().st_size if wal.exists() else 0,
                'contains_credentials_or_raw_requests': False, 'read_only': True,
                'inference_health_proven': False, 'duration_ms': round((time.monotonic() - started) * 1000, 2)}
    finally:
        connection.close()


def classify(*, reachable=None, ready=None, http_status=None, error_code=None, terminal_state=None):
    code = str(error_code or '')[:128].lower()
    result = {'endpoint_reachable': reachable, 'ready': ready, 'authentication': 'unknown',
              'model_access': 'unknown', 'quota': 'unknown', 'channel': 'unknown',
              'stream': 'unknown', 'inference_proven': terminal_state == 'completed',
              'account_availability_changed': None, 'action': 'inspect_bounded_evidence'}
    if http_status in (401, 403):
        result.update(authentication='rejected', action='verify_authentication_source')
    if code in ('channel_check_rejected', 'upstream.channel_check_rejected'):
        result.update(channel='rejected', account_availability_changed=False, action='review_scoped_protocol_compatibility')
    elif code == '11128':
        result.update(action='inspect_11128_message_before_classification')
    elif code == 'provider_attempt_budget_exhausted':
        result.update(action='inspect_bounded_attempt_timeline_without_replay')
    elif code in ('model_not_found', 'unsupported_model'):
        result.update(model_access='rejected', action='verify_model_mapping')
    elif code in ('quota_exceeded', 'insufficient_quota'):
        result.update(quota='rejected', action='verify_quota_evidence')
    elif terminal_state in ('protocol_incomplete', 'transport_error', 'client_cancelled'):
        result.update(stream=terminal_state, action='inspect_stream_terminal_evidence')
    elif terminal_state == 'completed':
        result.update(stream='completed', action='none')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bounded metadata-only offline gateway diagnostics; never performs inference')
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--minutes', type=int, default=60)
    parser.add_argument('--max-rows', type=int, default=1000)
    options = parser.parse_args(argv)
    try:
        result = inspect(options.db, minutes=options.minutes, max_rows=options.max_rows)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__, 'read_only': True}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
