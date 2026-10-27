"""Application-owned instance lease lifecycle; not a global writer election."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time

from . import drain, instancelease

logger = logging.getLogger(__name__)


class InstanceRuntime:
    def __init__(self, *, heartbeat_sec=None, ttl_sec=None):
        self.interval = float(instancelease.HEARTBEAT_SEC if heartbeat_sec is None else heartbeat_sec)
        self.ttl = int(instancelease.LEASE_TTL_SEC if ttl_sec is None else ttl_sec)
        if not math.isfinite(self.interval) or self.interval <= 0 or self.ttl <= self.interval:
            raise ValueError('Instance heartbeat must be positive and shorter than lease TTL')
        self.identity = None
        self.fence = None
        self.task = None
        self.held = False
        self.closed = False
        self.lost = False
        self.failure_reason = None
        self.release_ok = None
        self.heartbeats = 0
        self.transient_errors = 0
        self.last_error = None
        # 本地保守的租约到期预算（单调时钟）；DB 里 lease_until 用 int(time.time())
        # 生成，可能比本地预算早最多 1 秒，所以要留安全边际。
        self._lease_deadline = None

    async def start(self):
        if self.held or self.closed:
            raise RuntimeError('Instance runtime cannot be started twice')
        self.identity = instancelease.identity()
        started = time.monotonic()
        claimed, self.fence = instancelease.claim(
            self.identity['instance_id'], owner=self.identity['owner'],
            ttl_seconds=self.ttl,
            role='draining' if drain.is_draining() else 'serving',
            detail='application lifespan')
        if not claimed:
            raise RuntimeError('Application instance lease claim rejected')
        self.held = True
        self._renew_deadline(started)
        if self._lease_remaining() <= 0:
            self._mark_lost('claim_budget_exhausted')
            self._release()
            raise RuntimeError('Application instance lease budget exhausted during claim')
        coroutine = self._heartbeat()
        try:
            self.task = asyncio.create_task(coroutine, name='manager-instance-heartbeat')
        except BaseException:
            coroutine.close()
            self._release()
            raise
        return self

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.interval)
            started = time.monotonic()
            try:
                renewed = instancelease.heartbeat(
                    self.identity['instance_id'], self.fence,
                    owner=self.identity['owner'], ttl_seconds=self.ttl)
            except Exception as exc:
                # 存储瞬时故障 != 真正失权：本地租约预算还有余量时重试下一个周期，
                # 耗尽才停止新准入。否则一次 SQLite busy/IO 抖动会让实例永久停机。
                self.transient_errors += 1
                self.last_error = f'{type(exc).__name__}: {exc}'
                if self.transient_errors == 1 or self.transient_errors % 30 == 0:
                    logger.exception('Application instance heartbeat failed (attempt %s)',
                                     self.transient_errors)
                if self._lease_remaining() <= self.interval:
                    logger.error('Lease budget exhausted after heartbeat failures; '
                                 'stopping admission before any other instance may claim')
                    self._mark_lost('heartbeat_error')
                    return
                continue
            if not renewed:
                self._mark_lost('ownership_rejected')
                return
            self.heartbeats += 1
            self._renew_deadline(started)
            if self._lease_remaining() <= 0:
                self._mark_lost('heartbeat_budget_exhausted')
                return

    def _renew_deadline(self, started):
        """记下本地保守的租约到期预算；成功 claim/心跳后调用；等待与提交耗时不能凭空续命。"""
        margin = min(2.0, max(1.0, self.ttl * 0.1))
        self._lease_deadline = started + self.ttl - margin

    def _lease_remaining(self):
        if self._lease_deadline is None:
            return 0.0
        return self._lease_deadline - time.monotonic()

    def _mark_lost(self, reason):
        self.lost = True
        self.failure_reason = reason
        logger.critical('Instance lease lost; stopping admission until process restart reason=%s', reason)
        # Existing bodies finish; no automatic re-claim or administrative resume
        # can let an old fence continue serving after ownership loss.
        drain.begin(shutdown=True)

    def _release(self):
        if not self.held:
            return
        try:
            snapshot = drain.handoff_snapshot()
            snapshot['instance_runtime'] = {'heartbeats': self.heartbeats,
                'transient_errors': self.transient_errors,
                'heartbeat_sec': self.interval, 'ttl_sec': self.ttl,
                'failure_reason': self.failure_reason}
            if not self.lost and snapshot['total_in_flight'] == 0:
                instancelease.finalize_handoff(
                    self.identity['instance_id'], self.fence,
                    owner=self.identity['owner'], snapshot=snapshot)
            self.release_ok = instancelease.release(
                self.identity['instance_id'], self.fence,
                owner=self.identity['owner'], snapshot=snapshot)
            if not self.release_ok:
                logger.error('Instance release rejected; a different owner/fence is preserved')
        except Exception:
            self.release_ok = False
            logger.exception('Instance release failed; expiry remains the crash-recovery fallback')
        finally:
            self.held = False

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.task is not None:
                self.task.cancel()
                await self.task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception('Instance heartbeat task ended unexpectedly during cleanup')
        finally:
            self._release()

    def status(self):
        token = self.identity['instance_id'] if self.identity else ''
        return {'identity_sha256_16': hashlib.sha256(token.encode()).hexdigest()[:16] if token else None,
                'fence': self.fence, 'held': self.held, 'closed': self.closed,
                'lost': self.lost, 'failure_reason': self.failure_reason,
                'release_ok': self.release_ok,
                'heartbeats': self.heartbeats,
                'transient_errors': self.transient_errors,
                'last_error': self.last_error,
                'lease_remaining_sec': (round(self._lease_remaining(), 3)
                                        if self.held else None),
                'heartbeat_running': self.task is not None and not self.task.done()}
