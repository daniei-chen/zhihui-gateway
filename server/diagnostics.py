import argparse
from collections import Counter
import json
import re
from pathlib import Path
import sqlite3
import time


#: 早期用过的上游错误码。保留它只是为了让旧库的离线分析结果不变；
#: 判断「能不能记」现在靠下面的格式约束，不再靠这张表。
_KNOWN_CODES = {'11128', '11148', '11140', '11101', '11135', '12153', '14017',
                'channel_check_rejected', 'upstream_transport_uncertain', 'upstream_error',
                'client_cancelled', 'first_byte_timeout', 'stream_idle_timeout',
                'upstream_timeout', 'upstream_rejected', 'protocol_incomplete', 'rate_limit_exceeded',
                'provider_attempt_budget_exhausted'}

#: `error_code` 的安全形状。
#:
#: ## 为什么从白名单改成格式约束
#:
#: 2026-11-15 实测：库里实际出现过 23 种错误码，**白名单只认识其中 4 种**，其余 19 种
#: 全被归成 `other` —— 包括出现次数最多的 `gateway.auth.denied`（6593 次）。
#:
#: 也就是说，离线诊断报告的「错误码分布」几乎全是 `other`，**那份报告没有信息量**。
#:
#: 根因是白名单永远滞后于命名演进：错误码从早期的数字码与裸名字，扩展成了
#: `gateway.*` / `portal_*` 命名空间。往后还会有新的。
#:
#: ## 格式约束能防什么
#:
#: 这个过滤器的本意是**不让任意字符串进报告**（可能带用户输入）。格式约束同样做得到：
#: 限定字符集、限定长度、限定形态 —— 任何含空格、引号、斜杠、换行的东西仍然进不来。
#:
#: 而错误码是我们自己与上游共同产出的**结构化标识**，形状是可枚举的。
_SAFE_ERROR_CODE = re.compile(
    r'^(?:'
    r'gateway\.[a-z][a-z0-9._]{0,48}'      # gateway.auth.denied / gateway.transport.error
    r'|portal_[a-z][a-z0-9_]{0,40}'        # portal_wallet_pending
    # 裸名字按「段」拼：每段字母开头、可带下划线与数字，段间单个点。
    # `client.cancelled` 是这个形状（659 次）；`a..b` 这类连续点号进不来。
    # 单段上限 40：真实最长的单段码是 `provider_attempt_budget_exhausted`（32 字符）。
    r'|[a-z][a-z0-9_]{0,40}(?:\.[a-z][a-z0-9_]{0,40}){0,4}'
    r'|\d{3,6}'                            # 上游数字码：11128 / 10605 / 503
    r')$')
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
        # 白名单里的老码一律放行；其余按格式判断 —— 见 `_SAFE_ERROR_CODE` 的说明。
        # 只认白名单会让 19/23 的真实码变成 `other`，报告就没有信息量了。
        text = str(value)
        if text in _KNOWN_CODES:
            return value
        # 长度上限单独挡一道：正则里的分段上限组合起来仍可能很长，
        # 而真实错误码没有超过 40 字符的。这里先挡掉，免得靠正则算总长。
        if not 3 <= len(text) <= 64:
            return 'other'
        return value if _SAFE_ERROR_CODE.fullmatch(text) else 'other'
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
        # 认证失败是**首要**原因：401/403 下不管错误码写的是什么，先让人去查认证。
        #
        # 2026-11-15 发现：此前这个判定会被后面的 elif 链覆盖 —— 一个 403 如果恰好
        # 带着 `invalid_messages`，诊断会建议「改请求体」，把人引到完全错误的方向。
        #
        # 保留那个已知特例：上游有时用 403 表达「渠道拒绝」（见下面的分支），
        # 那不是调用方的认证问题。除它之外，401/403 直接给出结论。
        if code not in ('channel_check_rejected', 'upstream.channel_check_rejected'):
            return result
    if code in ('channel_check_rejected', 'upstream.channel_check_rejected'):
        result.update(channel='rejected', account_availability_changed=False, action='review_scoped_protocol_compatibility')
    elif code == '11128':
        result.update(action='inspect_11128_message_before_classification')
    elif code == 'provider_attempt_budget_exhausted':
        result.update(action='inspect_bounded_attempt_timeline_without_replay')
    elif code in ('model_not_found', 'unsupported_model'):
        result.update(model_access='rejected', action='verify_model_mapping')
    elif code in ('invalid_messages', 'invalid_model', 'prompt_too_long'):
        # 请求体本身不成立 —— 不是认证、配额或上游的问题，改请求就能过。
        result.update(action='fix_request_body')
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
