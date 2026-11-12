"""Public inference relay application for generation handoff.

The relay has no management API and no fallback backend.  It reads a signed
operator-controlled route file for each new request; existing streams retain
the backend selected when they were accepted.  The process must be exposed
only through explicit Nginx locations for inference paths.  Panel and control
paths continue to use the Manager listener directly.
"""
from __future__ import annotations

import os

from .rollingrelay import INFERENCE_PATHS, StreamingRelay


def _address(name: str, default_port: int) -> str:
    value = os.environ.get(name, f"http://127.0.0.1:{default_port}").strip()
    if not value:
        raise RuntimeError(f"{name} must not be empty")
    return value


def _paths() -> frozenset[str]:
    raw = os.environ.get("WB_RELAY_ALLOWED_PATHS", "")
    if not raw.strip():
        return INFERENCE_PATHS
    paths = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not paths or any(not item.startswith("/") or "?" in item for item in paths):
        raise RuntimeError("WB_RELAY_ALLOWED_PATHS must contain absolute paths")
    return paths


relay = StreamingRelay(
    {
        "old": _address("WB_RELAY_BACKEND_OLD", 7866),
        "new": _address("WB_RELAY_BACKEND_NEW", 7865),
    },
    os.environ.get("WB_RELAY_ROUTE_FILE", "/opt/workbuddy-manager/data/relay-route.json"),
    maximum=int(os.environ.get("WB_RELAY_MAX_CONNECTIONS", "128")),
    allowed_paths=_paths(),
)

app = relay
