#!/usr/bin/env python3
"""Host-side fallback, independent of Manager uptime; no restarts or model calls.

Run only from a verified signed release. Business rules belong to the in-process
steward. This watchdog owns exactly manager readiness and collector liveness.
It never imports the old privileged alert script or its provider credentials.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.request

try:
    import fcntl
except ImportError:
    fcntl = None

RULES = frozenset({'manager_readiness', 'collector_liveness'})
TERMINAL = frozenset({'accepted', 'failed', 'rejected', 'cancelled'})
PUBLIC_POLICY_MAX_AGE = 24 * 3600
ALERT_COOLDOWN_SECONDS = 30 * 60


@dataclass(frozen=True)
class Paths:
    database: Path = Path('/opt/workbuddy-manager/data/manager.db')
    state_dir: Path = Path('/var/lib/zhihui-gateway/steward-watchdog/worker')
    smtp: Path = Path('/etc/zhihui/steward-smtp.json')
    publication_lock: Path = Path('/run/zh-release.lock')


def atomic(path, value):
    temp = path.with_name('.' + path.name + '-' + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink()


def safe_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 524288):
            raise ValueError('unsafe_watchdog_file')
        return json.loads(source.read(524289))


@contextmanager
def owned(paths):
    if fcntl is None:
        raise RuntimeError('watchdog_requires_posix_file_locks')
    directory = paths.state_dir
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError('unsafe_watchdog_directory')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_watchdog_directory')
    fd = os.open(directory / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('unsafe_watchdog_lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def read_control(paths):
    # No schema changes, DB copies or credential reads; no fallback to old config.
    uri = paths.database.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
        connection.execute('PRAGMA query_only=ON')
        rows = dict(connection.execute('SELECT key,value FROM settings WHERE key IN (?,?)',
                    ('steward.policy.v1', 'steward.heartbeat.v1')))
    def parse(key):
        value = rows.get(key)
        if value is None:
            return None
        if len(value) > 131072:
            raise ValueError('watchdog_control_too_large')
        return json.loads(value)
    return {'policy': parse('steward.policy.v1'), 'heartbeat': parse('steward.heartbeat.v1')}


def readiness():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open('http://127.0.0.1:7866/readyz', timeout=3) as response:
            value = json.loads(response.read(257))
            return response.status == 200 and value == {'ok': True}
    except (OSError, ValueError, urllib.error.URLError):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def publishing(paths):
    if fcntl is None:
        raise RuntimeError('watchdog_requires_posix_file_locks')
    try:
        info = paths.publication_lock.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
        raise ValueError('unsafe_publication_lock')
    # The root-owned 0600 lock must not be made readable to the application.
    # Kernel lock metadata is enough for a bounded notification mute, not a
    # publication permit; never acquire or modify the actual authority here.
    with Path('/proc/locks').open('rb') as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('kernel_lock_inventory_too_large')
    identity = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    held = False
    for line in raw.decode('ascii').splitlines():
        fields = line.split()
        if len(fields) != 8 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
            continue
        try:
            major, minor, inode = fields[5].split(':')
            actual = (int(major, 16), int(minor, 16), int(inode))
        except (ValueError, IndexError):
            raise ValueError('invalid_kernel_lock_inventory') from None
        held |= actual == identity
    after = paths.publication_lock.lstat()
    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
        raise ValueError('publication_lock_identity_changed')
    return held


def validate_state(state, now):
    from server.steward_config import Policy, validate_policy
    integer = lambda value: type(value) is int and value >= 0
    if (not isinstance(state, dict) or set(state) - {'schema', 'next_id', 'outbox', 'rules', 'policy', 'policy_at', 'maintenance_since'}
            or type(state.get('schema')) is not int or state['schema'] != 1
            or not integer(state.get('next_id')) or state['next_id'] < 1
            or not isinstance(state.get('outbox'), list) or len(state['outbox']) > 200
            or not isinstance(state.get('rules'), dict) or set(state['rules']) - RULES):
        raise ValueError('invalid_watchdog_state')
    since = state.get('maintenance_since')
    if since is not None and (not integer(since) or since > now + 5):
        raise ValueError('invalid_watchdog_maintenance')
    if 'policy' in state:
        validate_policy(state['policy'])
        if not integer(state.get('policy_at')) or state['policy_at'] > now + 5:
            raise ValueError('invalid_watchdog_policy_time')
    for item in state['rules'].values():
        if (not isinstance(item, dict) or set(item) != {'active', 'bad', 'good', 'generation', 'notified'}
                or type(item.get('active')) is not bool
                or any(not integer(item.get(key)) for key in ('bad', 'good', 'generation'))
                or not isinstance(item.get('notified'), dict) or set(item['notified']) - {'firing', 'recovered'}):
            raise ValueError('invalid_watchdog_rule')
        for recipients in item['notified'].values():
            if (not isinstance(recipients, list) or len(recipients) > 200
                    or any(not isinstance(email, str) for email in recipients)
                    or Policy.emails(recipients) != recipients):
                raise ValueError('invalid_watchdog_notification_history')
    ids, identities = set(), set()
    for item in state['outbox']:
        if (not isinstance(item, dict)
                or set(item) - {'id', 'recipient', 'status', 'attempts', 'next_at', 'created_at', 'code', 'payload', 'generation', 'attempted_at'}
                or not {'id', 'recipient', 'status', 'attempts', 'next_at', 'created_at', 'code', 'payload', 'generation'} <= set(item)
                or not integer(item.get('id')) or not 0 < item['id'] < state['next_id'] or item['id'] in ids
                or not integer(item.get('attempts')) or item['attempts'] > 3
                or any(not integer(item.get(key)) for key in ('next_at', 'created_at', 'generation'))
                or item['status'] not in TERMINAL | {'pending', 'sending', 'unknown'}
                or (item['status'] == 'pending' and item['attempts'] >= 3)
                or not isinstance(item['code'], str) or not re.fullmatch(r'[a-z0-9_]{0,80}', item['code'])
                or not isinstance(item['recipient'], str) or Policy.emails([item['recipient']]) != [item['recipient']]):
            raise ValueError('invalid_watchdog_delivery')
        if 'attempted_at' in item and (not integer(item['attempted_at']) or item['attempts'] < 1):
            raise ValueError('invalid_watchdog_attempt_time')
        payload = item['payload']
        if (not isinstance(payload, dict)
                or set(payload) != {'rule', 'event', 'severity', 'title', 'detail', 'observed_at'}
                or payload.get('rule') not in RULES or payload.get('event') not in ('firing', 'recovered')
                or payload.get('severity') not in ('critical', 'info')
                or any(not isinstance(payload.get(key), str) or len(payload[key]) > 500 for key in ('title', 'detail'))
                or not integer(payload.get('observed_at')) or item['generation'] < 1):
            raise ValueError('invalid_watchdog_payload')
        identity = (payload['rule'], item['generation'], payload['event'], item['recipient'])
        if identity in identities:
            raise ValueError('duplicate_watchdog_delivery')
        ids.add(item['id'])
        identities.add(identity)
    return state


def transition(state, rule, active, *, selected, now):
    rules = state.setdefault('rules', {})
    prior = rules.setdefault(rule, {'active': False, 'bad': 0, 'good': 0, 'generation': 0, 'notified': {}})
    prior['bad' if active else 'good'] = min(prior['bad' if active else 'good'] + 1, 10000)
    prior['good' if active else 'bad'] = 0
    required = selected.consecutive_failures if active else 2
    if prior['bad' if active else 'good'] < required:
        return
    changed = prior['active'] != active
    if changed:
        prior['active'] = active
        if active:
            prior['generation'] += 1
            prior['notified'] = {}
        for item in state['outbox']:
            if item['status'] == 'pending' and item['payload']['rule'] == rule:
                item.update(status='cancelled', code='state_superseded')
    if (not selected.enabled or prior['generation'] == 0
            or (not active and (not selected.send_recovery or not changed))):
        return
    event = 'firing' if active else 'recovered'
    notified = prior['notified'].setdefault(event, [])
    title = '智汇工作台暂不可用' if rule == 'manager_readiness' else '智汇管家采集需要核对'
    detail = ('本机最小就绪探针未通过；不能据此断言三平台推理全部中断。' if rule == 'manager_readiness'
              else 'Manager 就绪，但内置管家的采集心跳未及时更新；业务用量可能仍在正常处理。')
    for recipient in selected.recipients:
        if recipient in notified:
            continue
        if len(notified) >= 200:
            raise ValueError('watchdog_recipient_history_full')
        if len(state['outbox']) >= 200:
            raise ValueError('watchdog_outbox_full')
        ident = state['next_id']
        state['next_id'] += 1
        state['outbox'].append({'id': ident, 'recipient': recipient, 'status': 'pending', 'attempts': 0,
            'generation': prior['generation'],
            'next_at': now, 'created_at': now, 'code': '', 'payload': {'rule': rule,
            'event': event, 'severity': 'critical' if active else 'info',
            'title': title, 'detail': detail if active else '连续两轮取得恢复证据，请在工作台核对当前状态。',
            'observed_at': now}})
        notified.append(recipient)


def run_once(paths, *, probe=readiness, control=read_control, maintenance=publishing, sender=None, now=None):
    from server import steward_config, steward_mail
    now = int(time.time() if now is None else now)
    with owned(paths):
        target = paths.state_dir / 'state.json'
        try:
            state = safe_json(target)
        except FileNotFoundError:
            state = {'schema': 1, 'next_id': 1, 'outbox': [], 'rules': {}}
        validate_state(state, now)
        for item in state['outbox']:
            if item['status'] == 'sending':
                item.update(status='unknown', code='watchdog_interrupted')
        state['outbox'] = [item for item in state['outbox'] if item['status'] not in TERMINAL
                           or now - max(item['created_at'], item.get('attempted_at', item['next_at'])) < 7 * 86400]
        try:
            facts = control(paths)
        except (OSError, sqlite3.Error):
            age = now - state.get('policy_at', -PUBLIC_POLICY_MAX_AGE)
            current_cache = 0 <= age <= PUBLIC_POLICY_MAX_AGE
            selected = steward_config.validate_policy(state.get('policy', {}) if current_cache else {})
            facts, control_current = {}, False
        else:
            if not isinstance(facts, dict) or set(facts) != {'policy', 'heartbeat'}:
                raise ValueError('invalid_watchdog_control')
            selected = steward_config.validate_policy(facts['policy'] if facts['policy'] is not None else {})
            # Only public settings are cached, never SMTP or model credentials.
            state['policy'] = selected.model_dump()
            state['policy_at'] = now
            control_current = True
        held = maintenance(paths)
        if type(held) is not bool:
            raise ValueError('invalid_watchdog_maintenance_probe')
        since = state.get('maintenance_since')
        state['maintenance_since'] = (now if since is None else since) if held else None
        muted = held and state['maintenance_since'] is not None and now - state['maintenance_since'] < 900
        if not muted:
            ready = probe()
            transition(state, 'manager_readiness', not ready, selected=selected, now=now)
            if ready:
                heart = facts.get('heartbeat')
                stamp = heart.get('at') if isinstance(heart, dict) else None
                fresh = type(stamp) is int and -5 <= now - stamp <= max(180, selected.interval_seconds + 75)
                transition(state, 'collector_liveness', not fresh, selected=selected, now=now)
        else:
            for row in state['rules'].values():
                row['bad'], row['good'] = 0, 0
        for item in state['outbox']:
            if item['status'] == 'pending' and (not selected.enabled or item['recipient'] not in selected.recipients):
                item.update(status='cancelled', code='policy_changed')
        validate_state(state, now)
        atomic(target, state)
        sent = 0
        for item in state['outbox']:
            if muted or sent >= 3 or item['status'] != 'pending' or item['next_at'] > now:
                continue
            if item['payload']['event'] == 'firing' and any(
                    old['id'] != item['id'] and old['attempts'] > 0
                    and old['recipient'] == item['recipient'] and old['payload']['event'] == 'firing'
                    and old['payload']['rule'] == item['payload']['rule']
                    and now - old.get('attempted_at', max(old['created_at'], old['next_at'])) < ALERT_COOLDOWN_SECONDS
                    for old in state['outbox']):
                item.update(status='cancelled', code='alert_cooldown_30m')
                atomic(target, state)
                continue
            item.update(status='sending', attempts=item['attempts'] + 1, attempted_at=now)
            atomic(target, state)  # Durable before any external effect.
            try:
                raw = json.loads(steward_config.secret_file(paths.smtp))
                # Same fixed endpoint and identity validator as the application.
                transport = steward_config.validate_smtp(raw)
            except Exception:
                outcome = {'status': 'retry', 'code': 'watchdog_notification_unavailable'}
            else:
                # A sequential SMTP batch can take over two minutes. A policy
                # removed in the meantime must not keep sending to old targets.
                try:
                    latest = control(paths)
                except (OSError, sqlite3.Error):
                    latest = None
                if latest is not None:
                    if not isinstance(latest, dict) or set(latest) != {'policy', 'heartbeat'}:
                        raise ValueError('invalid_watchdog_control')
                    selected = steward_config.validate_policy(latest['policy'] if latest['policy'] is not None else {})
                    state['policy'], state['policy_at'] = selected.model_dump(), now
                rule = state['rules'].get(item['payload']['rule'])
                if (not selected.enabled or item['recipient'] not in selected.recipients
                        or not rule or rule['generation'] != item['generation']
                        or rule['active'] != (item['payload']['event'] == 'firing')
                        or item['payload']['event'] == 'recovered' and not selected.send_recovery):
                    outcome = {'status': 'cancelled', 'code': 'policy_or_state_changed'}
                else:
                    try:
                        outcome = (sender or steward_mail.send)(transport, item['recipient'], item['payload'],
                                                               delivery_id='watchdog-' + str(item['id']))
                    except Exception:
                        outcome = {'status': 'unknown', 'code': 'watchdog_transport_outcome_unknown'}
            if (not isinstance(outcome, dict) or outcome.get('status') not in ('accepted', 'unknown', 'rejected', 'retry', 'cancelled')
                    or not isinstance(outcome.get('code', ''), str)
                    or not re.fullmatch(r'[a-z0-9_]{0,80}', outcome.get('code', ''))):
                raise ValueError('invalid_watchdog_transport_result')
            status = outcome['status']
            retry = status == 'retry' and item['attempts'] < 3
            item.update(status='pending' if retry else 'failed' if status == 'retry' else status,
                        code=outcome.get('code', '')[:80], next_at=now + (60 if item['attempts'] == 1 else 300))
            atomic(target, state)
            sent += 1
        atomic(paths.state_dir / 'heartbeat.json', {'at': now, 'control_current': control_current,
            'maintenance': muted, 'pending': sum(item['status'] == 'pending' for item in state['outbox']),
            'unknown': sum(item['status'] == 'unknown' for item in state['outbox']), 'schema': 1})
        return {'ok': True, 'processed': sent, 'control_current': control_current, 'maintenance': muted}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / 'deploy'))
    import source_preflight
    source_preflight.check(root)
    sys.path.insert(0, str(root))
    result = run_once(Paths())
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # A corrupt state is never reset, and uncertain sends are never replayed.
        print('zhihui steward watchdog unavailable; inspect protected local evidence', file=sys.stderr)
        raise SystemExit(1)
