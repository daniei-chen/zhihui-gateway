"""Platform-bound account authorization; API results never expose provider credentials.

Short-lived sessions use the existing settings transaction store (no schema/reader
change). Trae's current engine has one PKCE slot, so its pending login is serialized
across Manager processes. An uncertain completion is not silently replayed.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException, Request

from .. import db, security

PREFIX = 'auth.platform-session.'
TTL = 300
RESULT_TTL = 900
MAX_SESSIONS = 256
TRAE_REDIRECT = 'http://127.0.0.1:17388/authorize'
TRAE_API_HOST = 'https://api.trae.com.cn'
logger = logging.getLogger(__name__)
UNCERTAIN = frozenset(('finishing', 'unknown', 'polling'))


def _deny(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


def platform_for(request: Request | None, body: dict | None = None) -> str:
    values = []
    if request is not None:
        values += request.headers.getlist('x-qtg-platform')
        values += request.query_params.getlist('platform')
    if body and body.get('platform') is not None:
        values.append(body['platform'])
    if any(not isinstance(v, str) or v.strip().lower() not in
           ('workbuddy', 'qoder', 'trae') for v in values):
        _deny(400, '授权平台无效，请重新选择 WorkBuddy、Qoder 或 Trae')
    normalized = {v.strip().lower() for v in values}
    if len(normalized) > 1:
        _deny(400, '授权平台参数不一致，请刷新页面后重试')
    return next(iter(normalized), 'workbuddy')


def realm_for(raw: object) -> str:
    realm = 'cn' if raw is None else str(raw).strip().lower()
    if realm not in ('cn', 'global'):
        _deny(400, '授权版本无效，请选择国内版或国际版')
    return realm


def realm_from(request: Request | None, body: dict | None = None, fallback=None) -> str:
    values = request.query_params.getlist('realm') if request is not None else []
    if body is not None and 'realm' in body:
        values.append(body['realm'])
    if not values:
        values.append(fallback)
    normalized = {realm_for(value) for value in values}
    if len(normalized) != 1:
        _deny(400, '授权版本参数不一致，请刷新页面后重试')
    return normalized.pop()


def check_poll_origin(request: Request | None):
    """Polling can insert an account, so safe-method CSRF exemptions do not apply."""
    if request is None:
        return
    if request.headers.get('sec-fetch-site', '').lower() == 'cross-site':
        _deny(403, '授权结果只能在智汇控制台同源页面读取')
    origins = request.headers.getlist('origin')
    source = (origins[0] if origins else '') or request.headers.get('referer', '')
    if len(origins) > 1 or (source and security._origin_triple(source)
                            != security.expected_origin(request)):
        _deny(403, '授权请求来源与智汇控制台不一致')


async def _adapter(request, platform):
    rt = getattr(request.app.state, 'rt', None)
    if rt is None:
        from ..routers.qtg_panel import get_rt
        try:
            rt = await get_rt()
        except Exception:
            _deny(503, '当前平台的授权服务不可用，请稍后重试')
    adapter = getattr(rt, platform, None)
    if adapter is None:
        _deny(503, '当前平台的授权服务未配置')
    return adapter


def _engine(adapter):
    return (f"{getattr(adapter, 'name', type(adapter).__name__)}|{getattr(adapter, 'base_url', '')}"
            f"|{getattr(adapter, 'authorization_generation', '')}")


def _save(state, session):
    db.set_setting(PREFIX + state, session)


def _read(state):
    if not isinstance(state, str) or not re.fullmatch(r'zh-[0-9a-f]{48}', state):
        _deny(404, '授权会话不存在或已失效，请重新获取')
    row = db.get_setting(PREFIX + state)
    if not isinstance(row, dict):
        _deny(404, '授权会话不存在或已失效，请重新获取')
    if row.get('status') not in UNCERTAIN and time.time() > row['expires'] + RESULT_TTL:
        _deny(404, '授权会话不存在或已失效，请重新获取')
    return row


def _sessions(now):
    rows = []
    for item in db.query('SELECT key, value FROM settings WHERE key LIKE ?', (PREFIX + '%',)):
        try:
            session = json.loads(item['value'])
        except (TypeError, ValueError):
            _deny(503, '授权会话台账异常，已暂停发起新授权')
        if (not isinstance(session, dict) or not isinstance(session.get('expires'), (int, float))
                or not isinstance(session.get('owner'), str)
                or session.get('platform') not in ('qoder', 'trae')
                or session.get('realm') not in ('cn', 'global')
                or not isinstance(session.get('engine'), str)):
            _deny(503, '授权会话台账异常，已暂停发起新授权')
        # Unknown completion is deliberately retained: a remote write may have
        # continued after our timeout. Do not open another Trae PKCE slot over it.
        if session.get('status') not in UNCERTAIN and now > session['expires'] + RESULT_TTL:
            db.execute('DELETE FROM settings WHERE key=?', (item['key'],))
        else:
            rows.append((item['key'][len(PREFIX):], session))
    return rows


def _after_success(user, platform, realm):
    from ..qtgcore.cache import cache
    cache.invalidate()
    try:
        security.audit(user, 'accounts.auth.complete', platform, f'realm={realm}')
    except Exception:
        # A committed provider result must not be turned into a retryable write
        # because a secondary audit append failed. Do not log exception secrets.
        logger.error('Authorization saved; audit append unavailable (platform=%s)', platform)


def _operation_deadline(adapter, now):
    timeout = float(getattr(getattr(adapter, 'cfg', None), 'upstream_timeout_sec', 20))
    return now + max(TTL, 2 * timeout + 30)


def _owned(state, user, platform, realm, upstream_id, adapter=None):
    user = security.authorization_owner(user)
    session = _read(state)
    if (session['owner'] != user['username'] or session['platform'] != platform
            or session.get('owner_binding') != user['_authorization_binding']
            or session['realm'] != realm or upstream_id is not None
            or (adapter is not None and session['engine'] != _engine(adapter))):
        _deny(404, '授权会话不存在或与当前平台、版本不匹配')
    return session


def _view(state, session):
    return {'state': state, 'platform': session['platform'], 'realm': session['realm'],
            'mode': session['mode'], 'authUrl': session['auth_url'],
            'expires_at': session['expires']}


def _url(value, host, path):
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 32 for c in value):
        _deny(502, '上游授权链接格式异常，已停止跳转')
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'https' and parts.hostname == host and
                 parts.port in (None, 443) and not parts.username and not parts.password
                 and parts.path == path and not parts.fragment)
    except ValueError:
        valid = False
    if not valid:
        _deny(502, '上游授权链接与所选平台、版本不一致，已停止跳转')
    return parts


def _query(parts, *, status=400):
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=64)
    except ValueError:
        _deny(status, '授权链接参数格式无效')
    if len(dict(pairs)) != len(pairs):
        _deny(status, '授权链接含重复参数，已拒绝')
    return dict(pairs)


async def start(request, user, platform, realm, upstream_id, body):
    user = security.authorization_owner(user)
    realm = realm_for(realm)
    if upstream_id is not None:
        _deny(409, '当前平台使用独立账号池，不支持 WorkBuddy 账号分组')
    if body.get('region') or body.get('proxy'):
        _deny(409, '当前平台不支持 WorkBuddy 的注册地区或单账号出口选项')
    if platform == 'trae' and realm == 'global':
        _deny(409, '当前 Trae 引擎仅支持国内版授权；国际版尚未接入，不会跳转 WorkBuddy')
    adapter = await _adapter(request, platform)
    if not callable(getattr(adapter, 'oauth_start' if platform == 'qoder' else 'oauth_login_url', None)):
        _deny(409, '当前平台引擎尚未提供该授权方式，请使用已支持的账号导入方式')
    security.authorization_owner(user)
    now = int(time.time())
    with db.transaction():
        rows = _sessions(now)
        if platform == 'trae':
            for state, session in rows:
                if session['platform'] != 'trae':
                    continue
                busy = session['status'] in ('finishing', 'unknown')
                pending_until = max(session['expires'], session.get('operation_deadline', 0))
                if busy or (pending_until > now and session['status'] in ('starting', 'waiting')):
                    if (session['owner'] == user['username'] and session.get('owner_binding') == user['_authorization_binding'] and session['realm'] == realm
                            and session['status'] == 'waiting' and session['engine'] == _engine(adapter)):
                        return _view(state, session)
                    _deny(409, 'Trae 当前有授权会话处理中，请先完成或取消；结果待核对时不能重复提交')
        if len(rows) >= MAX_SESSIONS:
            _deny(429, '授权会话过多，请稍后重试')
        state = 'zh-' + secrets.token_hex(24)
        session = {'owner': user['username'], 'owner_binding': user['_authorization_binding'], 'platform': platform, 'realm': realm,
                   'engine': _engine(adapter), 'expires': now + TTL, 'status': 'starting',
                   'mode': 'poll' if platform == 'qoder' else 'callback', 'auth_url': ''}
        session['operation_deadline'] = _operation_deadline(adapter, now)
        _save(state, session)
    try:
        if platform == 'qoder':
            out = await adapter.oauth_start('intl' if realm == 'global' else 'cn')
            auth_url = out.get('authUrl')
            _url(auth_url, 'qoder.com' if realm == 'global' else 'qoder.com.cn', '/device/selectAccounts')
            engine_state = out.get('state')
            if not isinstance(engine_state, str) or not 1 <= len(engine_state) <= 256:
                _deny(502, 'Qoder 未返回有效授权会话')
        else:
            out = await adapter.oauth_login_url()
            auth_url = out.get('url')
            query = _query(_url(auth_url, 'www.trae.cn', '/authorization'), status=502)
            engine_state = out.get('state')
            if (not isinstance(engine_state, str) or not re.fullmatch(r'[0-9a-fA-F]{32,128}', engine_state)
                    or query.get('login_trace_id') != engine_state
                    or query.get('auth_callback_url') != TRAE_REDIRECT
                    or out.get('redirect_uri') != TRAE_REDIRECT
                    or query.get('code_challenge_method') != 'S256'
                    or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', query.get('code_challenge', ''))):
                _deny(502, 'Trae 授权会话缺少有效的回调绑定或 PKCE 校验')
        with db.transaction():
            latest = _read(state)
            if latest['status'] != 'starting' or time.time() >= session['expires']:
                _deny(409, '授权取链已过期，请重新获取')
            session.update(auth_url=auth_url, engine_state=engine_state, status='waiting',
                           engine=_engine(adapter), operation_deadline=0)
            _save(state, session)
        try:
            security.audit(user, 'accounts.auth.start', platform, f'realm={realm}')
        except Exception:
            logger.error('Authorization started; audit append unavailable (platform=%s)', platform)
        security.authorization_owner(user)
        return _view(state, session)
    except BaseException as exc:
        # Start creates no account. An uncertain start holds the PKCE slot until
        # its normal expiry; unlike an uncertain finish it needs no billing/data reconciliation.
        with db.transaction():
            latest = _read(state)
            if latest['status'] == 'starting':
                session['status'] = 'starting' if platform == 'trae' and not isinstance(exc, HTTPException) else 'error'
                _save(state, session)
        if isinstance(exc, (HTTPException, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        # Exception bodies can contain upstream URLs/tokens. Keep them out of UI/audit.
        _deny(502, '当前平台未能创建授权链接，请稍后重试；未切换到其他平台')


def _result(session, status, **metadata):
    return {'status': status, 'platform': session['platform'], 'realm': session['realm'], **metadata}


def _review_item(state, session, adapter, now):
    """Project evidence only; never poll a provider, expire a row or release a slot."""
    raw = session.get('status')
    binding = ('unavailable' if adapter is None else
               'current' if session['engine'] == _engine(adapter) else 'changed')
    result = session.get('result') or {}
    status = 'invalid'
    if raw == 'success' and result.get('status') == 'success':
        status = 'success'
    elif raw == 'unknown':
        status = 'unconfirmed'
    elif raw in ('finishing', 'polling'):
        status = ('unconfirmed' if now >= session.get('operation_deadline', session['expires'])
                  else 'processing')
    elif raw in ('waiting', 'starting'):
        if now >= max(session['expires'], session.get('operation_deadline', 0) if raw == 'starting' else 0):
            status = 'expired'
        elif binding == 'changed':
            status = 'engine_changed'
        else:
            status = 'processing' if raw == 'starting' else 'waiting'
    item = _result(session, status, reference=state[-8:], engine_binding=binding,
                   expires_at=session['expires'],
                   requires_reconciliation=status == 'unconfirmed')
    if status == 'success':
        # Do not serialize the stored result wholesale, even for a completed session.
        item.update({key: result[key][:256] for key in ('uid', 'nickname')
                     if isinstance(result.get(key), str)})
    return item


def review(request, user, platform, realm, state=None, upstream_id=None):
    """Owner-scoped read-only evidence, available without a live engine generation."""
    check_poll_origin(request)
    user = security.authorization_owner(user)
    realm = realm_for(realm)
    if platform == 'workbuddy':
        _deny(409, 'WorkBuddy 授权暂不支持持久化状态核对，请在账号列表核对')
    if upstream_id is not None:
        _deny(404, '授权会话不存在或与当前平台、版本不匹配')
    now = int(time.time())
    rt = getattr(request.app.state, 'rt', None)
    adapter = getattr(rt, platform, None) if rt is not None else None
    if state is not None:
        rows = [(state, _owned(state, user, platform, realm, None))]
    else:
        # One SELECT gives a coherent snapshot without taking a SQLite writer lock.
        # Unlike _sessions(), this path must not prune expired evidence.
        records = db.query('SELECT key, value FROM settings WHERE key LIKE ? ORDER BY key LIMIT ?',
                           (PREFIX + '%', MAX_SESSIONS + 1))
        if len(records) > MAX_SESSIONS:
            _deny(503, '授权会话台账超出核对上限，请联系管理员检查')
        rows = []
        for record in records:
            try:
                session = json.loads(record['value'])
            except (TypeError, ValueError):
                _deny(503, '授权会话台账异常，暂不能核对状态')
            if not isinstance(session, dict):
                _deny(503, '授权会话台账异常，暂不能核对状态')
            if (session.get('owner'), session.get('platform'), session.get('realm')) != (
                    user['username'], platform, realm):
                continue
            if session.get('owner_binding') != user['_authorization_binding']:
                continue
            if (not isinstance(session.get('expires'), (int, float))
                    or not isinstance(session.get('engine'), str)):
                _deny(503, '授权会话台账异常，暂不能核对状态')
            if session.get('status') in UNCERTAIN or now <= session['expires'] + RESULT_TTL:
                rows.append((record['key'][len(PREFIX):], session))
    items = [_review_item(key, session, adapter, now) for key, session in rows]
    return {'platform': platform, 'realm': realm, 'reviewed_at': now, 'sessions': items}


async def poll(request, user, platform, realm, state, upstream_id):
    check_poll_origin(request)
    user = security.authorization_owner(user)
    adapter = await _adapter(request, platform)
    now = int(time.time())
    with db.transaction():
        session = _owned(state, user, platform, realm_for(realm), upstream_id, adapter)
        if session.get('result'):
            return session['result']
        if session['status'] == 'unknown':
            return _result(session, 'unconfirmed')
        if session['status'] in ('polling', 'finishing'):
            if now >= session.get('operation_deadline', session['expires']):
                session.update(status='unknown', auth_url='')
                _save(state, session)
                return _result(session, 'unconfirmed')
            return _result(session, 'waiting')
        if now >= session['expires']:
            return _result(session, 'expired')
        if now < session.get('next_poll_at', 0):
            return _result(session, 'waiting')
        if session['status'] != 'waiting':
            return _result(session, 'invalid')
        if platform == 'trae':
            return _result(session, 'waiting', mode='callback')
        session['status'] = 'polling'
        session['next_poll_at'] = now + 2
        session['operation_deadline'] = _operation_deadline(adapter, now)
        _save(state, session)
    try:
        out = await adapter.oauth_poll(session['engine_state'])
        status = out.get('status')
        if status == 'ok':
            account = out.get('account') or {}
            expected = 'intl' if session['realm'] == 'global' else 'cn'
            if not isinstance(account, dict) or not account.get('uid') or account.get('realm') != expected:
                _deny(502, 'Qoder 授权入池返回与当前版本不一致，请核对账号列表')
            result = _result(session, 'success', uid=str(account['uid']),
                             nickname=str(account.get('nickname') or ''))
        elif status in ('pending', 'waiting'):
            result = _result(session, 'waiting')
        elif status in ('expired', 'unknown'):
            result = _result(session, 'expired' if status == 'expired' else 'invalid')
        else:
            _deny(502, 'Qoder 授权查询失败，请稍后重试；未将上游错误当成成功')
        with db.transaction():
            session['status'] = 'waiting' if result['status'] == 'waiting' else result['status']
            if result['status'] != 'waiting':
                session['result'] = result
                session['auth_url'] = ''
            _save(state, session)
        if result['status'] == 'success':
            security.authorization_owner(user)
            _after_success(user, platform, session['realm'])
    except BaseException as exc:
        with db.transaction():
            # A lost response may follow an engine-side token save. The poll
            # consumes the device grant, so another poll is not a safe retry.
            if session.get('status') != 'success':
                session.update(status='unknown', auth_url='')
                _save(state, session)
        if isinstance(exc, (HTTPException, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return _result(session, 'unconfirmed')
    security.authorization_owner(user)
    return result


def validated_callback(value, session):
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 32 for c in value):
        _deny(400, '请粘贴本次 Trae 授权后的完整回调地址')
    try:
        parts = urlsplit(value)
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        valid = base == TRAE_REDIRECT and not parts.fragment
    except ValueError:
        valid = False
    if not valid:
        _deny(400, '回调地址不属于本次 Trae 授权，请复制完整地址')
    query = _query(parts)
    if set(query) - {'loginTraceID', 'host', 'authCodeInfo', 'userInfo', 'userRegion',
                     'isRedirect', 'scope'}:
        _deny(400, '回调参数不符合本次 Trae PKCE 授权流程，已拒绝旧令牌导入')
    # The native IDE authorization page includes these fixed routing hints.
    # Validate them here, but do not give the legacy exchange parser new inputs.
    if (('isRedirect' in query and query['isRedirect'] != 'true')
            or ('scope' in query and query['scope'] != 'trae')):
        _deny(400, '回调参数不符合本次 Trae PKCE 授权流程，已拒绝旧令牌导入')
    trace = query.get('loginTraceID', '')
    if not trace.isascii() or not hmac.compare_digest(trace, session['engine_state']):
        _deny(400, '回调与本次 Trae 授权不匹配，请勿使用其他窗口或旧回调')
    if query.get('host', TRAE_API_HOST).rstrip('/') != TRAE_API_HOST:
        _deny(400, '回调中的 Trae 接口地址不在允许范围，已停止交换凭据')
    try:
        info = json.loads(query.get('authCodeInfo', ''))
    except (ValueError, TypeError):
        _deny(400, '回调缺少有效的 Trae 授权码')
    if not isinstance(info, dict) or not isinstance(info.get('AuthCode'), str) or not info['AuthCode']:
        _deny(400, '回调缺少有效的 Trae 授权码')
    # Canonicalize before passing to the legacy permissive parser: no duplicate,
    # ambiguous escaping, attacker-chosen token, or attacker-chosen exchange host.
    query['host'] = TRAE_API_HOST
    query.pop('isRedirect', None)
    query.pop('scope', None)
    # The legacy engine trusts userInfo.UserID ahead of the exchanged JWT. Derive
    # account identity from that token instead of allowing callback text to name
    # another account whose saved credentials would then be overwritten.
    query.pop('userInfo', None)
    return TRAE_REDIRECT + '?' + urlencode(query, quote_via=quote)


async def finish(request, user, platform, realm, state, callback_url):
    user = security.authorization_owner(user)
    if platform != 'trae' or realm_for(realm) != 'cn':
        _deny(409, '当前平台或版本不支持回调地址授权')
    adapter = await _adapter(request, platform)
    with db.transaction():
        session = _owned(state, user, platform, 'cn', None, adapter)
        if session.get('result', {}).get('status') == 'success':
            return session['result']
        if session['status'] in ('finishing', 'unknown'):
            _deny(409, '授权已提交或结果待核对，请先刷新账号列表，不要重复提交')
        if session['status'] != 'waiting' or time.time() >= session['expires']:
            _deny(409, '授权会话已失效，请重新获取')
        url = validated_callback(callback_url, session)
        session['status'] = 'finishing'
        session['operation_deadline'] = _operation_deadline(adapter, time.time())
        _save(state, session)
    try:
        out = await adapter.oauth_finish(url)
        if not isinstance(out, dict) or not out.get('user_id'):
            raise ValueError('unconfirmed account insertion')
        result = _result(session, 'success', uid=str(out['user_id']), nickname=str(out.get('name') or ''))
        with db.transaction():
            session.update(status='success', result=result, auth_url='')
            _save(state, session)
        security.authorization_owner(user)
        _after_success(user, platform, 'cn')
    except BaseException as exc:
        with db.transaction():
            if session.get('status') != 'success':
                session.update(status='unknown', auth_url='')
                _save(state, session)
        if isinstance(exc, (HTTPException, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        _deny(502, 'Trae 授权提交后未能确认入池结果；请刷新账号列表核对，不要重复提交')
    security.authorization_owner(user)
    return result


async def cancel(request, user, platform, realm, state):
    with db.transaction():
        session = _owned(state, user, platform, realm_for(realm), None)
        if session['status'] in (*UNCERTAIN, 'starting'):
            _deny(409, '授权正在处理或结果待核对，暂不能取消')
        if session.get('result', {}).get('status') == 'success':
            return {'ok': True}
        session.update(status='cancelled', auth_url='')
        session['result'] = _result(session, 'invalid')
        _save(state, session)
    # Closing a Manager dialog does not revoke an existing provider account.
    return {'ok': True}
