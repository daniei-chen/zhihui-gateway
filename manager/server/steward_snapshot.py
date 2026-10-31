"""Bounded, typed projections for both human diagnostics and model context.

Never pass account dictionaries, raw logs, prompts, environment or config blobs
to a model. A failed collector is unknown, not an empty healthy account pool.
"""
from __future__ import annotations

import asyncio
import json
import math
import shutil
import time

from . import config, db, operational
from .services.asyncwork import joined_thread


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def resources():
    disk = shutil.disk_usage(config.DATA_DIR)
    memory = {'total_bytes': None, 'available_bytes': None, 'used_percent': None}
    try:
        fields = {}
        with open('/proc/meminfo', encoding='ascii') as source:
            for line in source:
                key, raw = line.split(':', 1)
                if key in ('MemTotal', 'MemAvailable'):
                    fields[key] = int(raw.strip().split()[0]) * 1024
        total, available = fields['MemTotal'], fields['MemAvailable']
        memory = {'total_bytes': total, 'available_bytes': available,
                  'used_percent': round(100 * (total - available) / total, 1)}
    except (OSError, ValueError, KeyError):
        pass
    return {'disk': {'total_bytes': disk.total, 'used_bytes': disk.used,
                     'free_bytes': disk.free, 'used_percent': round(100 * disk.used / disk.total, 1)},
            'memory': memory}


def certificate_fact(now):
    # Consume only the fixed public certbot receipt. Do not expose domain/path
    # text, scan unrelated certificates or read any TLS private key.
    from pathlib import Path
    path = Path('/var/lib/zhihui-gateway/cert-status.json')
    try:
        with path.open('rb') as source:
            raw = source.read(8193)
        value = json.loads(raw) if len(raw) <= 8192 else None
        expires = value.get('not_after') if isinstance(value, dict) else None
        generated = value.get('generated_at') if isinstance(value, dict) else None
        if type(expires) is not int or expires <= 0 or type(generated) is not int or not 0 < generated <= now + 300:
            raise ValueError('invalid_certificate_receipt')
        age = max(0, now - generated)
        return {'status': 'ok' if age <= 7 * 86400 else 'stale',
                'not_after': expires, 'days_left': (expires - now) // 86400,
                'age_seconds': age, 'served_chain_verified': False}
    except (OSError, ValueError, TypeError):
        return {'status': 'unknown', 'not_after': None, 'days_left': None,
                'age_seconds': None, 'served_chain_verified': False}


def local_facts(now=None):
    now = int(time.time() if now is None else now)
    traffic = db.query_one(
        "SELECT COUNT(*) n,SUM(CASE WHEN terminal_state IN ('upstream_error','transport_error',"
        "'protocol_incomplete','application_error','interrupted') THEN 1 ELSE 0 END) failed "
        "FROM request_outcomes WHERE ts>=? AND protocol!='http' AND terminal_state!='rejected'", (now - 300,))
    samples = [row['latency_ms'] for row in db.query(
        "SELECT latency_ms FROM request_logs WHERE ts>=? AND status BETWEEN 200 AND 299 "
        'ORDER BY ts DESC,id DESC LIMIT 500', (now - 300,)) if number(row['latency_ms']) is not None]
    samples.sort()
    p95 = samples[max(0, math.ceil(len(samples) * .95) - 1)] if samples else None
    unknown = db.query_one("SELECT COUNT(*) n,MIN(created_at) oldest FROM budget_reservations WHERE state='unknown'")
    key_counts = db.query_one('SELECT COUNT(*) total,SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) enabled FROM api_keys')
    from . import ledgerreplay, product
    channel = ledgerreplay.channel_state()
    backup = {'status': 'unknown', 'age_seconds': None, 'restore_verified': False}
    try:
        path = config.BACKUP_STATUS_FILE
        with path.open('rb') as source:
            raw = source.read(65537)
        record = json.loads(raw) if len(raw) <= 65536 else None
        age = now - path.stat().st_mtime
        if isinstance(record, dict) and record.get('restore_check') == 'ok' and -300 <= age and type(record.get('bytes')) is int and record['bytes'] > 0:
            backup = {'status': 'ok' if age <= 26 * 3600 else 'stale',
                      'age_seconds': max(0, int(age)), 'restore_verified': True}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    n, failed = int(traffic['n']), int(traffic['failed'] or 0)
    classified = db.query_one(
        "SELECT SUM(CASE WHEN error_code IN ('channel_check_rejected','11128','11-128') THEN 1 ELSE 0 END) channel,"
        "SUM(CASE WHEN error_source='upstream' AND (http_status=401 OR error_code IN "
        "('session_dead','invalid_grant','upstream_unauthorized')) THEN 1 ELSE 0 END) auth,"
        "SUM(CASE WHEN error_code IN ('no_healthy_account','11102') THEN 1 ELSE 0 END) pool "
        "FROM request_outcomes WHERE ts>=? AND protocol!='http' AND terminal_state!='completed'", (now - 300,))
    return {'observed_at': now, 'resources': resources(),
            'traffic': {'window_seconds': 300, 'requests': n, 'failed': failed,
                        'failure_percent': round(100 * failed / n, 2) if n else None,
                        'channel_rejections': int(classified['channel'] or 0),
                        'upstream_auth_failures': int(classified['auth'] or 0),
                        'pool_unavailable': int(classified['pool'] or 0),
                        'p95_total_latency_ms': p95, 'latency_samples': len(samples),
                        'latency_note': '最近最多500条成功请求的总耗时，不是首字延迟'},
            'budget': {'unknown_count': int(unknown['n']), 'oldest_at': unknown['oldest'],
                       'note': '待核对不是已计费，也不是零用量；未归档和已归档均计入'},
            'keys': {'total': int(key_counts['total']), 'enabled': int(key_counts['enabled'] or 0)},
            'ledger': {k: channel.get(k) for k in ('state', 'pending', 'dead', 'writable', 'checked_at')},
            'backup': backup, 'certificate': certificate_fact(now),
            'release': {'version': product.identity()['version'],
                                         'release_id': operational._release_id()},
            'limits': {'no_generation_probe': True, 'no_credentials_in_context': True,
                       'offsite_restore_proven': False}}


def pool_projection(body, platform='qoder'):
    if not isinstance(body, dict):
        raise ValueError('invalid_pool_response')
    rows = body.get('accounts')
    if (not isinstance(rows, list) or len(rows) > 10000
            or any(not isinstance(row, dict) for row in rows)):
        raise ValueError('invalid_pool_response')
    enabled_values = []
    credits = []
    for row in rows:
        if platform == 'workbuddy':
            flags = (row.get('disabled'), row.get('manual_disabled'))
            if all(type(flag) is bool for flag in flags):
                enabled_values.append(not any(flags))
            credits.append(number(row.get('credits')))
        else:
            if type(row.get('enabled')) is bool:
                enabled_values.append(row['enabled'])
            raw = row.get('credits')
            credits.append(number(row.get('remaining_credits')) if platform == 'trae'
                           else number(raw.get('remain')) if isinstance(raw, dict) else None)
    enabled = sum(enabled_values) if len(enabled_values) == len(rows) else None
    finite = [value for value in credits if value is not None]
    return {'total': len(rows), 'enabled': enabled,
            'credits_remain': sum(finite) if finite and math.isfinite(sum(finite)) else None,
            'credits_coverage': len(finite), 'healthy_accounts': None,
            'scope': body.get('scope') if body.get('scope') in ('engine_loaded_pool', 'engine_account_list')
                     else 'engine_account_list'}


async def platform_fact(name, adapter):
    if adapter is None:
        return {'name': name, 'state': 'unknown', 'reason': 'adapter_unavailable', 'pool': None}
    try:
        async def collect():
            pool = getattr(adapter, 'diagnostic_pool', None) or adapter.accounts
            results = await asyncio.gather(adapter.health(), pool(), return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                raise ValueError('invalid_platform_evidence')
            health, body = results
            if not isinstance(health, dict) or type(health.get('up')) is not bool:
                raise ValueError('invalid_health_contract')
            return {'name': name, 'state': 'up' if health['up'] else 'down',
                    'latency_ms': number(health.get('latency_ms')), 'pool': pool_projection(body, name)}
        return await asyncio.wait_for(collect(), timeout=6)
    except Exception:
        return {'name': name, 'state': 'unknown', 'reason': 'collector_failed_or_timed_out', 'pool': None}


async def collect(runtime):
    local = await joined_thread(local_facts)
    platforms = await asyncio.gather(*(platform_fact(name, getattr(runtime, attribute, None))
                                       for name, attribute in (('workbuddy', 'wb'), ('qoder', 'qoder'), ('trae', 'trae'))))
    return {**local, 'platforms': platforms}


def recent_failure_projection():
    # Stable classification only; upstream error bodies and identities are untrusted.
    rows = db.query('SELECT error_source,error_code,COUNT(*) n FROM request_outcomes '
                    "WHERE ts>=? AND terminal_state!='completed' GROUP BY error_source,error_code ORDER BY n DESC LIMIT 12",
                    (int(time.time()) - 3600,))
    import re
    safe = lambda x: x if isinstance(x, str) and re.fullmatch(r'[a-z0-9_.:-]{1,80}', x) else 'unclassified'
    return [{'source': safe(row['error_source']), 'code': safe(row['error_code']), 'count': row['n']} for row in rows]


def security_projection():
    now = int(time.time())
    return {'observed_at': now, 'window_seconds': 3600,
            'blocked_accesses': db.query_one('SELECT COUNT(*) n FROM ip_access_logs WHERE ts>=? AND blocked=1', (now - 3600,))['n'],
            'source_failure_records': db.query_one('SELECT COUNT(*) n FROM login_fails_ip WHERE updated>=?', (now - 3600,))['n'],
            'identity_failure_records': db.query_one('SELECT COUNT(*) n FROM login_fails_user WHERE updated>=?', (now - 3600,))['n'],
            'ip_rules': {row['kind']: row['n'] for row in db.query('SELECT kind,COUNT(*) n FROM ip_rules GROUP BY kind')
                         if row['kind'] in ('blacklist', 'whitelist', 'allow', 'deny')},
            'admin_actions': db.query_one('SELECT COUNT(*) n FROM audit_logs WHERE ts>=?', (now - 3600,))['n'],
            'note': '仅总量，不含用户名、IP、用户代理、审计正文、凭据或登录失败密码'}


def task_projection():
    from .services import dailyplan, renew
    now = int(time.time())
    daily = dailyplan.get_plan()
    renewal = renew.get_plan()
    task_counts = [{'state': row['level'] if row['level'] in ('ok', 'error', 'warn', 'failed', 'warning') else 'other',
                    'count': row['n']} for row in db.query(
                        'SELECT level,COUNT(*) n FROM task_logs WHERE ts>=? GROUP BY level', (now - 86400,))]
    return {'observed_at': now, 'window_seconds': 86400, 'results': task_counts,
            'plans': {name: {'enabled': value.get('enabled') is True,
                             'hour': value.get('hour') if type(value.get('hour')) is int else None}
                      for name, value in daily.items() if name in dailyplan.TASKS and isinstance(value, dict)},
            'token_renewal': {'enabled': renewal.get('enabled') is True,
                             'interval_days': renewal.get('interval_days') if type(renewal.get('interval_days')) is int else None},
            'note': '读取本机计划和历史分类，不启动任务、不提取脚本、不检查容器、不触发签到或续期'}


def configuration_projection():
    from .routers import gateway
    from . import drain
    limits = gateway.max_body_bytes()
    metrics = db.transaction_stats()
    return {'observed_at': int(time.time()), 'draining': drain.is_draining(),
            'background_jobs_enabled': config.BACKGROUND_JOBS,
            'application_body_limit_bytes': limits,
            'database': {key: value for key, value in metrics.items()
                         if key in ('transactions', 'busy_errors', 'lock_wait_total_ms', 'lock_wait_max_ms',
                                    'begin_max_ms', 'busy_timeout_ms')},
            'note': '仅应用生效参数，不含环境变量、账号文件、原始配置或秘密；边缘限额另行验证'}
