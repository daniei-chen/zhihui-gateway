"""A fail-closed inference cohort controller; no installed host adapter.

This controller can be exercised against real Linux processes and Nginx in a
disposable lab. It deliberately cannot run the production publisher, issue a
writer capability, alter the schema floor, or admit an old non-cooperating
Manager. A reviewed host adapter and first-activation procedure are still
required. Background/control-plane ownership stays outside this cohort.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
import asyncio


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class Contract:
    generation: str
    source_sha256: str
    coordinator_id: str
    admission_policy_sha256: str
    schema_sha256: str
    engine_sha256: str
    engine_config_sha256: str
    authority_sha256: str
    background_owner: str
    shared_protocol: int = 1
    background_jobs: bool = False
    inference_only: bool = True


def compatible(old: Contract, new: Contract):
    for contract in (old, new):
        if not re.fullmatch(r'[A-Za-z0-9_.+-]{1,96}', contract.generation):
            raise Refused('invalid_generation')
        for field in ('source_sha256', 'admission_policy_sha256', 'schema_sha256',
                      'engine_sha256', 'engine_config_sha256', 'authority_sha256'):
            if not re.fullmatch(r'[0-9a-f]{64}', getattr(contract, field)):
                raise Refused('invalid_contract_digest')
        if contract.shared_protocol != 1:
            raise Refused('baseline_bootstrap_required')
        if contract.background_jobs or not contract.inference_only:
            raise Refused('control_plane_handoff_not_admitted')
        if not contract.coordinator_id or not contract.background_owner:
            raise Refused('missing_shared_authority')
    if old.generation == new.generation:
        raise Refused('distinct_generations_required')
    for field in ('coordinator_id', 'admission_policy_sha256', 'schema_sha256',
                  'engine_sha256', 'engine_config_sha256', 'authority_sha256',
                  'background_owner'):
        if getattr(old, field) != getattr(new, field):
            raise Refused('incompatible_' + field)


@contextmanager
def controller_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if not os.fstat(fd).st_size:
                os.write(fd, b'1')
            os.lseek(fd, 0, 0)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Refused('another_controller_active') from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused('another_controller_active') from None
        yield
    finally:
        os.close(fd)


def atomic_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    temporary = path.with_name(path.name + '.' + os.urandom(8).hex() + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class InferenceRollout:
    """Adapter verifies artifacts/identity and updates the stable relay route.

    No request replay. A failed/stuck drain retains the owning process. A health
    failure routes only subsequent requests back; accepted new streams survive.
    Journals are single use. An interrupted journal requires explicit inspection,
    not guessing which generation is active from a stale on-disk config alone.
    """
    def __init__(self, old, new, operations, journal):
        self.old, self.new, self.ops = old, new, operations
        self.journal = Path(journal)
        if self.journal.exists():
            raise Refused('existing_journal_requires_inspection')
        self.events = []

    def record(self, phase, **fields):
        self.events.append({'phase': phase, 'at': time.time(), **fields})
        atomic_json(self.journal, {'schema': 'zhihui-inference-rollout/v1',
            'old': asdict(self.old), 'new': asdict(self.new), 'events': self.events})

    async def run(self, *, observe_seconds=1.0, drain_seconds=120.0):
        if observe_seconds < 0 or not 0 < drain_seconds <= 3600:
            raise ValueError('invalid_rollout_window')
        with controller_lock(self.journal.parent / 'cohort-controller.lock'):
            if self.journal.exists():
                raise Refused('existing_journal_requires_inspection')
            compatible(self.old, self.new)
            # The lab supplies an independent verifier. No production adapter is
            # implemented here and no unverified automatic shell launcher exists.
            if not await self.ops.verify_artifacts(self.old, self.new):
                raise Refused('artifact_verification_failed')
            if await self.ops.active_generation() != self.old.generation:
                raise Refused('unexpected_live_route')
            if not await self.ops.healthy(self.old):
                raise Refused('rollback_target_not_healthy')
            if not await self.ops.healthy(self.new):
                raise Refused('candidate_not_healthy')
            self.record('prepared')
            switched = False
            retirement_started = False
            try:
                await self.ops.route(self.new)
                switched = True
                self.record('routed')
                until = time.monotonic() + observe_seconds
                while True:
                    if not await self.ops.healthy(self.new):
                        raise Refused('candidate_health_failed')
                    if time.monotonic() >= until:
                        break
                    await asyncio.sleep(min(0.05, max(0, until - time.monotonic())))
                self.record('observed')
                if not await self.ops.drained(self.old, drain_seconds):
                    self.record('retirement_deferred', reason='old_requests_still_owned')
                    return {'phase': 'retirement_deferred', 'active': self.new.generation,
                            'old_retained': True, 'forced_stop': False}
                # Artifact retention is a separate policy; never delete old data.
                self.record('retiring')
                retirement_started = True
                await self.ops.retire(self.old)
                self.record('complete')
                return {'phase': 'complete', 'active': self.new.generation,
                        'forced_stop': False}
            except BaseException:
                if retirement_started and await self.ops.healthy(self.new):
                    # A missing exit receipt is not permission to route to an
                    # already stopped old process. Keep the healthy route alive.
                    try:
                        self.record('retirement_review_required', new_still_serving=True)
                    except OSError:
                        pass
                    raise Refused('new_serving_retirement_requires_inspection') from None
                # A journal volume failure must not prevent route recovery.
                try:
                    self.record('failed', route_switched=switched)
                except OSError:
                    pass
                try:
                    observed = await self.ops.active_generation()
                except Exception:
                    observed = None
                # A reload may have taken effect before its acknowledgement failed.
                if switched or observed != self.old.generation:
                    if not await self.ops.healthy(self.old):
                        try:
                            self.record('recovery_required', reason='rollback_target_unhealthy')
                        except OSError:
                            pass
                        raise Refused('both_generations_need_operator') from None
                    await self.ops.route(self.old)
                    try:
                        self.record('rolled_back', accepted_new_requests_retained=True)
                    except OSError:
                        pass
                raise


def contract_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
