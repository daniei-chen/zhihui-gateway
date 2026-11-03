"""User-owned WorkBuddy authorization and one shared check-in operation."""
from __future__ import annotations

import json
import asyncio
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from contextlib import nullcontext

from fastapi import HTTPException

from . import config, portalpolicy, portalstore as store
from .services import credits, tencent, wb2api


def credential_dir() -> Path:
    # Deliberately outside the existing engine's watched auth directory.
    path = config.DATA_DIR / 'user-portal-auths'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def pool_files(uid):
    if not config.AUTH_DIR.exists():
        return []
    if config.AUTH_DIR.is_symlink():
        raise ValueError('公共池目录异常')
    matches = []
    for index, path in enumerate(config.AUTH_DIR.iterdir()):
        if index >= 20000:
            raise ValueError('公共池文件数量需管理员核对')
        if path.name.startswith('workbuddy') and (path.name.endswith('.json') or path.name.endswith('.json.disabled')):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError('公共池文件异常，停止自动认领')
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or not isinstance(raw.get('account'), dict):
                raise ValueError('公共池账号格式待管理员核对')
            if str(raw['account'].get('uid')) == uid:
                matches.append(path)
    return matches


def save_credentials(account: dict, result: dict):
    path = credential_dir() / (account['id'] + '.json')
    payload = {key: result.get(key) for key in ('uid', 'nickname', 'enterprise_id', 'access_token',
                                               'refresh_token', 'expires_at', 'domain', 'realm', 'proxy')}
    if path.is_symlink():
        raise ValueError('账号存储异常')
    fd, temporary = tempfile.mkstemp(prefix='.portal-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_credentials(account_id: str, *, _conn=None) -> dict:
    if not re.fullmatch('[a-f0-9]{32}', account_id):
        raise ValueError('账号标识无效')
    with (nullcontext(_conn) if _conn is not None else store.transaction()) as conn:
        link = conn.execute('SELECT filename FROM portal_pool_links WHERE account_id=?', (account_id,)).fetchone()
        account = dict(store.require_account(conn, account_id)) if link else None
    if link:
        if (not re.fullmatch(r'workbuddy-[0-9A-Za-z_-]{1,80}\.json', link['filename'])
                or link['filename'] != f'workbuddy-{account["provider_uid"]}.json'
                or config.AUTH_DIR.is_symlink()):
            raise ValueError('公共池账号路径异常')
        path = config.AUTH_DIR / link['filename']
        if path.is_symlink():
            raise ValueError('公共池账号文件异常')
        raw = json.loads(path.read_text(encoding='utf-8'))
        acct, auth = raw.get('account') or {}, raw.get('auth') or {}
        if str(acct.get('uid')) != account['provider_uid'] or acct.get('enterpriseId') or auth.get('realm') != 'cn':
            raise ValueError('公共池账号身份发生变化，未使用凭据')
        return {'uid': str(acct['uid']), 'nickname': acct.get('nickname', ''), 'enterprise_id': '',
                'access_token': auth.get('accessToken', ''), 'refresh_token': auth.get('refreshToken', ''),
                'expires_at': auth.get('expiresAt'), 'domain': auth.get('domain', ''), 'realm': 'cn',
                'proxy': raw.get('proxy', ''), 'device_token': raw.get('device_token', '')}
    path = credential_dir() / (account_id + '.json')
    if path.is_symlink():
        raise ValueError('账号存储异常')
    return json.loads(path.read_text(encoding='utf-8'))


def publication_credentials(account):
    local = credential_dir() / (account['id'] + '.json')
    info = local.lstat()
    if not stat.S_ISREG(info.st_mode) or local.is_symlink() or info.st_nlink != 1 or info.st_size > 1024 * 1024:
        raise ValueError('账号凭据文件异常')
    auth = json.loads(local.read_text(encoding='utf-8'))
    if (not isinstance(auth, dict) or str(auth.get('uid') or '') != account['provider_uid']
            or auth.get('realm') != 'cn' or auth.get('enterprise_id')
            or not isinstance(auth.get('access_token'), str) or not auth['access_token'].strip()
            or (auth.get('expires_at') is not None and (type(auth['expires_at']) not in (int, float)
                or not auth['expires_at'] > store.now() + 60))):
        raise ValueError('账号身份或授权已变化，请核对后再入池')
    return auth


def promote(account_id):
    """Exclusive, recoverable publication; never overwrite an existing pool file.

    Keep the engine and Manager on ONE credential file after promotion. Existing
    engine-owned renewal remains authoritative; portal never overwrites it.
    """
    if not portalpolicy.enabled():
        return False
    # The engine scheduler does not participate in our SQL check-in claims.
    # Its independent signing MUST be off before candidate pool publication.
    schedule = (wb2api.load_upstream_config().get('schedule') or {})
    if schedule.get('checkin_enabled') is not False:
        raise ValueError('公共池接入要求先关闭引擎独立签到，由共享签到入口统一执行')
    with store.transaction() as conn:
        store.require_not_quarantined(conn)
        account = dict(store.require_account(conn, account_id))
        owner = conn.execute('SELECT enabled FROM portal_users WHERE id=?', (account['owner_id'],)).fetchone()
        if not owner or not owner[0] or account['status'] != 'active' or account['initial_state'] != 'confirmed':
            raise ValueError('用户或初始积分尚未核实，未加入公共池')
        if not re.fullmatch(r'[0-9A-Za-z_-]{1,80}', account['provider_uid']):
            raise ValueError('账号身份无效')
        link = conn.execute('SELECT filename FROM portal_pool_links WHERE account_id=?', (account_id,)).fetchone()
        if link:
            load_credentials(account_id, _conn=conn)
            return True
        if pool_files(account['provider_uid']):
            # Existing publication intents may recover their own file below.
            intent = conn.execute('SELECT 1 FROM portal_pool_intents WHERE account_id=?', (account_id,)).fetchone()
            if not intent:
                raise ValueError('公共池已有同一UID，未认领现有资产')
        filename = f'workbuddy-{account["provider_uid"]}.json'
        conn.execute('INSERT OR IGNORE INTO portal_pool_intents VALUES(?,?,?,?)', (account_id, filename, secrets.token_hex(24), store.now()))
    with store.transaction() as conn:
        # Intent survives both file publication and a later SQL commit failure.
        store.require_not_quarantined(conn)
        account = dict(store.require_account(conn, account_id))
        owner = conn.execute('SELECT enabled FROM portal_users WHERE id=?', (account['owner_id'],)).fetchone()
        if not owner or not owner[0] or account['status'] != 'active' or account['initial_state'] != 'confirmed':
            raise ValueError('用户或账号状态已变化，未发布账号')
        intent = conn.execute('SELECT * FROM portal_pool_intents WHERE account_id=?', (account_id,)).fetchone()
        base = config.AUTH_DIR
        base.mkdir(parents=True, exist_ok=True)
        filename = intent['filename']
        if filename != f'workbuddy-{account["provider_uid"]}.json':
            raise ValueError('公共池发布意图路径异常')
        target = base / filename
        if any(path != target for path in pool_files(account['provider_uid'])):
            raise ValueError('公共池其他文件已包含同一UID，未重复接入')
        if target.exists() or target.is_symlink() or (base / (filename + '.disabled')).exists():
            if target.is_symlink() or (base / (filename + '.disabled')).exists():
                raise ValueError('公共池同一账号需管理员核对，未覆盖')
            raw = json.loads(target.read_text(encoding='utf-8'))
            acct = raw.get('account') if isinstance(raw, dict) else None
            shared_auth = raw.get('auth') if isinstance(raw, dict) else None
            if (not isinstance(acct, dict) or not isinstance(shared_auth, dict)
                    or raw.get('portal_publication') != intent['marker'] or str(acct.get('uid')) != account['provider_uid']
                    or acct.get('enterpriseId') or shared_auth.get('realm') != 'cn'):
                raise ValueError('公共池已存在同一账号，未覆盖；请管理员核对')
            conn.execute('INSERT OR IGNORE INTO portal_pool_links VALUES(?,?,?)', (account_id, filename, store.now()))
            return True
        auth = publication_credentials(account)
        payload = {'account': {'uid': account['provider_uid'], 'enterpriseId': '', 'nickname': auth.get('nickname', '')},
            'auth': {'accessToken': auth['access_token'], 'refreshToken': auth.get('refresh_token', ''),
                     'expiresAt': auth.get('expires_at'), 'domain': auth.get('domain', ''), 'realm': 'cn'},
            'portal_publication': intent['marker']}
        if auth.get('proxy'):
            payload['proxy'] = auth['proxy']
        fd, name = tempfile.mkstemp(prefix='.portal-pool-', suffix='.tmp', dir=base)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o644)
            # link is atomic no-clobber (unlike exists + replace).
            os.link(name, target)
            conn.execute('INSERT INTO portal_pool_links VALUES(?,?,?)', (account_id, filename, store.now()))
        finally:
            Path(name).unlink(missing_ok=True)
    # The isolated original is no longer read/written after a pool link exists.
    return True


def publish_confirmed_accounts():
    """Recover only explicit customer-owned publication; never infer old pool ownership."""
    if not portalpolicy.enabled():
        return {'published': 0, 'pending': 0}
    with store.transaction() as conn:
        store.require_not_quarantined(conn)
        accounts = [row[0] for row in conn.execute("SELECT a.id FROM portal_accounts a JOIN portal_users u ON u.id=a.owner_id "
            "LEFT JOIN portal_pool_links p ON p.account_id=a.id WHERE u.enabled=1 AND a.status='active' "
            "AND a.initial_state='confirmed' AND p.account_id IS NULL ORDER BY a.created_at,a.id LIMIT 1000")]
    published = 0
    for account_id in accounts:
        try:
            published += bool(promote(account_id))
        except (ValueError, OSError):
            logging.getLogger(__name__).warning('portal account publication pending; details withheld')
    return {'published': published, 'pending': len(accounts) - published}


async def import_ready(owner: dict, result: dict) -> dict:
    uid = str(result.get('uid') or '')
    if not re.fullmatch(r'[0-9A-Za-z_-]{1,80}', uid) or result.get('realm') != 'cn' or result.get('enterprise_id'):
        raise ValueError('首版仅接入支持每日签到的 WorkBuddy 国内个人账号')
    with store.transaction() as conn:
        current = conn.execute('SELECT enabled,sv FROM portal_users WHERE id=?', (owner['id'],)).fetchone()
        if not current or not current['enabled'] or current['sv'] != owner['sv']:
            raise HTTPException(status_code=401, detail='登录已失效')
    # Never claim credentials already in the pre-existing administrator pool.
    with store.transaction() as conn:
        pooled = conn.execute('SELECT a.id,a.owner_id FROM portal_accounts a JOIN portal_pool_links p ON p.account_id=a.id WHERE a.provider_uid=?', (uid,)).fetchone()
    if pooled:
        if pooled['owner_id'] != owner['id']:
            raise ValueError('该账号已绑定其他用户')
        raise ValueError('账号已在公共池；请总台更新授权，用户端不会覆盖引擎续期凭据或重复入账')
    exact = config.AUTH_DIR / f'workbuddy-{uid}.json'
    if exact.exists() or exact.is_symlink() or (config.AUTH_DIR / f'workbuddy-{uid}.json.disabled').exists() or pool_files(uid):
        raise ValueError('该账号已在现有总池中，需管理员核对归属；不会重复入账')
    nickname = str(result.get('nickname') or '').strip()
    label = nickname[:1] + '***' + nickname[-1:] if nickname else 'WorkBuddy 账号'
    account = store.reserve_account(owner['id'], uid, label, owner['sv'])
    with store.transaction() as conn:
        current = conn.execute('SELECT enabled,sv FROM portal_users WHERE id=?', (owner['id'],)).fetchone()
        if not current or not current['enabled'] or current['sv'] != owner['sv']:
            raise HTTPException(status_code=401, detail='登录已失效')
        in_flight = conn.execute("SELECT 1 FROM portal_checkins WHERE account_id=? AND state IN ('running','unknown','confirmed_uncredited') LIMIT 1", (account['id'],)).fetchone()
        if in_flight:
            raise ValueError('账号存在进行中或结果未知的签到，请先核对再更新授权')
        save_credentials(account, result)
    if account['initial_state'] != 'confirmed':
        ok, total, _, expiries = await tencent.fetch_credits(result)
        try:
            buckets = store.snapshot(ok, total, expiries)
        except ValueError:
            return {'id': account['id'], 'initial_state': 'pending', 'message': '已绑定，初始积分及到期明细待核实'}
        with store.transaction() as conn:
            current = conn.execute('SELECT enabled,sv FROM portal_users WHERE id=?', (owner['id'],)).fetchone()
            if not current or not current['enabled'] or current['sv'] != owner['sv']:
                raise HTTPException(status_code=401, detail='登录已失效，初始积分未提交')
        store.initial_credit(account['id'], buckets, owner['sv'])
    try:
        pooled = promote(account['id'])
    except (ValueError, OSError):
        pooled = False
    return {'id': account['id'], 'initial_state': 'confirmed', 'pool_state': 'public' if pooled else 'pending',
            'message': '账号已添加，初始积分按原到期时间入账；' + ('已贡献公共池' if pooled else '公共池接入待管理员核对，暂不可用于调用')}


async def verify_initial(account_id: str, owner_id=None, authorize=None):
    with store.transaction() as conn:
        account = dict(store.require_account(conn, account_id, owner_id))
        owner_sv = conn.execute('SELECT sv FROM portal_users WHERE id=?', (account['owner_id'],)).fetchone()[0]
    if account['initial_state'] == 'confirmed':
        promote(account_id)
        return {'ok': True, 'message': '初始积分已经核实，不重复入账'}
    auth = load_credentials(account_id)
    ok, total, _, expiries = await tencent.fetch_credits(auth)
    buckets = store.snapshot(ok, total, expiries)
    if authorize:
        authorize()
    store.initial_credit(account_id, buckets, owner_sv)
    promote(account_id)
    return {'ok': True, 'message': '初始积分已按原到期时间入账'}


async def live_credentials(account, authorize=None):
    auth = load_credentials(account['id'])
    expiry = auth.get('expires_at')
    if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and expiry <= store.now() + 300:
        with store.transaction() as conn:
            pooled = conn.execute('SELECT 1 FROM portal_pool_links WHERE account_id=?', (account['id'],)).fetchone()
        if pooled:
            raise ValueError('公共池凭据即将过期，请由总台或引擎续期，用户端不覆盖共享凭据')
        if authorize:
            authorize()
        ok, _, fields = await tencent.refresh_token(auth)
        if not ok:
            raise ValueError('账号授权需重新扫码，未发起签到')
        if authorize:
            authorize()
        with store.transaction() as conn:
            owner = conn.execute('SELECT enabled FROM portal_users WHERE id=?', (account['owner_id'],)).fetchone()
            if not owner or not owner['enabled'] or load_credentials(account['id'], _conn=conn) != auth:
                raise ValueError('授权已更新，旧续期结果未覆盖新凭据')
            auth = {**auth, **fields}
            save_credentials(account, auth)
    return auth


async def checkin(account_id: str, actor: str, owner_id=None, authorize=None) -> dict:
    with store.transaction() as conn:
        account = dict(store.require_account(conn, account_id, owner_id))
    if authorize:
        authorize()
    claimed, claim = store.claim_checkin(account_id, actor)
    if not claimed:
        return {'state': claim['state'], 'already': True, 'message': '今日状态已同步，不重复发起签到'}
    today = claim['day']
    dispatched = False
    confirmed = False
    try:
        auth = await live_credentials(account, authorize)
        if authorize:
            authorize()
        ok, total, _, expiries = await tencent.fetch_credits(auth)
        before = store.snapshot(ok, total, expiries)
        store.checkin_state(account_id, today, 'running', baseline=before)
        if authorize:
            authorize()
        dispatched = True
        token = tencent.portal_checkin_active.set(True)
        try:
            code, _ = await tencent.checkin(auth, 'cn')
        finally:
            tencent.portal_checkin_active.reset(token)
        if code not in (0, 10001):
            store.checkin_state(account_id, today, 'unknown' if code == -1 else 'failed', code=code)
            return {'state': 'unknown' if code == -1 else 'failed', 'message': '签到结果待确认' if code == -1 else '签到未成功，可稍后重试'}
        # Persist success before the balance read: a read failure must not cause another RPC.
        store.checkin_state(account_id, today, 'confirmed_uncredited', code=code)
        confirmed = True
        credits.invalidate(account['provider_uid'])
        ok, total, _, expiries = await tencent.fetch_credits(auth)
        try:
            after = store.snapshot(ok, total, expiries)
        except ValueError:
            return {'state': 'confirmed_uncredited', 'message': '今日已签到，到账金额待核实'}
        store.finish_checkin(account_id, today, code, before, after, claim['started_at'])
        with store.transaction() as conn:
            row = conn.execute('SELECT state,credited_units FROM portal_checkins WHERE account_id=? AND day=?', (account_id, today)).fetchone()
        return {'state': row['state'], 'credited_units': row['credited_units'],
                'message': '签到成功，100积分已入账' if row['credited_units'] else '今日已签到，金额需核对，不重复赠送'}
    except BaseException:
        if not confirmed:
            store.checkin_state(account_id, today, 'unknown' if dispatched else 'failed')
        raise


async def reconcile_checkin(account_id, owner_id=None, authorize=None):
    with store.transaction() as conn:
        store.require_account(conn, account_id, owner_id)
        row = conn.execute('SELECT * FROM portal_checkins WHERE account_id=? AND day=?', (account_id, store.day())).fetchone()
        row = dict(row) if row else None
    if not row or row['state'] != 'confirmed_uncredited' or row['code'] != 0:
        raise ValueError('没有可安全补记的成功回执；结果未知或此前已签到不能猜测赠额')
    if authorize:
        authorize()
    auth = load_credentials(account_id)
    ok, total, _, expiries = await tencent.fetch_credits(auth)
    after = store.snapshot(ok, total, expiries)
    if authorize:
        authorize()
    before = {int(expiry): amount for expiry, amount in json.loads(row['baseline']).items()}
    store.finish_checkin(account_id, row['day'], 0, before, after, row['started_at'])
    return {'ok': True, 'message': '已重新核对签到到账记录；无法确认的金额仍不入账'}


async def checkin_many(account_ids, actor, owner_id=None, authorize=None):
    results = []
    for account_id in account_ids:
        if authorize:
            authorize()
        try:
            result = await checkin(account_id, actor, owner_id, authorize)
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).warning('portal batch account pending; details withheld')
            result = {'state': 'pending', 'message': '该账号需核对；未自动重试，不影响其余账号'}
        results.append({**result, 'account_id': account_id})
    return results


def batch_message(results):
    confirmed = sum(row.get('state') == 'confirmed' for row in results)
    return f'本批处理 {len(results)} 个账号，{confirmed} 个已核实签到，其余请查看账号状态；未知结果不重复发起'


async def automatic_once():
    from . import portalwallet
    from .services.asyncwork import joined_thread
    portalwallet.recover()
    try:
        await joined_thread(publish_confirmed_accounts)
    except Exception:
        logging.getLogger(__name__).warning('portal pool publication recovery paused; details withheld')
    if not store.automatic_enabled():
        return 0
    with store.transaction() as conn:
        ids = [row[0] for row in conn.execute("SELECT a.id FROM portal_accounts a JOIN portal_users u ON u.id=a.owner_id "
            "LEFT JOIN portal_checkins c ON c.account_id=a.id AND c.day=? "
            "WHERE u.enabled=1 AND a.status='active' AND (c.state IS NULL OR c.state='failed') ORDER BY a.created_at LIMIT 100", (store.day(),))]
    completed = 0
    for account_id in ids:
        if not store.automatic_enabled():
            break
        def authorized():
            with store.transaction() as conn:
                account = store.require_account(conn, account_id)
                owner = conn.execute('SELECT enabled FROM portal_users WHERE id=?', (account['owner_id'],)).fetchone()
                setting = conn.execute("SELECT value FROM portal_settings WHERE key='automatic_checkin'").fetchone()
                if not owner or not owner['enabled'] or not setting or setting[0] != 'true':
                    raise ValueError('自动签到或用户已停用')
        try:
            await checkin(account_id, 'automatic', authorize=authorized)
            completed += 1
        except Exception:
            logging.getLogger(__name__).warning('portal automatic check-in pending; no credentials logged')
    return completed


async def automatic_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await automatic_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning('portal automatic check-in paused after local error; details withheld')
