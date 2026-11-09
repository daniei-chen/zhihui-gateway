from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from . import credentiallease


_flights = {}


def generation(credentials):
    return hashlib.sha256(json.dumps([credentials.get('accessToken', ''),
                                     credentials.get('refreshToken', '')],
                                    separators=(',', ':')).encode()).hexdigest()


async def refresh(filename: str, payload: dict, base_dir: Path | None = None):
    from . import tencent
    path = tencent._safe_auth_path(filename, base_dir)
    key = (asyncio.get_running_loop(), str(path.absolute()), str(payload.get('uid') or ''))
    task = _flights.get(key)
    if task is None:
        task = asyncio.create_task(_refresh(path, filename, payload, base_dir))
        _flights[key] = task

        def finished(completed):
            if _flights.get(key) is completed:
                _flights.pop(key, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
    return await asyncio.shield(task)


async def _refresh(path, filename, payload, base_dir):
    from . import tencent
    try:
        async with credentiallease.acquire(path):
            raw = json.loads(path.read_text(encoding='utf-8'))
            credentials = raw.get('auth', raw)
            identity = raw.get('account', raw)
            if (not isinstance(credentials, dict) or not isinstance(identity, dict)
                    or str(identity.get('uid') or '') != str(payload.get('uid') or '')):
                return False, 'credential identity changed; retry after reload', {}
            current = {key: credentials.get(source) for key, source in (
                ('access_token', 'accessToken'), ('refresh_token', 'refreshToken'),
                ('expires_at', 'expiresAt'), ('domain', 'domain')) if source in credentials}
            if not isinstance(current.get('access_token'), str) or not current['access_token'].strip():
                return False, 'stored credential is invalid; reload or log in again', {}
            if (current.get('access_token') != payload.get('access_token')
                    or current.get('refresh_token') != payload.get('refresh_token')):
                return True, 'credential already updated by another writer', current
            expected = generation(credentials)
            success, message, fields = await tencent.refresh_token(payload)
            if not success:
                return success, message, fields
            fields = {**fields, '_refresh_expected': expected,
                      '_refresh_expected_uid': str(payload.get('uid') or '')}
            tencent.update_auth_tokens(filename, fields, base_dir)
            return True, message, {key: value for key, value in fields.items()
                                   if key not in ('_refresh_expected', '_refresh_expected_uid')}
    except (OSError, ValueError, TimeoutError) as exc:
        return False, '刷新或写入账号文件失败：' + type(exc).__name__ + '；请稍后重试或重新加载账号', {}
