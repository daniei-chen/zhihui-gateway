"""U0 inventory: enumerate the live Manager control plane (routes + auth guard).

Read-only audit for H08 U0. It imports the application object in an isolated
temporary data directory so no workspace state is touched, then dumps every
route with its auth guard (as resolved from the dependency tree) and the
module/function that serves it.

Usage (from the execution tree root):
    python -B -X utf8 tools/audit_control_plane.py --out evidence/.../control-plane.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Directories that must never be treated as the authoritative backend.
REFERENCE_ONLY = ('src/three-platform/server',)


def _guard_names(route) -> list[str]:
    """Resolve the auth guards from the FastAPI dependency tree."""
    names: list[str] = []
    dependant = getattr(route, 'dependant', None)
    if dependant is None:
        return names
    stack = list(getattr(dependant, 'dependencies', []) or [])
    while stack:
        item = stack.pop()
        call = getattr(item, 'call', None)
        if call is not None:
            names.append(getattr(call, '__name__', repr(call)))
        stack.extend(getattr(item, 'dependencies', []) or [])
    return names


def _iter_routes(routes, prefix='', inherited_guards=()):
    """Expand FastAPI >=0.130 deferred `_IncludedRouter` wrappers.

    Include prefixes and inherited dependencies are part of the effective
    route. Dropping them reports /__qtg/api paths as public /api handlers.
    """
    for route in routes:
        if type(route).__name__ == '_IncludedRouter':
            original = getattr(route, 'original_router', None)
            if original is not None:
                context = route.include_context
                names = tuple(getattr(item.dependency, '__name__', repr(item.dependency))
                              for item in context.dependencies)
                yield from _iter_routes(original.routes, prefix + context.prefix,
                                        inherited_guards + names)
            continue
        path = getattr(route, 'path', None)
        yield route, prefix + path if path is not None else None, inherited_guards


def _auth_mechanism(module, name, path, guards):
    """Describe source-visible gates; absence of Depends is not public access.

    This is an inventory, not an authentication proof. Handler and middleware
    classifications still require integration tests and manual control-flow
    review; unknown routes deliberately stay unclassified.
    """
    if module.startswith('server.qtgcore.') and (path or '').startswith('/__qtg/'):
        return 'manager_rewrite_middleware', True, False
    if module == 'server.routers.portal' and 'current' in guards:
        return 'portal_session_dependency', True, False
    if any('admin' in guard or 'writer' in guard or 'read_scope' in guard
           or guard == 'current_user' for guard in guards):
        return 'manager_auth_dependency', True, False
    if module in ('server.routers.gateway', 'server.routers.responses', 'server.routers.anthropic'):
        if name == 'gateway_health':
            return 'public_health', False, True
        return 'handler_api_key_validation', True, False
    if module == 'server.main' and name == 'prometheus_metrics':
        return 'handler_metrics_token', True, False
    if module == 'server.routers.redpackets' and name in ('claim_info', 'claim_draw'):
        return 'public_claim_capability', False, True
    if ((module == 'server.routers.auth' and name in ('login', 'logout', 'healthz', 'readyz'))
            or (module == 'server.routers.portal' and name in ('login', 'logout'))
            or (module == 'server.main' and name == 'public_ready')):
        return 'public_session_or_health_entry', False, True
    return 'review_required', None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix='control-plane-audit-'))
    os.environ['WB_DATA_DIR'] = str(tmp)
    os.environ['WB_DB'] = str(tmp / 'manager.db')
    os.environ['WB_USERS_FILE'] = str(tmp / 'users.json')
    os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
    sys.path.insert(0, str(REPO / 'manager'))
    sys.dont_write_bytecode = True

    from server.main import app  # noqa: E402

    rows = []
    for route, path, inherited_guards in _iter_routes(app.routes):
        methods = sorted(getattr(route, 'methods', set()) or [])
        endpoint = getattr(route, 'endpoint', None)
        module = getattr(endpoint, '__module__', '') if endpoint else ''
        name = getattr(endpoint, '__name__', '') if endpoint else getattr(route, 'name', '')
        guards = list(inherited_guards) + (_guard_names(route) if hasattr(route, 'dependant') else [])
        admin = any('admin' in guard for guard in guards)
        writer = any('writer' in guard for guard in guards)
        read_scope = any('read_scope' in guard for guard in guards)
        mechanism, authenticated, public = _auth_mechanism(module, name, path, guards)
        rows.append({
            'path': path,
            'methods': methods,
            'handler': f'{module}.{name}',
            'module': module,
            'guards': sorted(set(guards)),
            'admin_required': admin,
            'write_scope_required': writer,
            'read_scope_accepted': read_scope,
            'auth_mechanism': mechanism,
            'authenticated': authenticated,
            'public': public,
        })

    rows.sort(key=lambda item: (item['path'] or '', ','.join(item['methods'])))
    by_prefix: dict[str, int] = {}
    for row in rows:
        prefix = '/' + (row['path'] or '').strip('/').split('/')[0]
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
    document = {
        'schema': 'zhihui-control-plane-inventory/2',
        'generated_at': __import__('datetime').datetime.now().astimezone().isoformat(),
        'source': 'manager/server/main.py::app',
        'reference_only_paths': list(REFERENCE_ONLY),
        'interpretation': ('Mechanisms describe source-visible gates, not proof of enforcement. '
                           'public=null means review required; missing Depends never proves anonymous access. '
                           '/__qtg routes require the Manager rewrite middleware; public API aliases are separate.'),
        'totals': {
            'routes': len(rows),
            'public': sum(1 for r in rows if r['public'] is True),
            'admin_required': sum(1 for r in rows if r['admin_required']),
            'authenticated': sum(1 for r in rows if r['authenticated'] is True),
            'review_required': sum(1 for r in rows if r['public'] is None),
        },
        'by_prefix': dict(sorted(by_prefix.items(), key=lambda kv: -kv[1])),
        'routes': rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'routes': document['totals'], 'by_prefix': document['by_prefix']},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
