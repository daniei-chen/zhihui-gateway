"""Bounded two-generation rollout primitives.

This module is deliberately an orchestration contract, not a process manager.
The caller supplies the operations that verify, route, drain and retire a
generation.  A route switch is allowed only after the candidate contract is
compatible and its artifacts are verified.  Retirement is deferred when the
old generation still owns requests; the controller never force-kills a live
stream and never retries an accepted request.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class Refused(RuntimeError):
    """The rollout contract or a required operation could not be proven."""


@dataclass(frozen=True)
class Contract:
    """Immutable compatibility facts shared by two serving generations.

    ``source_sha256`` is allowed to change during an upgrade.  The remaining
    coordination and data-plane facts must remain equal for an in-place
    handoff.  ``background_jobs`` is separate because a candidate helper must
    not silently become a second scheduler or credential writer.
    """

    generation: str
    source_sha256: str
    coordinator_id: str
    admission_policy_sha256: str
    schema_sha256: str
    engine_sha256: str
    engine_config_sha256: str
    authority_sha256: str
    background_owner: str = ""
    shared_protocol: int = 1
    background_jobs: bool = False


_COMPATIBILITY_FIELDS = (
    "coordinator_id",
    "admission_policy_sha256",
    "schema_sha256",
    "engine_sha256",
    "engine_config_sha256",
    "authority_sha256",
    "background_owner",
)


def compatible(old: Contract, new: Contract) -> None:
    """Raise unless ``new`` can share the old generation's writer domain."""

    if not isinstance(old, Contract) or not isinstance(new, Contract):
        raise Refused("invalid_contract")
    if old.shared_protocol != 1 or new.shared_protocol != 1:
        raise Refused("bootstrap_required")
    if new.background_jobs:
        raise Refused("handoff_not_admitted")
    for field in _COMPATIBILITY_FIELDS:
        if getattr(old, field) != getattr(new, field):
            raise Refused(f"incompatible_{field}")


def atomic_json(path: str | os.PathLike[str], value: Any) -> None:
    """Write a small journal/route document atomically in its parent.

    The helper never follows a destination symlink and fsyncs both the file
    and parent directory before returning.  It is suitable for a local route
    or rollout journal, not for storing credentials or business state.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise Refused("unsafe_atomic_target")
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise Refused("atomic_target_busy")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            data = (raw + "\n").encode("utf-8")
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
        if os.name == "posix":
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class InferenceRollout:
    """Run a journaled route/observe/drain/retire handoff.

    ``operations`` must provide async methods:

    * ``verify_artifacts(old, new)``
    * ``active_generation()``
    * ``healthy(target)``
    * ``route(target)``
    * ``drained(target, timeout)``
    * ``retire(target)``

    The object does not replay a partially journaled rollout.  An operator
    must inspect the active route and ownership before constructing a new
    controller, which prevents an interrupted process from making a second
    blind cutover.
    """

    def __init__(self, old: Contract, new: Contract, operations: Any, journal: str | os.PathLike[str]):
        self.old = old
        self.new = new
        self.operations = operations
        self.journal = Path(journal)
        if self.journal.is_symlink():
            raise Refused("unsafe_journal")
        if self.journal.exists():
            try:
                state = json.loads(self.journal.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise Refused("requires_inspection") from exc
            if isinstance(state, dict) and state.get("phase") not in (None, "created", "failed"):
                raise Refused("requires_inspection")

    def record(self, phase: str, **extra: Any) -> None:
        if not isinstance(phase, str) or not phase:
            raise Refused("invalid_phase")
        atomic_json(self.journal, {"schema": 1, "phase": phase, **extra})

    async def _route_back(self) -> None:
        try:
            await self.operations.route(self.old)
        except Exception as exc:  # noqa: BLE001
            raise Refused("rollback_route_failed") from exc

    async def run(self, *, observe_seconds: float = 15.0, drain_seconds: float = 75.0) -> dict:
        if not 0 <= observe_seconds <= 3600 or not 0 < drain_seconds <= 3600:
            raise Refused("invalid_rollout_budget")
        compatible(self.old, self.new)
        verified = await self.operations.verify_artifacts(self.old, self.new)
        if verified is not True:
            self.record("verification_failed")
            raise Refused("verification_failed")
        active = await self.operations.active_generation()
        if active != self.old.generation:
            raise Refused("active_generation_changed")

        try:
            await self.operations.route(self.new)
        except Refused:
            self.record("route_refused")
            raise
        except Exception as exc:  # noqa: BLE001
            self.record("route_refused")
            raise Refused("config_invalid") from exc
        self.record("routed", from_generation=self.old.generation,
                    to_generation=self.new.generation)

        if observe_seconds:
            await asyncio.sleep(observe_seconds)
        if not await self.operations.healthy(self.new):
            await self._route_back()
            self.record("rolled_back", reason="health_failed")
            raise Refused("health_failed")

        drained = await self.operations.drained(self.old, drain_seconds)
        if not drained:
            self.record("retirement_deferred", reason="old_generation_not_drained")
            return {"phase": "retirement_deferred", "active": self.new.generation}

        try:
            await self.operations.retire(self.old)
        except Exception as exc:  # noqa: BLE001
            self.record("retirement_requires_inspection")
            raise Refused("retirement_requires_inspection") from exc
        self.record("complete", active=self.new.generation)
        return {"phase": "complete", "active": self.new.generation}
