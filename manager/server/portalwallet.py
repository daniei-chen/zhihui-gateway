"""User-wide conservative reservations and main-ledger settlement outbox.

No local supplier price table exists. One user request holds the entire currently
available wallet, across all keys. This prevents concurrent overspend, not supplier
overbilling: an actual overrun becomes debt and blocks subsequent dispatch. Unknown
charges never age out. Separate SQLite databases do NOT form one transaction;
committed main-ledger receipts are replayed idempotently into the wallet.
"""
from __future__ import annotations

import json
import logging
import re

from . import budgetowner, db, observability, portalbilling, portalpolicy as policy, portalstore as store

RECEIPTS = '''CREATE TABLE IF NOT EXISTS portal_spend_receipts(
 request_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, payload TEXT NOT NULL
)'''


def reject(message, code='portal_wallet_unavailable'):
    from .budget import BudgetRejected
    raise BudgetRejected(message, code)


def reserve(key):
    if not policy.is_portal(key):
        return
    state = observability.current.get()
    if state is None:
        reject('缺少请求审计上下文')
    try:
        with store.transaction() as conn:
            owner = policy.identity(key, conn)
            if not policy.enabled():
                reject('用户公共池调用尚未启用')
            store.expire(conn, owner, store.now())
            if conn.execute("SELECT 1 FROM portal_spends WHERE owner_id=? AND state IN ('reserved','forwarded','unknown')", (owner,)).fetchone():
                reject('同一用户有进行中或费用待核对的请求', 'portal_wallet_pending')
            debt = conn.execute('SELECT amount_units FROM portal_debts WHERE owner_id=?', (owner,)).fetchone()
            if debt and debt[0] > 0:
                reject('存在超额费用待处理，暂停新请求', 'portal_wallet_debt')
            rows = store.spendable_grants(conn, owner, store.now())
            held = sum(row['remaining_units'] for row in rows)
            if not held:
                reject('额度已用尽，请联系管理员赠送额度', 'portal_wallet_empty')
            portalbilling.require_verified_cap()
            conn.execute("INSERT INTO portal_spends VALUES(?,?,?,?,? ,?,?,NULL,NULL)",
                (state.request_id, owner, key['id'], budgetowner.identity(), 'reserved', store.now(), held))
            for row in rows:
                table, allocations = ('portal_bonus_grants', 'portal_bonus_allocations') if row['kind'] == 'bonus' else ('portal_grants', 'portal_allocations')
                conn.execute(f'INSERT INTO {allocations} VALUES(?,?,?)', (state.request_id, row['id'], row['remaining_units']))
                conn.execute(f'UPDATE {table} SET remaining_units=0 WHERE id=?', (row['id'],))
        state.portal_owner = owner
        state.portal_credential_hash = key.get('credential_hash', '')
    except (ValueError, OSError) as exc:
        reject('用户积分或授权不可用，未发送请求')


def forwarded(state):
    if not getattr(state, 'portal_owner', None):
        return
    portalbilling.require_verified_cap()
    try:
        with store.transaction() as conn:
            # Recheck owner generation and revocation at the durable send boundary.
            owner = policy.identity({'id': state.key_id, 'credential_hash': getattr(state, 'portal_credential_hash', '')}, conn)
            if owner != state.portal_owner:
                reject('用户积分归属发生变化')
            if not policy.enabled():
                reject('用户公共池调用已关闭')
            changed = conn.execute("UPDATE portal_spends SET state='forwarded' WHERE request_id=? AND owner_id=? AND worker=? AND state='reserved'",
                (state.request_id, state.portal_owner, budgetowner.identity())).rowcount
            if changed != 1:
                reject('用户积分预留归属已变更')
    except Exception:
        reject('用户积分发送许可不可用，未发送请求')


def receipt(state, usage, credit):
    """Called ONLY inside the same main DB transaction as request_logs."""
    owner = getattr(state, 'portal_owner', None)
    if not owner:
        return
    amount = None
    if (state.terminal_state == 'completed' and isinstance(usage, dict)
            and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens'))):
        try:
            if credit is not None:
                amount = store.units(credit)
        except ValueError:
            pass
    db.execute('INSERT OR IGNORE INTO portal_spend_receipts VALUES(?,?,?)',
        (state.request_id, owner, json.dumps({'amount_units': amount})))


def replay_receipt(entry, facts):
    owner = (entry.get('budget') or {}).get('portal_owner')
    if not isinstance(owner, str) or not owner:
        return
    amount = None
    if facts.get('terminal_state') == 'completed' and facts.get('usage_complete') == 1:
        try:
            if facts.get('credit') is not None:
                amount = store.units(facts['credit'])
        except ValueError:
            pass
    # A delayed/spooled fact must not silently disagree with an operator's
    # immutable resolution. Caller rolls back replay and retains the evidence.
    with store.transaction() as conn:
        resolution = conn.execute('SELECT r.amount_units,s.owner_id FROM portal_spend_resolutions r '
            'JOIN portal_spends s ON s.request_id=r.request_id WHERE r.request_id=?', (facts['request_id'],)).fetchone()
        if resolution and (resolution['owner_id'] != owner or (amount is not None and resolution['amount_units'] != amount)):
            raise ValueError('迟到费用回执与人工核对结果冲突，保留回执等待复核')
    db.execute('INSERT OR IGNORE INTO portal_spend_receipts VALUES(?,?,?)',
        (facts['request_id'], owner, json.dumps({'amount_units': amount})))


def _release(conn, row, charge):
    left = charge
    allocations = conn.execute("SELECT g.id,g.account_id,g.expires_at,a.amount_units AS held,'account' AS kind "
        'FROM portal_allocations a JOIN portal_grants g ON g.id=a.grant_id WHERE a.request_id=? '
        "UNION ALL SELECT g.id,NULL AS account_id,g.expires_at,a.amount_units AS held,'bonus' AS kind "
        'FROM portal_bonus_allocations a JOIN portal_bonus_grants g ON g.id=a.grant_id WHERE a.request_id=? '
        'ORDER BY expires_at,id,kind', (row['request_id'], row['request_id'])).fetchall()
    for grant in allocations:
        spent = min(left, grant['held'])
        left -= spent
        returned = grant['held'] - spent
        if returned and grant['expires_at'] > store.now():
            table = 'portal_bonus_grants' if grant['kind'] == 'bonus' else 'portal_grants'
            conn.execute(f'UPDATE {table} SET remaining_units=remaining_units+? WHERE id=?', (returned, grant['id']))
        elif returned:
            conn.execute('INSERT OR IGNORE INTO portal_events(owner_id,account_id,kind,amount_units,ts,expires_at,source_ref) VALUES(?,?,?,?,?,?,?)',
                (row['owner_id'], grant['account_id'], 'expired', -returned, store.now(), grant['expires_at'], 'held-expiry:' + row['request_id'] + ':' + grant['id']))
    if left:
        conn.execute('INSERT INTO portal_debts VALUES(?,?) ON CONFLICT(owner_id) DO UPDATE SET amount_units=amount_units+excluded.amount_units', (row['owner_id'], left))
    if charge:
        conn.execute('INSERT OR IGNORE INTO portal_events(owner_id,kind,amount_units,ts,source_ref) VALUES(?,?,?,?,?)',
            (row['owner_id'], 'usage', -charge, store.now(), 'spend:' + row['request_id']))


def apply(request_id):
    # Read main DB before opening portal DB: no reverse nested database locks.
    receipt = db.query_one('SELECT owner_id,payload FROM portal_spend_receipts WHERE request_id=?', (request_id,))
    with store.transaction() as conn:
        store.require_not_quarantined(conn)
        row = conn.execute('SELECT * FROM portal_spends WHERE request_id=?', (request_id,)).fetchone()
        if not row or row['state'] in ('settled', 'released'):
            return
        if row['state'] == 'reserved' and (row['worker'] == budgetowner.identity() or budgetowner.alive(row['worker']) is False):
            _release(conn, row, 0)
            conn.execute("UPDATE portal_spends SET state='released',settled_at=? WHERE request_id=?", (store.now(), request_id))
        elif receipt:
            if receipt['owner_id'] != row['owner_id']:
                raise ValueError('settlement owner mismatch')
            amount = json.loads(receipt['payload'])['amount_units']
            if type(amount) is int and amount >= 0:
                _release(conn, row, amount)
                conn.execute("UPDATE portal_spends SET state='settled',charged_units=?,settled_at=? WHERE request_id=?", (amount, store.now(), request_id))
            else:
                conn.execute("UPDATE portal_spends SET state='unknown' WHERE request_id=?", (request_id,))
        elif row['state'] == 'forwarded':
            conn.execute("UPDATE portal_spends SET state='unknown' WHERE request_id=?", (request_id,))


def finish(state):
    if getattr(state, 'portal_owner', None):
        try:
            apply(state.request_id)
        except Exception:
            # A failed apply leaves the durable reservation blocking the user.
            logging.getLogger(__name__).exception('portal settlement pending request_id=%s', state.request_id)
        try:
            mark_request_ended(state)
        except Exception:
            logging.getLogger(__name__).exception('portal request-end evidence pending request_id=%s', state.request_id)


def mark_request_ended(state):
    """Only called by finalization after owned upstream/stream work is joined.

    A request log or stream terminal frame alone is NOT closure evidence. This
    marker proves gateway lifetime closure, not that the supplier charged zero.
    """
    if getattr(state, 'portal_cleanup_unconfirmed', False):
        return
    with store.transaction() as conn:
        row = conn.execute('SELECT owner_id,key_id,worker FROM portal_spends WHERE request_id=?', (state.request_id,)).fetchone()
        if not row or (row['owner_id'], row['key_id'], row['worker']) != (state.portal_owner, state.key_id, budgetowner.identity()):
            raise ValueError('request end owner mismatch')
        conn.execute('INSERT OR IGNORE INTO portal_request_ends VALUES(?,?,?,?,?)',
            (state.request_id, row['owner_id'], row['key_id'], row['worker'], store.now()))


def request_ended(conn, row):
    end = conn.execute('SELECT * FROM portal_request_ends WHERE request_id=?', (row['request_id'],)).fetchone()
    return bool(end and (end['owner_id'], end['key_id'], end['worker']) == (row['owner_id'], row['key_id'], row['worker']))


def recover():
    if not (store.config.DATA_DIR / 'user-portal.sqlite3').exists():
        return
    with store.transaction() as conn:
        quarantine = conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
        if quarantine and quarantine[0] == 'true':
            return
        rows = [dict(row) for row in conn.execute("SELECT request_id,worker FROM portal_spends WHERE state IN ('reserved','forwarded','unknown')")]
    for row in rows:
        db.execute(RECEIPTS)
        committed = db.query_one('SELECT 1 FROM portal_spend_receipts WHERE request_id=?', (row['request_id'],))
        if committed or budgetowner.alive(row['worker']) is False:
            apply(row['request_id'])


def resolve_unknown(request_id, amount, actor, reason, evidence):
    """Explicit operator evidence, never an automatic timeout/zero write-off."""
    charge = store.units(amount)
    _require_evidence(reason, evidence)
    # Serialize with receipt replay in main -> portal lock order. Main is only
    # read/locked; this is NOT a cross-database accounting transaction.
    with db.transaction():
        problem = replay_problem()
        if problem:
            raise ValueError(problem)
        receipt = _committed_receipt(request_id)
        if receipt:
            known = json.loads(receipt['payload'])['amount_units']
            if known is not None and known != charge:
                raise ValueError('金额与已提交的真实费用回执冲突，未改账')
        with store.transaction() as conn:
            store.require_not_quarantined(conn)
            row = conn.execute('SELECT * FROM portal_spends WHERE request_id=?', (request_id,)).fetchone()
            if not row or row['state'] != 'unknown':
                raise ValueError('仅支持核对已终止的费用未知请求，不修改在途或已结算记录')
            if receipt and receipt['owner_id'] != row['owner_id']:
                raise ValueError('主库回执与用户账本归属冲突，未改账')
            if not request_ended(conn, row) and budgetowner.alive(row['worker']) is not False:
                # Lease age or an unreadable owner lock never proves termination.
                raise ValueError('请求尚无可信结束凭证且所属进程未确认退出，不能凭超时解冻')
            _release(conn, row, charge)
            conn.execute('INSERT INTO portal_spend_resolutions VALUES(?,?,?,?,?,?)', (request_id, charge, actor, reason.strip(), evidence.strip(), store.now()))
            conn.execute("UPDATE portal_spends SET state='settled',charged_units=?,settled_at=? WHERE request_id=?", (charge, store.now(), request_id))


def _require_evidence(reason, evidence):
    if not all(isinstance(value, str) and 8 <= len(value.strip()) <= 500 for value in (reason, evidence)):
        raise ValueError('必须填写8至500字的核对原因与凭证说明；不要粘贴密码、Token或完整授权文件')


def _committed_receipt(request_id):
    if not db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portal_spend_receipts'"):
        return None
    return db.query_one('SELECT owner_id,payload FROM portal_spend_receipts WHERE request_id=?', (request_id,))


def replay_problem():
    from . import ledgerreplay
    try:
        directory = ledgerreplay.spool_dir()
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            return '费用回执通道不可核实，先恢复回执通道再人工处理'
        if not directory.exists() and (store.config.DATA_DIR / '.spool-used').exists():
            return '此前使用的费用回执目录已缺失，先恢复回执通道再人工处理'
        if directory.exists() and any(path.suffix == '.json' for path in directory.iterdir()):
            return '仍有费用回执待重放，先完成重放核对，不能手动覆盖'
        dead = directory / 'dead'
        if dead.is_symlink() or (dead.exists() and not dead.is_dir()):
            return '费用死信目录不可核实，先核对回执'
        if dead.exists() and any(path.suffix == '.json' for path in dead.iterdir()):
            return '存在未处置的费用死信，先核对回执'
        if ledgerreplay.dead_letters() != 0:
            return '存在未处置或不可读取的费用死信，先核对回执'
    except OSError:
        return '费用回执目录不可读取，先恢复回执通道再人工处理'
    return ''


def pending_page(page=1):
    page = max(1, min(10000, page))
    with store.transaction() as conn:
        clause = "FROM portal_spends s JOIN portal_users u ON u.id=s.owner_id WHERE s.state IN ('reserved','forwarded','unknown')"
        total = conn.execute('SELECT COUNT(*) ' + clause).fetchone()[0]
        rows = [dict(row) for row in conn.execute('SELECT s.request_id,s.owner_id,u.username,s.key_id,s.worker,s.state,s.created_at,s.held_units '
            + clause + ' ORDER BY s.created_at,s.request_id LIMIT 30 OFFSET ?', ((page - 1) * 30,))]
        for row in rows:
            row['end_confirmed'] = request_ended(conn, row)
    replay_block = replay_problem()
    for row in rows:
        live = budgetowner.alive(row.pop('worker'))
        row['worker_status'] = 'alive' if live is True else 'dead' if live is False else 'unproven'
        row['can_resolve'] = row['state'] == 'unknown' and (row['end_confirmed'] or live is False)
        row['blocked_reason'] = ('请求仍在预留或发送阶段，不能手动结算' if row['state'] != 'unknown' else
            '' if row['end_confirmed'] else
            '所属进程仍存活，先停用调用并确认进程已退出' if live is True else
            '无法证明所属进程已退出，不能凭超时解除冻结' if live is not False else '')
        receipt = _committed_receipt(row['request_id'])
        row['receipt_units'] = json.loads(receipt['payload'])['amount_units'] if receipt else None
        if receipt and receipt['owner_id'] != row['owner_id']:
            row['can_resolve'] = False
            row['blocked_reason'] = '主库回执归属冲突，请离线核对'
        if replay_block:
            row['can_resolve'] = False
            row['blocked_reason'] = replay_block
        log = db.query_one('SELECT model,status,terminal_state,credit FROM request_logs WHERE request_id=?', (row['request_id'],))
        row['request'] = dict(log) if log else None
    return {'items': rows, 'total': total, 'page': page, 'page_size': 30}


def debts_page(page=1):
    page = max(1, min(10000, page))
    with store.transaction() as conn:
        clause = 'FROM portal_debts d JOIN portal_users u ON u.id=d.owner_id WHERE d.amount_units>0'
        total = conn.execute('SELECT COUNT(*) ' + clause).fetchone()[0]
        rows = [dict(row) for row in conn.execute('SELECT d.owner_id,u.username,u.enabled,d.amount_units ' + clause
            + ' ORDER BY d.owner_id LIMIT 30 OFFSET ?', ((page - 1) * 30,))]
        for row in rows:
            row['repayable_units'] = sum(g['remaining_units'] for g in store.spendable_grants(conn, row['owner_id'], store.now()))
    return {'items': rows, 'total': total, 'page': page, 'page_size': 30}


def adjust_debt(owner_id, action_id, kind, amount, expected_units, actor, reason, evidence):
    """Repay from this owner's unheld grants or explicitly waive an obligation.

    Never mint balance, alter a supplier charge, or touch another owner's grants.
    The action ID and expected debt protect retries and stale operator forms.
    """
    if not isinstance(action_id, str) or not re.fullmatch(r'[a-f0-9]{32}', action_id):
        raise ValueError('处置标识无效，请刷新后重新操作')
    if kind not in ('repay', 'waive'):
        raise ValueError('仅支持使用现有积分抵扣或管理员豁免欠额')
    charge = store.units(amount)
    if charge <= 0 or type(expected_units) is not int or expected_units <= 0:
        raise ValueError('处置金额与预期欠额必须为正数')
    _require_evidence(reason, evidence)
    reason, evidence = reason.strip(), evidence.strip()
    with store.transaction() as conn:
        store.require_not_quarantined(conn)
        existing = conn.execute('SELECT * FROM portal_debt_actions WHERE id=?', (action_id,)).fetchone()
        if existing:
            if (existing['owner_id'], existing['kind'], existing['amount_units'], existing['before_units'], existing['actor'], existing['reason'], existing['evidence']) != (
                    owner_id, kind, charge, expected_units, actor, reason, evidence):
                raise ValueError('同一处置标识已用于其他内容，未再次改账')
            return {'already': True, 'before_units': existing['before_units'], 'after_units': existing['after_units']}
        debt = conn.execute('SELECT amount_units FROM portal_debts WHERE owner_id=?', (owner_id,)).fetchone()
        if not debt or debt[0] != expected_units:
            raise ValueError('欠额已变化或已处理，请刷新并重新核对')
        if charge > debt[0]:
            raise ValueError('处置金额不能超过当前欠额')
        store.expire(conn, owner_id, store.now())
        allocations = []
        if kind == 'repay':
            grants = store.spendable_grants(conn, owner_id, store.now())
            if sum(row['remaining_units'] for row in grants) < charge:
                raise ValueError('已入池、未冻结且未过期的积分不足；不会动用其他用户或冻结批次')
            left = charge
            for grant in grants:
                used = min(left, grant['remaining_units'])
                if used:
                    table = 'portal_bonus_grants' if grant['kind'] == 'bonus' else 'portal_grants'
                    conn.execute(f'UPDATE {table} SET remaining_units=remaining_units-? WHERE id=?', (used, grant['id']))
                    allocations.append((grant['kind'], grant['id'], used))
                    left -= used
                if not left:
                    break
        after = debt[0] - charge
        conn.execute('UPDATE portal_debts SET amount_units=? WHERE owner_id=?', (after, owner_id))
        conn.execute('INSERT INTO portal_debt_actions VALUES(?,?,?,?,?,?,?,?,?,?)',
            (action_id, owner_id, kind, charge, debt[0], after, actor, reason, evidence, store.now()))
        for grant_kind, grant_id, used in allocations:
            table = 'portal_debt_bonus_allocations' if grant_kind == 'bonus' else 'portal_debt_allocations'
            conn.execute(f'INSERT INTO {table} VALUES(?,?,?)', (action_id, grant_id, used))
        conn.execute('INSERT INTO portal_events(owner_id,kind,amount_units,ts,source_ref) VALUES(?,?,?,?,?)',
            (owner_id, 'debt_repaid' if kind == 'repay' else 'debt_waived', -charge if kind == 'repay' else 0, store.now(), 'debt:' + action_id))
        return {'already': False, 'before_units': debt[0], 'after_units': after}


def history_page(page=1):
    page = max(1, min(10000, page))
    union = """SELECT r.request_id AS id,s.owner_id,u.username,'resolve' AS kind,
        r.amount_units,NULL AS before_units,NULL AS after_units,r.actor,r.reason,r.evidence,r.created_at,NULL AS expires_at
        FROM portal_spend_resolutions r JOIN portal_spends s ON s.request_id=r.request_id JOIN portal_users u ON u.id=s.owner_id
        UNION ALL SELECT a.id,a.owner_id,u.username,a.kind,a.amount_units,a.before_units,a.after_units,a.actor,a.reason,a.evidence,a.created_at,NULL AS expires_at
        FROM portal_debt_actions a JOIN portal_users u ON u.id=a.owner_id
        UNION ALL SELECT b.id,b.owner_id,u.username,'bonus',b.amount_units,NULL,NULL,b.actor,b.reason,'',b.received_at,b.expires_at
        FROM portal_bonus_grants b JOIN portal_users u ON u.id=b.owner_id"""
    with store.transaction() as conn:
        total = conn.execute('SELECT COUNT(*) FROM (' + union + ')').fetchone()[0]
        rows = [dict(row) for row in conn.execute('SELECT * FROM (' + union + ') ORDER BY created_at DESC,id DESC,kind DESC LIMIT 30 OFFSET ?', ((page - 1) * 30,))]
    return {'items': rows, 'total': total, 'page': page, 'page_size': 30}


def grant_bonus(owner_id, action_id, amount, valid_days, actor, reason):
    if actor != 'admin':
        raise ValueError('仅超级管理员admin可赠送积分')
    if not isinstance(action_id, str) or not re.fullmatch('[a-f0-9]{32}', action_id):
        raise ValueError('赠送标识无效，请刷新后重试')
    value = store.units(amount)
    if not 0 < value <= 1_000_000 * store.SCALE:
        raise ValueError('单次赠送需大于0且不超过100万积分，最多6位小数')
    if type(valid_days) is not int or not 1 <= valid_days <= 365:
        raise ValueError('赠送有效期需为1至365天')
    if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 500:
        raise ValueError('请填写8至500字的赠送原因，不要填写密码或Token')
    reason = reason.strip()
    with store.transaction() as conn:
        prior = conn.execute('SELECT * FROM portal_bonus_grants WHERE id=?', (action_id,)).fetchone()
        if prior:
            if (prior['owner_id'], prior['amount_units'], prior['valid_days'], prior['actor'], prior['reason']) != (owner_id, value, valid_days, actor, reason):
                raise ValueError('同一赠送标识已用于其他内容，未重复入账')
            return {'already': True, 'amount_units': value, 'expires_at': prior['expires_at']}
        store.require_not_quarantined(conn)
        owner = conn.execute('SELECT enabled FROM portal_users WHERE id=?', (owner_id,)).fetchone()
        if not owner or not owner['enabled']:
            raise ValueError('用户不存在或已停用，未赠送积分')
        at = store.now()
        expiry = at + valid_days * 86400
        conn.execute('INSERT INTO portal_bonus_grants VALUES(?,?,?,?,?,?,?,?,?)', (action_id, owner_id, value, value, at, expiry, actor, reason, valid_days))
        conn.execute('INSERT INTO portal_events(owner_id,kind,amount_units,ts,expires_at,source_ref) VALUES(?,?,?,?,?,?)',
            (owner_id, 'admin_bonus', value, at, expiry, 'admin-bonus:' + action_id))
        return {'already': False, 'amount_units': value, 'expires_at': expiry}
