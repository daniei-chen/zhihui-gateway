"""Server-owned platform selection and per-key platform authorization (K06)."""
from __future__ import annotations

import json
from contextvars import ContextVar
from urllib.parse import quote

PLATFORMS = ('workbuddy', 'qoder', 'trae')
current_platform: ContextVar[str] = ContextVar('forced_platform', default='')


class PlatformConflict(ValueError):
    def __init__(self, message, code='platform_conflict'):
        super().__init__(message)
        self.code = code


def normalize(values, *, default=True):
    if values is None and default:
        return list(PLATFORMS)
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
        raise ValueError('platforms must be an array of platform names')
    if any(v not in PLATFORMS for v in values):
        raise ValueError('platforms contains an unknown platform')
    return [p for p in PLATFORMS if p in values]


def stored_policy(raw):
    try:
        return normalize(json.loads(raw) if isinstance(raw, str) else raw, default=False), True
    except (ValueError, TypeError):
        return [], False


def split_model(model):
    if isinstance(model, str) and '/' in model:
        prefix, rest = model.split('/', 1)
        if prefix.lower() in PLATFORMS and rest:
            return prefix.lower(), rest
    return '', model


def select(path, header='', model=None):
    explicit = ''
    for p in PLATFORMS:
        base = f'/{p}/v1'
        if path == base or path.startswith(base + '/'):
            explicit = p
            break
    hint = (header or '').strip().lower()
    if hint and hint not in PLATFORMS:
        raise PlatformConflict('Unknown X-QTG-Platform', 'invalid_platform')
    model_platform, _ = split_model(model)
    sources = [v for v in (explicit, hint, model_platform) if v]
    if len(set(sources)) > 1:
        raise PlatformConflict('Platform path, header and model prefix disagree')
    return next(iter(sources), 'workbuddy')


def bind_request(request, model=None):
    path = request.scope.get('zhihui.original_path', request.url.path)
    chosen = select(path, request.headers.get('x-qtg-platform', ''), model)
    current_platform.set(chosen if chosen != 'workbuddy' else '')
    request.state.requested_platform = chosen
    return chosen


class PlatformRoutingMiddleware:
    def __init__(self, app, base_path=''):
        self.app = app
        self.base_path = (base_path or '').rstrip('/')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        original = scope.get('path', '')
        path = original
        if self.base_path and (path == self.base_path or path.startswith(self.base_path + '/')):
            path = path[len(self.base_path):] or '/'
        prefixes = ('/v1', '/v2') + tuple(f'/{p}/v1' for p in PLATFORMS)
        is_data = any(path == b or path.startswith(b + '/') for b in prefixes)
        token = current_platform.set('')
        try:
            if is_data:
                hint = next((v.decode('latin-1') for k, v in scope.get('headers', [])
                             if k.lower() == b'x-qtg-platform'), '')
                try:
                    chosen = select(path, hint)
                except PlatformConflict as exc:
                    from starlette.responses import JSONResponse
                    await JSONResponse({'error': {'message': str(exc), 'code': exc.code,
                                                  'type': 'invalid_request_error'}},
                                       status_code=400)(scope, receive, send)
                    return
                scope['zhihui.original_path'] = path
                current_platform.set(chosen if chosen != 'workbuddy' else '')
                for p in PLATFORMS:
                    base = f'/{p}/v1'
                    if path == base or path.startswith(base + '/'):
                        new_path = '/v1' + path[len(base):]
                        if self.base_path and original.startswith(self.base_path + '/'):
                            new_path = self.base_path + new_path
                        scope['path'] = new_path
                        scope['raw_path'] = quote(new_path, safe='/:@').encode('ascii')
                        break
            await self.app(scope, receive, send)
        finally:
            current_platform.reset(token)
