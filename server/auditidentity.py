"""Small, credential-free identity contract for request audit state."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict


class AuditActor(TypedDict):
    username: str
    role: str
    scope: str


def actor_name(actor: object) -> str:
    """Keep the ledger's username column compatible with older string actors."""
    value = actor.get('username') if isinstance(actor, Mapping) else actor
    return value if isinstance(value, str) and value else 'anonymous'


def bind(request, user: Mapping[str, object]) -> AuditActor:
    """Copy only identity fields, never a session, token, or authorization secret."""
    actor: AuditActor = {
        'username': actor_name(user),
        'role': user.get('role') if isinstance(user.get('role'), str) else '',
        'scope': user.get('scope') if isinstance(user.get('scope'), str) else '',
    }
    request.state.audit_actor = actor
    return actor
