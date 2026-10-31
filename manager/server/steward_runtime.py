"""One lifetime worker lease; slow SMTP/AI work never blocks inference/event loop."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time

from . import config, db, steward_ai, steward_config, steward_mail, steward_rules, steward_snapshot, steward_store
from .services.asyncwork import joined_task, joined_thread

log = logging.getLogger('zhihui.steward')
SNAPSHOT_KEY = 'steward.snapshot.v1'
HEARTBEAT_KEY = 'steward.heartbeat.v1'
STREAK_KEY = 'steward.streaks.v1'


def snapshot():
    value = db.get_setting(SNAPSHOT_KEY)
    if not isinstance(value, dict) or type(value.get('observed_at')) is not int:
        return {'available': False, 'stale': True, 'snapshot': None, 'age_seconds': None}
    raw_age = int(time.time()) - value['observed_at']
    age = max(0, raw_age)
    interval = value.get('collection_interval_seconds', 60)
    interval = interval if type(interval) is int and 30 <= interval <= 300 else 60
    return {'available': True, 'stale': age > max(180, interval + 75) or raw_age < -5,
            'age_seconds': age, 'snapshot': value}


class StewardRuntime:
    def __init__(self, upstream_runtime):
        self.upstream_runtime = upstream_runtime
        self.owner = secrets.token_hex(16)
        self.tasks = []
        self._fd = None
        self.streaks = {}

    async def start(self):
        await joined_thread(steward_store.ensure_schema)
        if not config.BACKGROUND_JOBS:
            return
        if os.name != 'posix':
            log.warning('steward background requires Linux; read-only UI remains available')
            return
        import fcntl
        path = config.DATA_DIR / '.steward-worker.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            import stat
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError('unsafe_steward_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        await joined_thread(steward_store.recover_interrupted)
        saved = await joined_thread(db.get_setting, STREAK_KEY)
        if (isinstance(saved, dict) and type(saved.get('at')) is int
                and 0 <= time.time() - saved['at'] <= 600 and isinstance(saved.get('rules'), dict)):
            self.streaks = {key: tuple(value) for key, value in saved['rules'].items()
                            if (isinstance(key, str) and isinstance(value, list) and len(value) == 2
                                and type(value[0]) is bool and type(value[1]) is int and 0 < value[1] <= 10000)}
        self.tasks = [asyncio.create_task(self.monitor(), name='zhihui-steward-monitor'),
                      asyncio.create_task(self.deliver(), name='zhihui-steward-delivery'),
                      asyncio.create_task(self.analyze(), name='zhihui-steward-analysis')]

    async def close(self):
        # Do not cancel an in-progress SMTP DATA operation: bounded thread finish
        # precedes releasing the singleton lease. Unknown rows are never replayed.
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def observe(self, current, selected):
        for signal in steward_rules.evaluate(current, selected):
            if signal.active is None:
                self.streaks.pop(signal.rule, None)
                continue
            previous = self.streaks.get(signal.rule, (None, 0))
            count = previous[1] + 1 if previous[0] == signal.active else 1
            self.streaks[signal.rule] = (signal.active, min(count, 10000))
            required = selected.consecutive_failures if signal.active else 2
            if count >= required:
                steward_store.observe(signal.rule, signal.active, signal.severity, signal.title, signal.detail,
                    recipients=selected.recipients if selected.enabled else (), recover=selected.send_recovery,
                    analyze=selected.ai_analysis)

    def record(self, current, selected):
        with db.transaction():
            self.observe(current, selected)
            db.set_setting(SNAPSHOT_KEY, {**current, 'collection_interval_seconds': selected.interval_seconds})
            db.set_setting(STREAK_KEY, {'at': int(time.time()), 'rules': self.streaks})
            db.set_setting(HEARTBEAT_KEY, {'at': int(time.time()), 'collector': 'ok', 'schema': 1})

    async def monitor(self):
        while True:
            delay = 60
            try:
                selected = await joined_thread(steward_config.policy)
                delay = selected.interval_seconds
                current = await steward_snapshot.collect(self.upstream_runtime)
                await joined_thread(self.record, current, selected)
                await joined_thread(steward_store.prune)
            except Exception:
                # Never log upstream bodies, SMTP exceptions, prompts or credentials.
                log.error('steward collection failed; last snapshot must be treated as stale')
            await asyncio.sleep(delay)

    async def deliver_once(self):
        # Claim + finish are owned even if cancellation arrives during a DB call.
        # The whole cycle is shielded by deliver(), including SMTP thread finish.
        item = await joined_thread(steward_store.claim, self.owner)
        if item is None:
            return False
        try:
            selected = await joined_thread(steward_config.policy)
            if item['recipient'] not in selected.recipients or (not selected.enabled and not item['event'].startswith('test-')):
                result = {'status': 'cancelled', 'code': 'recipient_or_policy_changed'}
            else:
                payload = json.loads(item['payload'])
                transport = await joined_thread(steward_config.smtp_settings)
                try:
                    result = await joined_thread(steward_mail.send, transport,
                                                 item['recipient'], payload, delivery_id=item['id'])
                except Exception:
                    result = {'status': 'unknown', 'code': 'notification_transport_outcome_unknown'}
        except Exception:
            log.error('steward delivery unavailable; no credentials or provider body logged')
            result = {'status': 'retry', 'code': 'notification_configuration_unavailable'}
        await joined_thread(steward_store.finish, item, result)
        return True

    async def deliver(self):
        while True:
            cycle = asyncio.create_task(self.deliver_once())
            try:
                worked = await joined_task(cycle)
            except asyncio.CancelledError:
                raise
            except Exception:
                worked = False
                log.error('steward delivery persistence failed; sending evidence requires review')
            await asyncio.sleep(1 if worked else 3)

    async def analyze_once(self):
        item = await joined_thread(steward_store.claim_analysis, self.owner)
        if item is None:
            return
        answer, recipients, code = None, (), 'analysis_disabled_or_stale'
        try:
            selected = await joined_thread(steward_config.policy)
            evidence = await joined_thread(snapshot)
            if selected.enabled and selected.ai_analysis and evidence['available'] and not evidence['stale']:
                payload = json.loads(item['payload'])
                def permitted():
                    current = steward_config.policy()
                    incident = db.query_one('SELECT state FROM steward_incidents WHERE id=?', (item['incident_id'],))
                    if (not current.enabled or not current.ai_analysis or current.revision != selected.revision
                            or snapshot()['stale'] or incident is None
                            or (incident['state'] == 'resolved') != (item['event'] == 'recovered')):
                        raise RuntimeError('steward_analysis_superseded')
                result = await steward_ai.answer('分析这个状态变化，说明事实、影响、可能原因和下一步：' +
                    payload['title'] + '；' + payload['detail'] + '；事件类型：' + payload['event'],
                    evidence['snapshot'], model=selected.model, before_dispatch=permitted)
                answer, code = result['answer'], ''
            selected = await joined_thread(steward_config.policy)
            recipients = selected.recipients if selected.enabled and selected.ai_analysis else ()
        except Exception:
            code = 'analysis_unavailable'
            log.warning('steward analysis unavailable; rule delivery continues independently')
        await joined_thread(steward_store.finish_analysis, item, answer, recipients=recipients, code=code)

    async def analyze(self):
        while True:
            try:
                # Bounded model operation and its durable finish precede lease
                # release, even when shutdown lands while claiming the job.
                await joined_task(asyncio.create_task(self.analyze_once()))
            except Exception:
                log.error('steward analysis persistence failed; evidence requires review')
            await asyncio.sleep(3)
