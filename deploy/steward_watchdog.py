#!/usr/bin/env python3
"""Durable, bounded notification watchdog for the local Steward policy."""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import dataclass
import json
import re
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import time

# systemd invokes this module as a direct isolated script (`python -I -B
# .../deploy/steward_watchdog.py`), so Python does not add the repository root
# to sys.path. Bind the sibling server package explicitly at the entrypoint.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from server import steward_config

try:
    import fcntl
except ImportError:  # Windows: native lock tests provide a synthetic sentinel.
    fcntl = None


PUBLIC_POLICY_MAX_AGE = 15 * 60
STATE_SCHEMA = 2


@dataclass(frozen=True)
class Paths:
    database: Path = Path("/opt/workbuddy-manager/data/manager.db")
    state_dir: Path = Path("/opt/workbuddy-manager/data/steward-watchdog")
    smtp_file: Path = Path("/etc/zhihui/steward-smtp.json")
    publication_lock: Path = Path("/run/zhihui-release.lock")


def atomic(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass


def safe_json(path: Path) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("state missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("state must be an object")
    return value


@contextmanager
def owned(paths: Paths):
    if os.name != "posix":
        raise RuntimeError("requires_posix")
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    lock = paths.state_dir / "worker.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _default_state() -> dict:
    return {
        "schema": STATE_SCHEMA, "policy": {}, "policy_checked_at": None,
        "control_current": False, "maintenance_since": None, "outbox": [],
        "rules": {
            "manager_readiness": {"active": False, "failures": 0, "generation": 0},
            "collector_liveness": {"active": False, "failures": 0, "generation": 0},
        },
    }


def validate_state(value: dict, now: int | None = None) -> None:
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise ValueError("invalid watchdog state")
    if not isinstance(value.get("outbox"), list) or not isinstance(value.get("rules"), dict):
        raise ValueError("invalid watchdog state")
    ids = set()
    for row in value["outbox"]:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                or row["id"] > max(1000, len(value["outbox"]) * 1000)
                or row["id"] in ids or not isinstance(row.get("recipient"), str)
                or not row["recipient"] or any(c in row["recipient"] for c in "\r\n")
                or row.get("status") not in {"pending", "sending", "accepted", "unknown", "failed", "cancelled", "rejected"}
                or type(row.get("attempts")) is not int or row["attempts"] < 0
                or row["attempts"] > 3 or not isinstance(row.get("code", ""), str)
                or len(row.get("code", "")) > 160 or any(c in row.get("code", "") for c in "\r\n")):
            raise ValueError("invalid watchdog delivery")
        if row.get("code", "") and not re.fullmatch(r"[A-Za-z0-9_.-]+", row["code"]):
            raise ValueError("invalid watchdog delivery code")
        ids.add(row["id"])
    for name in ("manager_readiness", "collector_liveness"):
        rule = value["rules"].get(name)
        if (not isinstance(rule, dict) or type(rule.get("active")) is not bool
                or type(rule.get("failures")) is not int or rule["failures"] < 0
                or type(rule.get("generation")) is not int or rule["generation"] < 0):
            raise ValueError("invalid watchdog rule")


def read_control(paths: Paths) -> dict:
    with closing(sqlite3.connect(paths.database)) as connection:
        rows = dict(connection.execute(
            "SELECT key,value FROM settings WHERE key IN ('steward.policy.v1','steward.heartbeat.v1')"))
    result = {"policy": json.loads(rows.get("steward.policy.v1", "{}"))}
    if "steward.heartbeat.v1" in rows:
        result["heartbeat"] = json.loads(rows["steward.heartbeat.v1"])
    else:
        result["heartbeat"] = None
    return result


def _state_path(paths: Paths) -> Path:
    return paths.state_dir / "state.json"


def _load(paths: Paths) -> dict:
    path = _state_path(paths)
    if not path.exists():
        value = _default_state()
    else:
        value = safe_json(path)
        validate_state(value)
    return value


def _next_id(state: dict) -> int:
    return max([row["id"] for row in state["outbox"]] or [0]) + 1


def _queue(state: dict, *, rule: str, event: str, policy, now: int, detail: str,
           severity: str = "warning") -> None:
    if not policy.enabled or not policy.recipients or event == "recovered" and not policy.send_recovery:
        return
    if event == "firing":
        cooldown_recipients = set()
        for prior in state["outbox"]:
            if prior.get("rule") == rule and prior.get("event") == "firing" and prior.get("status") in {
                    "accepted", "unknown", "sending"}:
                at = int(prior.get("finished_at") or prior.get("attempted_at") or prior.get("next_at") or 0)
                if now - at < 30 * 60:
                    cooldown_recipients.add(prior.get("recipient"))
    existing = {(row.get("event"), row.get("recipient")) for row in state["outbox"]
                if row.get("status") in {"pending", "sending"}}
    for recipient in policy.recipients:
        if event == "firing" and recipient in cooldown_recipients:
            state["outbox"].append({
                "id": _next_id(state), "rule": rule, "event": event,
                "recipient": recipient, "payload": {"event": event, "rule": rule,
                "severity": severity, "title": "智汇网关状态需要关注", "detail": detail,
                "observed_at": now}, "status": "cancelled", "attempts": 0,
                "attempted_at": now, "finished_at": now, "next_at": now,
                "code": "alert_cooldown_30m",
            })
            continue
        if (event, recipient) in existing:
            continue
        state["outbox"].append({
            "id": _next_id(state), "rule": rule, "event": event,
            "recipient": recipient, "payload": {"event": event, "rule": rule,
            "severity": severity, "title": "智汇网关状态需要关注" if event != "recovered" else "智汇网关已恢复",
            "detail": detail, "observed_at": now}, "status": "pending",
            "attempts": 0, "next_at": now, "code": "",
        })


def _cancel_rule(state: dict, rule: str, now: int) -> None:
    for row in state["outbox"]:
        if row.get("rule") == rule and row.get("status") == "pending":
            row.update(status="cancelled", finished_at=now, code="incident_recovered")


def _policy_from(raw: dict):
    return steward_config.validate_policy(raw)


def _read_policy(control, paths, state, now):
    try:
        current = control(paths)
    except Exception:
        state["control_current"] = False
        checked = state.get("policy_checked_at")
        cached = state.get("policy") or {}
        if (isinstance(checked, int) and now - checked <= PUBLIC_POLICY_MAX_AGE):
            return _policy_from(cached)
        state["policy"] = {"enabled": False, "recipients": []}
        for row in state["outbox"]:
            if row.get("status") == "pending":
                row.update(status="cancelled", finished_at=now, code="policy_unavailable")
        return _policy_from(state["policy"])
    # A current policy that is malformed is an operator/configuration error;
    # stale cached recipients must never be used to hide it.
    policy = _policy_from(current.get("policy") or {})
    state["policy"] = policy.model_dump()
    state["policy_checked_at"] = now
    state["control_current"] = True
    state["heartbeat"] = current.get("heartbeat")
    return policy


def _send_pending(paths, state, policy, sender, now, control) -> int:
    processed = 0
    for row in list(state["outbox"]):
        if row.get("status") != "pending" or int(row.get("next_at", 0)) > now:
            continue
        if row.get("recipient") not in policy.recipients and not str(row.get("event", "")).startswith("test-"):
            row.update(status="cancelled", finished_at=now, code="recipient_removed")
            continue
        row["status"] = "sending"
        row["attempts"] += 1
        row["attempted_at"] = now
        atomic(_state_path(paths), state)
        try:
            raw = steward_config.secret_file(paths.smtp_file)
            settings = steward_config.validate_smtp(json.loads(raw))
        except Exception:
            if row["attempts"] >= 3:
                row.update(status="failed", finished_at=now, code="smtp_config_unavailable")
            else:
                row.update(status="pending", next_at=now + (60 if row["attempts"] == 1 else 300), code="smtp_config_unavailable")
            atomic(_state_path(paths), state)
            processed += 1
            continue
        try:
            outcome = sender(settings, row["recipient"], row["payload"], delivery_id=row["id"])
            if not isinstance(outcome, dict):
                outcome = {"status": "unknown", "code": "invalid_sender_result"}
        except Exception:
            outcome = {"status": "unknown", "code": "smtp_transport_unknown"}
        status = outcome.get("status")
        if status == "retry" and row["attempts"] < 3:
            row.update(status="pending", next_at=now + (60 if row["attempts"] == 1 else 300), code=str(outcome.get("code", "smtp_retry"))[:80])
        else:
            row.update(status=status if status in {"accepted", "unknown", "rejected", "cancelled"} else "failed",
                       finished_at=now, code=str(outcome.get("code", ""))[:80])
        atomic(_state_path(paths), state)
        processed += 1
        # Re-read current policy after each delivery so recipient removal made
        # during a batch cannot send the next address.
        try:
            policy = _policy_from(control(paths).get("policy") or {})
        except Exception:
            pass
    return processed


def run_once(paths: Paths, *, probe, control=read_control, maintenance=lambda _: False,
             sender=None, now=None) -> dict:
    now = int(time.time() if now is None else now)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    state = _load(paths)
    # A process that died after handing a message to SMTP leaves acceptance
    # unknown.  Never resend it automatically on the next watchdog lifetime.
    for row in state["outbox"]:
        if row.get("status") == "sending":
            row.update(status="unknown", finished_at=now, code="worker_interrupted")
    policy = _read_policy(control, paths, state, now)
    ready = bool(probe())
    raw_maintenance = bool(maintenance(paths))
    held = raw_maintenance and (state.get("maintenance_since") is None
                                or now - int(state["maintenance_since"]) < 15 * 60)
    if held:
        state["maintenance_since"] = state.get("maintenance_since") or now
    else:
        state["maintenance_since"] = None
    rule = state["rules"]["manager_readiness"]
    if ready:
        if rule["active"]:
            rule["active"] = False
            rule["failures"] = 0
            _cancel_rule(state, "manager_readiness", now)
            _queue(state, rule="manager_readiness", event="recovered", policy=policy, now=now,
                   detail="Manager readiness 已恢复")
        else:
            rule["failures"] = 0
    else:
        rule["failures"] += 1
        if rule["failures"] >= policy.consecutive_failures and not rule["active"]:
            rule["active"] = True
            rule["generation"] += 1
            _queue(state, rule="manager_readiness", event="firing", policy=policy, now=now,
                   detail="Manager readiness 未通过")
    known_firing_recipients = {
        row.get("recipient") for row in state["outbox"]
        if row.get("rule") == "manager_readiness" and row.get("event") == "firing"
    }
    if (not ready and rule["active"] and policy.enabled
            and any(recipient not in known_firing_recipients for recipient in policy.recipients)):
        _queue(state, rule="manager_readiness", event="firing", policy=policy, now=now,
               detail="Manager readiness 未通过")
    # A fixed heartbeat older than the selected interval is an evidence gap.
    heartbeat = state.get("heartbeat") or {}
    collector = state["rules"]["collector_liveness"]
    at = heartbeat.get("at") if isinstance(heartbeat, dict) else None
    if type(at) is int and now - at > policy.interval_seconds:
        collector["failures"] += 1
        if collector["failures"] >= 2 and not collector["active"]:
            collector["active"] = True
            collector["generation"] += 1
            _queue(state, rule="collector_liveness", event="firing", policy=policy, now=now,
                   detail="后台采集心跳超过配置周期未更新")
    else:
        collector["failures"] = 0
        if collector["active"]:
            collector["active"] = False
            _cancel_rule(state, "collector_liveness", now)
    processed = 0 if held or sender is None else _send_pending(paths, state, policy, sender, now, control)
    validate_state(state)
    atomic(_state_path(paths), state)
    return {"processed": processed, "maintenance": held, "control_current": state["control_current"],
            "policy": state["policy"], "rules": state["rules"]}


def publishing(paths: Paths) -> bool:
    """Best-effort kernel lock evidence; never trust a root-only marker file."""
    if not paths.publication_lock:
        return False
    try:
        info = Path(paths.publication_lock).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            return False
        with Path(paths.publication_lock).open("rb") as stream:
            raw = stream.read().decode("ascii", "ignore")
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) >= 8 and fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"] and fields[5].endswith(str(info.st_ino)):
                return True
    except OSError:
        return False
    return False


def main(argv=None) -> int:
    parser = __import__("argparse").ArgumentParser()
    parser.add_argument("--onfailure")
    args = parser.parse_args(argv)
    if args.onfailure:
        parser.error("onfailure interface retired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
