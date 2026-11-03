"""Customer portal sessions are separate from every administrator credential."""
from __future__ import annotations

import hmac
import json
import re
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db, keysvc, portalpolicy, portalservice as service, portalstore as store, security
from ..services import tencent
from ..services.asyncwork import joined_thread

router = APIRouter(prefix='/api/portal', tags=['user-portal'])
admin_router = APIRouter(prefix='/api/portal-admin', tags=['user-portal-admin'])
COOKIE = 'zh-user-portal'
_login_active = 0


def same_origin(request):
    if request.method in ('GET', 'HEAD'):
        return
    origins = request.headers.getlist('origin')
    expected = security.expected_origin(request)
    if len(origins) != 1 or expected is None or security._origin_triple(origins[0]) != expected:
        raise HTTPException(status_code=403, detail='用户端写操作需要同源请求')


def current(request: Request) -> dict:
    same_origin(request)
    raw = request.cookies.get(COOKIE, '')
    payload = security._unsign(raw, store.secret()) if raw else None
    if (not isinstance(payload, dict) or type(payload.get('exp')) is not int
            or type(payload.get('sv')) is not int or payload['exp'] <= store.now()):
        raise HTTPException(status_code=401, detail='请登录用户工作台')
    user = store.user(str(payload.get('id') or ''))
    if not user or not user['enabled'] or user['sv'] != payload.get('sv'):
        raise HTTPException(status_code=401, detail='用户登录已失效')
    return user


def check_user(user):
    with store.transaction() as conn:
        store.require_not_quarantined(conn)
    row = store.user(user['id'])
    if not row or not row['enabled'] or row['sv'] != user['sv']:
        raise HTTPException(status_code=401, detail='用户登录已失效')


def require_writable(conn):
    try:
        store.require_not_quarantined(conn)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post('/login')
async def login(request: Request, body: dict = Body(...)):
    same_origin(request)
    username, password = body.get('username'), body.get('password')
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 256:
        raise HTTPException(status_code=400, detail='登录格式无效')
    global _login_active
    if _login_active >= 4:
        raise HTTPException(status_code=429, detail='登录繁忙，请稍后重试')
    _login_active += 1
    try:
        def verify():
            rejected = False
            with store.transaction() as conn:
                row = conn.execute('SELECT * FROM portal_users WHERE username=?', (username.strip(),)).fetchone()
                user = dict(row) if row else None
            if user and user['blocked_until'] > store.now():
                raise HTTPException(status_code=429, detail='登录尝试过多，请稍后重试')
            if not user:
                security.dummy_verify(password)
            valid = bool(user and user['enabled'] and security.verify_pwd(password, user['pwd_hash']))
            with store.transaction() as conn:
                fresh = conn.execute('SELECT * FROM portal_users WHERE id=?', (user['id'],)).fetchone() if user else None
                if not valid or not fresh or not fresh['enabled'] or fresh['sv'] != user['sv'] or fresh['pwd_hash'] != user['pwd_hash']:
                    if fresh:
                        failures = fresh['failed_logins'] + 1
                        conn.execute('UPDATE portal_users SET failed_logins=?,blocked_until=? WHERE id=?',
                                     (failures, store.now() + 600 if failures >= 5 else 0, user['id']))
                    rejected = True
                else:
                    conn.execute('UPDATE portal_users SET failed_logins=0,blocked_until=0 WHERE id=?', (user['id'],))
            if rejected:
                raise HTTPException(status_code=401, detail='用户名或密码错误')
            return user
        user = await joined_thread(verify)
    finally:
        _login_active -= 1
    token = security._sign(json.dumps({'id': user['id'], 'sv': user['sv'], 'exp': store.now() + 86400}), store.secret())
    response = JSONResponse({'username': user['username']})
    response.set_cookie(COOKIE, token, max_age=86400, httponly=True, secure=security.cookie_secure(request), samesite='strict', path='/')
    return response


@router.get('/me')
def me(user=Depends(current)):
    return {'username': user['username'], 'id': user['id']}


@router.post('/logout')
def logout(request: Request):
    same_origin(request)
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE, path='/')
    return response


@router.get('/overview')
def overview(user=Depends(current)):
    return store.overview(user['id'])


@router.get('/requests')
def requests(page: int = 1, user=Depends(current)):
    page = min(10000, max(1, page))
    with store.transaction() as conn:
        links = conn.execute('SELECT key_id,key_hash FROM portal_key_links WHERE owner_id=?', (user['id'],)).fetchall()
    # Ownership is explicit; matching display names or mutable assigned_to is not evidence.
    ids = [row['key_id'] for row in links if (live := db.query_one('SELECT key_hash FROM api_keys WHERE id=?', (row['key_id'],)))
           and hmac.compare_digest(live['key_hash'], row['key_hash'])]
    if not ids:
        return {'items': [], 'total': 0, 'page': page, 'dispatch_enabled': False}
    placeholders = ','.join('?' for _ in ids)
    count = db.query_one(f'SELECT COUNT(*) FROM request_logs WHERE key_id IN ({placeholders})', ids)[0]
    rows = db.query(f'SELECT request_id,key_id,ts,model,status,prompt_tokens,completion_tokens,latency_ms,credit FROM request_logs '
                    f'WHERE key_id IN ({placeholders}) ORDER BY id DESC LIMIT 30 OFFSET ?', (*ids, (page - 1) * 30))
    items = [dict(row) for row in rows]
    with store.transaction() as conn:
        for item in items:
            spend = conn.execute('SELECT state,charged_units FROM portal_spends WHERE request_id=? AND owner_id=? AND key_id=?',
                (item['request_id'], user['id'], item['key_id'])).fetchone()
            if spend:
                item['billing_state'] = spend['state']
                item['credit'] = (spend['charged_units'] / store.SCALE if spend['state'] == 'settled' else
                                  0 if spend['state'] == 'released' else None)
    from .. import portalbilling
    return {'items': items, 'total': count, 'page': page, 'dispatch_enabled': portalpolicy.enabled() and portalbilling.dispatch_ready()}


@router.get('/keys')
def keys(user=Depends(current)):
    with store.transaction() as conn:
        rows = [dict(row) for row in conn.execute('SELECT l.key_id,l.key_hash,k.label,k.created_at,k.revoked,k.owner_sv FROM portal_key_links l '
            'JOIN portal_keys k ON k.key_id=l.key_id WHERE l.owner_id=? ORDER BY k.created_at DESC,l.key_id DESC', (user['id'],))]
    items = []
    for row in rows:
        live = db.query_one('SELECT key_hash,prefix,enabled,source FROM api_keys WHERE id=?', (row['key_id'],))
        if live and hmac.compare_digest(live['key_hash'], row['key_hash']):
            items.append({'id': row['key_id'], 'label': row['label'], 'prefix': live['prefix'], 'created_at': row['created_at'],
                'enabled': bool(live['enabled'] and not row['revoked'] and row['owner_sv'] == user['sv'] and live['source'] == portalpolicy.SOURCE)})
    from .. import portalbilling
    pooled = portalpolicy.enabled()
    with store.transaction() as conn:
        row = conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
        quarantined = bool(row and row[0] == 'true')
        approved = portalbilling.dispatch_ready(conn)
    return {'items': items, 'allowed_models': list(portalpolicy.MODELS), 'pool': 'public', 'max_ips': 0,
            'model_discovery_enabled': not quarantined, 'pool_enabled': pooled and not quarantined,
            'dispatch_enabled': pooled and not quarantined and approved,
            'dispatch_note': '恢复副本隔离核对中，不可调用' if quarantined else
                ('按调用密钥归属用户扣分，余额为零拒绝新请求；单次超出冻结余额由平台承担，不形成欠款。'
                 if approved else portalbilling.MESSAGE) if pooled else '公共池调用尚未开启；密钥可以读取模型目录，但不能发起计费调用。'}


@router.post('/keys')
def create_key(body: dict = Body(...), user=Depends(current)):
    label = body.get('label', '我的调用密钥')
    if set(body) - {'label'} or not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
        raise HTTPException(status_code=400, detail='密钥名称需为1至60字；不能修改平台、模型或IP策略')
    check_user(user)
    with store.transaction() as conn:
        count = conn.execute('SELECT COUNT(*) FROM portal_key_links l JOIN portal_keys k ON k.key_id=l.key_id WHERE l.owner_id=? AND k.revoked=0 AND k.owner_sv=?', (user['id'], user['sv'])).fetchone()[0]
        if count >= 5:
            raise HTTPException(status_code=409, detail='最多保留5个有效密钥，请先撤销不用的密钥')
    # Source provenance is committed with the key. An orphan is never an ordinary
    # unlimited manager key, even if portal-link creation or activation fails.
    with db.transaction() as conn:
        made = keysvc.create_key(label.strip(), models=list(portalpolicy.MODELS), realm='cn', platforms=['workbuddy'], _conn=conn, _portal=True)
        conn.execute('UPDATE api_keys SET source=?,enabled=0 WHERE id=?', (portalpolicy.SOURCE, made['id']))
    with store.transaction() as conn:
        fresh = conn.execute('SELECT sv,enabled FROM portal_users WHERE id=?', (user['id'],)).fetchone()
        if not fresh or not fresh['enabled'] or fresh['sv'] != user['sv']:
            raise HTTPException(status_code=401, detail='用户登录已失效')
        count = conn.execute('SELECT COUNT(*) FROM portal_key_links l JOIN portal_keys k ON k.key_id=l.key_id WHERE l.owner_id=? AND k.revoked=0 AND k.owner_sv=?', (user['id'], user['sv'])).fetchone()[0]
        if count >= 5:
            raise HTTPException(status_code=409, detail='最多保留5个有效密钥，请先撤销不用的密钥')
        conn.execute('INSERT INTO portal_key_links VALUES(?,?,?)', (made['id'], user['id'], keysvc._hash(made['key'])))
        conn.execute('INSERT INTO portal_keys VALUES(?,?,?,?,0)', (made['id'], user['sv'], label.strip(), store.now()))
    db.execute('UPDATE api_keys SET enabled=1 WHERE id=? AND source=?', (made['id'], portalpolicy.SOURCE))
    return {'id': made['id'], 'key': made['key'], 'label': label.strip(), 'message': '明文仅返回这一次，请妥善保存；多个密钥共享你的积分钱包'}


@router.delete('/keys/{key_id}')
def revoke_key(key_id: int, user=Depends(current)):
    with store.transaction() as conn:
        row = conn.execute('SELECT key_hash FROM portal_key_links WHERE key_id=? AND owner_id=?', (key_id, user['id'])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='密钥不存在')
        conn.execute('UPDATE portal_keys SET revoked=1 WHERE key_id=?', (key_id,))
    db.execute('UPDATE api_keys SET enabled=0 WHERE id=? AND key_hash=? AND source=?', (key_id, row['key_hash'], portalpolicy.SOURCE))
    return {'ok': True}


@router.post('/auth/start')
async def auth_start(user=Depends(current)):
    check_user(user)
    session = secrets.token_hex(24)
    with store.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) FROM portal_auth_sessions WHERE owner_id=? AND expires_at>? AND state IN ('starting','waiting','processing')", (user['id'], store.now())).fetchone()[0]
        if count >= 2:
            raise HTTPException(status_code=429, detail='请先完成或取消当前扫码')
        # Reserve capacity before awaiting the provider; concurrent tabs share it.
        conn.execute("INSERT INTO portal_auth_sessions(id,owner_id,owner_sv,upstream_state,expires_at,state) VALUES(?,?,?,?,?,'starting')",
                     (session, user['id'], user['sv'], '', store.now() + 300))
    upstream = ''
    try:
        with tencent.authorization_scope(lambda: check_user(user)):
            result = await tencent.start_login('cn', '')
        upstream = result.get('state') if isinstance(result, dict) else ''
        url = urlsplit(result.get('authUrl', '')) if isinstance(result, dict) else None
        if (not isinstance(upstream, str) or not 1 <= len(upstream) <= 2048 or url is None
                or url.scheme != 'https' or not url.hostname or url.username or url.password):
            raise ValueError('invalid provider authorization response')
        check_user(user)
        with store.transaction() as conn:
            current_user = conn.execute('SELECT enabled,sv FROM portal_users WHERE id=?', (user['id'],)).fetchone()
            if not current_user or not current_user['enabled'] or current_user['sv'] != user['sv']:
                raise HTTPException(status_code=401, detail='用户登录已失效')
            changed = conn.execute("UPDATE portal_auth_sessions SET upstream_state=?,state='waiting' WHERE id=? AND state='starting' AND expires_at>?",
                                   (upstream, session, store.now())).rowcount
            if not changed:
                raise HTTPException(status_code=409, detail='扫码准备已到期，请重新添加')
    except BaseException as exc:
        with store.transaction() as conn:
            conn.execute("UPDATE portal_auth_sessions SET state='failed' WHERE id=? AND state='starting'", (session,))
        if isinstance(upstream, str) and upstream:
            tencent.drop_state(upstream)
        if not isinstance(exc, Exception) or isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail='扫码准备暂时失败，请稍后重新添加；未更改账号或积分') from exc
    return {'session': session, 'auth_url': result['authUrl'], 'expires_in': 300}


@router.post('/auth/cancel')
def auth_cancel(body: dict = Body(...), user=Depends(current)):
    session = body.get('session')
    if not isinstance(session, str) or not re.fullmatch('[a-f0-9]{48}', session):
        raise HTTPException(status_code=404, detail='扫码会话不存在')
    with store.transaction() as conn:
        row = conn.execute('SELECT * FROM portal_auth_sessions WHERE id=? AND owner_id=? AND owner_sv=?', (session, user['id'], user['sv'])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='扫码会话不存在')
        if row['state'] == 'processing':
            raise HTTPException(status_code=409, detail='授权正在核对，关闭窗口不代表已取消上游授权')
        if row['state'] == 'complete':
            return {'ok': True, 'state': 'complete'}
        conn.execute("UPDATE portal_auth_sessions SET state='cancelled' WHERE id=?", (session,))
    tencent.drop_state(row['upstream_state'])
    return {'ok': True}


@router.post('/auth/poll')
async def auth_poll(body: dict = Body(...), user=Depends(current)):
    session = body.get('session')
    if not isinstance(session, str) or not re.fullmatch('[a-f0-9]{48}', session):
        raise HTTPException(status_code=404, detail='扫码会话不存在')
    with store.transaction() as conn:
        row = conn.execute('SELECT * FROM portal_auth_sessions WHERE id=? AND owner_id=? AND owner_sv=?', (session, user['id'], user['sv'])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='扫码会话不存在')
        if row['state'] == 'complete':
            return {'state': 'complete', 'account_id': row['account_id']}
        if row['state'] == 'failed':
            return {'state': 'failed', 'message': '授权处理未完成，请先刷新账号列表核对，不自动重复导入'}
        if row['state'] == 'processing':
            return {'state': 'processing', 'message': '授权结果正在核对，不重复处理'}
        if row['expires_at'] <= store.now() or row['state'] == 'cancelled':
            return {'state': 'expired'}
        if row['state'] == 'starting':
            return {'state': 'waiting'}
        if row['last_poll'] > store.now() - 2:
            return {'state': 'waiting'}
        conn.execute("UPDATE portal_auth_sessions SET state='processing',last_poll=? WHERE id=?", (store.now(), session))
    importing = False
    try:
        with tencent.authorization_scope(lambda: check_user(user)):
            result = await tencent.poll_login(row['upstream_state'], 'cn')
            check_user(user)
            if result.get('status') != 'ready':
                expired = result.get('status') in ('invalid', 'expired', 'realm_mismatch')
                with store.transaction() as conn:
                    conn.execute("UPDATE portal_auth_sessions SET state=? WHERE id=? AND state='processing'",
                                 ('cancelled' if expired else 'waiting', session))
                if expired:
                    tencent.drop_state(row['upstream_state'])
                return {'state': 'expired' if expired else 'waiting'}
            importing = True
            account = await service.import_ready(user, result)
        check_user(user)
        with store.transaction() as conn:
            conn.execute("UPDATE portal_auth_sessions SET state='complete',account_id=? WHERE id=?", (account['id'], session))
        tencent.drop_state(row['upstream_state'])
        return {'state': 'complete', **account}
    except BaseException as exc:
        # Provider reads may retry; an import may already have written credentials
        # or grants, so never replay it automatically after an uncertain failure.
        terminal = importing or isinstance(exc, HTTPException)
        with store.transaction() as conn:
            conn.execute("UPDATE portal_auth_sessions SET state=? WHERE id=? AND state='processing'",
                         ('failed' if terminal else 'waiting', session))
        if terminal:
            tencent.drop_state(row['upstream_state'])
        if not isinstance(exc, Exception) or isinstance(exc, HTTPException):
            raise
        if isinstance(exc, ValueError) and importing:
            raise HTTPException(status_code=409, detail='账号绑定未完成，请核对归属、授权状态与初始积分；不会自动重复导入') from exc
        raise HTTPException(status_code=502, detail='授权处理未完成，请先刷新账号列表核对，不自动重复导入' if importing
                            else '扫码连接暂时失败，可以重试当前二维码；未导入账号') from exc


@router.post('/accounts/{account_id}/checkin')
async def checkin(account_id: str, user=Depends(current)):
    try:
        return await service.checkin(account_id, 'user', user['id'], lambda: check_user(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='账号不存在') from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail='账号凭据或积分待核实，请联系管理员') from exc


@router.post('/accounts/{account_id}/join-public-pool')
def join_public_pool(account_id: str, user=Depends(current)):
    check_user(user)
    try:
        with store.transaction() as conn:
            store.require_account(conn, account_id, user['id'])
        joined = service.promote(account_id)
        return {'ok': joined, 'message': '账号已贡献到公共调度池' if joined else '本地公共池候选尚未启用，未写入调度目录'}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='账号不存在') from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail='未加入公共池：初始积分、引擎签到开关或同UID文件需管理员核对；不会覆盖现有账号') from exc


@router.post('/accounts/{account_id}/verify-initial')
async def verify_initial(account_id: str, user=Depends(current)):
    try:
        check_user(user)
        return await service.verify_initial(account_id, user['id'], lambda: check_user(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='账号不存在') from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail='上游积分及到期时间尚未核实') from exc


@admin_router.get('/users')
def users(admin=Depends(security.require_session_admin)):
    with store.transaction() as conn:
        rows = conn.execute('SELECT id,username,enabled,created_at FROM portal_users ORDER BY created_at DESC').fetchall()
    return {'users': [{**dict(row), **store.overview(row['id'])} for row in rows],
            'automatic_checkin': store.automatic_enabled()}


@admin_router.get('/wallet-pending')
def wallet_pending(page: int = 1, admin=Depends(security.require_session_admin)):
    from .. import portalwallet
    return portalwallet.pending_page(page)


def require_bonus_admin(admin=Depends(security.require_session_admin)):
    if admin.get('username') != 'admin' or admin.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='仅超级管理员admin本人会话可赠送积分')
    return admin


@admin_router.post('/users/{owner_id}/bonus')
def give_bonus(owner_id: str, body: dict = Body(...), admin=Depends(require_bonus_admin)):
    from .. import portalwallet
    if set(body) != {'action_id', 'credit', 'valid_days', 'reason'}:
        raise HTTPException(status_code=400, detail='需要赠送标识、金额、有效天数和原因；不支持信用或透支参数')
    try:
        with security.authorization_guard(admin):
            result = portalwallet.grant_bonus(owner_id, body['action_id'], body['credit'], body['valid_days'], admin['username'], body['reason'])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {'ok': True, **result, 'message': '该赠送已入账，不重复增加积分' if result['already'] else '赠送积分已入账；计费调用仍需通过不透支门禁，不解除费用未知冻结或抹除历史超额费用'}


@admin_router.get('/wallet-debts')
def wallet_debts(page: int = 1, admin=Depends(security.require_session_admin)):
    from .. import portalwallet
    return portalwallet.debts_page(page)


@admin_router.get('/wallet-history')
def wallet_history(page: int = 1, admin=Depends(security.require_session_admin)):
    from .. import portalwallet
    return portalwallet.history_page(page)


@admin_router.get('/wallet-platform-costs')
def wallet_platform_costs(page: int = 1, admin=Depends(security.require_session_admin)):
    from .. import portalwallet
    return portalwallet.platform_costs_page(page)


@admin_router.post('/wallet-debts/{owner_id}/adjust')
def wallet_adjust(owner_id: str, body: dict = Body(...), admin=Depends(security.require_session_admin)):
    from .. import portalwallet
    if set(body) != {'action_id', 'kind', 'credit', 'expected_units', 'reason', 'evidence'}:
        raise HTTPException(status_code=400, detail='需要处置标识、方式、金额、原欠额、原因与凭证说明')
    try:
        with security.authorization_guard(admin):
            result = portalwallet.adjust_debt(owner_id, body['action_id'], body['kind'], body['credit'], body['expected_units'],
                admin['username'], body['reason'], body['evidence'])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {'ok': True, **result, 'message': '该处置已提交，不重复改账' if result['already'] else '欠额处置已入账，保留原因与凭证；没有增加可用积分或修改原调用费用'}


@admin_router.post('/wallet-pending/{request_id}/resolve')
def wallet_resolve(request_id: str, body: dict = Body(...), admin=Depends(security.require_session_admin)):
    from .. import portalwallet
    if set(body) != {'credit', 'reason', 'evidence'}:
        raise HTTPException(status_code=400, detail='需要真实积分金额、核对原因与凭证说明')
    try:
        with security.authorization_guard(admin):
            portalwallet.resolve_unknown(request_id, body['credit'], admin['username'], body['reason'], body['evidence'])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {'ok': True, 'message': '已按核对凭证结算，保留操作者、金额、原因与证据说明'}


@router.post('/accounts/{account_id}/reconcile-checkin')
async def reconcile_checkin(account_id: str, user=Depends(current)):
    try:
        return await service.reconcile_checkin(account_id, user['id'], lambda: check_user(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='账号不存在') from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc) if isinstance(exc, ValueError) else '账号凭据待核实') from exc


@router.post('/checkin-all')
async def checkin_all(user=Depends(current)):
    with store.transaction() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM portal_accounts WHERE owner_id=? AND status='active' ORDER BY created_at,id LIMIT 100", (user['id'],))]
    results = await service.checkin_many(ids, 'user-batch', user['id'], lambda: check_user(user))
    return {'results': results, 'count': len(results), 'message': service.batch_message(results)}


@admin_router.post('/users')
async def create_user(body: dict = Body(...), admin=Depends(security.require_session_admin)):
    name, password = body.get('username'), body.get('password')
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{3,40}', name):
        raise HTTPException(status_code=400, detail='用户名需为3至40位字母、数字、下划线或短横线')
    validate_portal_password(password, name)
    hashed = await joined_thread(security.make_hash, password)
    with security.authorization_guard(admin), store.transaction() as conn:
        require_writable(conn)
        if conn.execute('SELECT 1 FROM portal_users WHERE username=?', (name,)).fetchone():
            raise HTTPException(status_code=409, detail='用户名已存在')
        user_id = secrets.token_hex(16)
        conn.execute('INSERT INTO portal_users(id,username,pwd_hash,created_at) VALUES(?,?,?,?)', (user_id, name, hashed, store.now()))
    return {'id': user_id, 'username': name}


def validate_portal_password(password, username=''):
    if not isinstance(password, str):
        raise HTTPException(status_code=400, detail='用户登录密码必须是字符串')
    if len(password) > 128:
        raise HTTPException(status_code=400, detail='用户登录密码最多 128 位')
    problem = security.managed_user_password_problem(password, username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)


@admin_router.post('/accounts/{account_id}/checkin')
async def admin_checkin(account_id: str, admin=Depends(security.require_session_admin)):
    try:
        return await service.checkin(account_id, 'admin', authorize=lambda: security.authorization_owner(admin))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='账号不存在') from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail='账号凭据或积分待核实') from exc


@admin_router.patch('/users/{user_id}')
async def update_user(user_id: str, body: dict = Body(...), admin=Depends(security.require_session_admin)):
    if set(body) - {'enabled', 'password'} or not body:
        raise HTTPException(status_code=400, detail='仅支持停用、启用或重置密码')
    if 'enabled' in body and type(body['enabled']) is not bool:
        raise HTTPException(status_code=400, detail='enabled 必须为布尔值')
    password = body.get('password')
    if 'password' in body:
        validate_portal_password(password)
    hashed = await joined_thread(security.make_hash, password) if password is not None else None
    with security.authorization_guard(admin), store.transaction() as conn:
        require_writable(conn)
        row = conn.execute('SELECT * FROM portal_users WHERE id=?', (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='用户不存在')
        if password is not None:
            validate_portal_password(password, row['username'])
        conn.execute('UPDATE portal_users SET enabled=?,pwd_hash=?,sv=sv+1 WHERE id=?',
                     (int(body.get('enabled', row['enabled'])), hashed or row['pwd_hash'], user_id))
        if hashed:
            conn.execute('UPDATE portal_users SET failed_logins=0,blocked_until=0 WHERE id=?', (user_id,))
    return {'ok': True, 'sessions_revoked': True}


@admin_router.post('/accounts/{account_id}/reconcile-checkin')
async def admin_reconcile_checkin(account_id: str, admin=Depends(security.require_session_admin)):
    try:
        return await service.reconcile_checkin(account_id, authorize=lambda: security.authorization_owner(admin))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail='账号不存在') from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail='没有可安全补记的成功签到回执') from exc


@admin_router.put('/automatic-checkin')
def automatic_checkin(body: dict = Body(...), admin=Depends(security.require_session_admin)):
    if set(body) != {'enabled'} or type(body['enabled']) is not bool:
        raise HTTPException(status_code=400, detail='enabled 必须为布尔值')
    with security.authorization_guard(admin), store.transaction() as conn:
        require_writable(conn)
        conn.execute("INSERT INTO portal_settings VALUES('automatic_checkin',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ('true' if body['enabled'] else 'false',))
    return {'ok': True, 'enabled': body['enabled']}


@admin_router.post('/checkin-all')
async def admin_checkin_all(admin=Depends(security.require_session_admin)):
    with store.transaction() as conn:
        ids = [row[0] for row in conn.execute("SELECT a.id FROM portal_accounts a JOIN portal_users u ON u.id=a.owner_id WHERE u.enabled=1 AND a.status='active' ORDER BY a.created_at,a.id LIMIT 100")]
    results = await service.checkin_many(ids, 'admin-batch', authorize=lambda: security.authorization_owner(admin))
    return {'count': len(results), 'results': results, 'message': service.batch_message(results)}
