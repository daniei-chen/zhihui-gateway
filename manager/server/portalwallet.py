"""User-wide conservative reservations and main-ledger settlement outbox.

No local supplier price table exists. One user request holds the entire currently
available wallet, across all keys. New approved requests cap user charges at this
hold; supplier overruns are platform costs, never user debt or future grants.
Unknown supplier usage is never guessed to be zero. A request that has a durable
end proof and a client-cancelled terminal state may be closed under the explicit
client-cancel policy, with a zero user charge and the supplier cost unresolved.
Separate SQLite databases do NOT form one transaction; committed main-ledger
receipts are replayed idempotently into the wallet.
"""
from __future__ import annotations

import json
import logging
import os
import re

from . import budgetowner, db, metering, observability, portalbilling, portalpolicy as policy, portalstore as store

RECEIPTS = '''CREATE TABLE IF NOT EXISTS portal_spend_receipts(
 request_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, payload TEXT NOT NULL
)'''

AUTO_CLOSE_ACTOR = 'system:client-cancel-v1'
AUTO_CLOSE_REASON = '客户端取消后自动释放冻结；供应商实际费用未知，平台承担差额'
AUTO_CLOSE_EVIDENCE = (
    '请求生命周期已结束，且客户端终止状态为 client_cancelled；'
    '没有可用的完整上游用量回执；不把供应商费用伪记为 0，仅释放用户侧的未决冻结'
)


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
            portalbilling.require_approved_policy(conn)
            conn.execute("INSERT INTO portal_spends VALUES(?,?,?,?,? ,?,?,NULL,NULL)",
                (state.request_id, owner, key['id'], budgetowner.identity(), 'reserved', store.now(), held))
            conn.execute('INSERT INTO portal_spend_policies VALUES(?,?)', (state.request_id, portalbilling.POLICY))
            for row in rows:
                table, allocations = ('portal_bonus_grants', 'portal_bonus_allocations') if row['kind'] == 'bonus' else ('portal_grants', 'portal_allocations')
                conn.execute(f'INSERT INTO {allocations} VALUES(?,?,?)', (state.request_id, row['id'], row['remaining_units']))
                conn.execute(f'UPDATE {table} SET remaining_units=0 WHERE id=?', (row['id'],))
        state.portal_owner = owner
        state.portal_credential_hash = key.get('credential_hash', '')
    except (ValueError, OSError) as exc:
        reject('用户积分或授权不可用，未发送请求')


def forwarded(state):
    from .budget import BudgetRejected
    if not getattr(state, 'portal_owner', None):
        return
    try:
        with store.transaction() as conn:
            portalbilling.require_approved_policy(conn)
            # Recheck owner generation and revocation at the durable send boundary.
            owner = policy.identity({'id': state.key_id, 'credential_hash': getattr(state, 'portal_credential_hash', '')}, conn)
            if owner != state.portal_owner:
                reject('用户积分归属发生变化')
            if not policy.enabled():
                reject('用户公共池调用已关闭')
            approved = conn.execute('SELECT policy FROM portal_spend_policies WHERE request_id=?', (state.request_id,)).fetchone()
            if not approved or approved[0] != portalbilling.POLICY:
                reject('请求计费规则未核实，未发送请求')
            changed = conn.execute("UPDATE portal_spends SET state='forwarded' WHERE request_id=? AND owner_id=? AND worker=? AND state='reserved'",
                (state.request_id, state.portal_owner, budgetowner.identity())).rowcount
            if changed != 1:
                reject('用户积分预留归属已变更')
    except BudgetRejected:
        raise
    except Exception:
        reject('用户积分发送许可不可用，未发送请求')


def supplier_units(credit):
    """Supplier amounts share the main ledger's bounded half-even quantization.

    User grants and operator-entered amounts retain their strict six-digit input
    validation; only measured supplier credit uses this conversion.
    """
    from . import ledger, metering
    parsed, malformed = metering.parse_credit(credit)
    if malformed or parsed is None:
        return None
    # Validate the supplier domain, then use the same decimal input as the
    # ledger. Legacy replay strings must not get an extra float rounding step.
    return ledger.units(credit)


def receipt(state, usage, credit):
    """Called ONLY inside the same main DB transaction as request_logs."""
    owner = getattr(state, 'portal_owner', None)
    if not owner:
        return
    amount = None
    supplier = None
    from . import metering
    try:
        if credit is not None:supplier = supplier_units(credit)
    except ValueError:pass
    if state.terminal_state == 'completed' and metering.parse_usage(usage).complete:amount = supplier
    db.execute('INSERT OR IGNORE INTO portal_spend_receipts VALUES(?,?,?)',
        (state.request_id, owner, json.dumps({'amount_units': amount, 'supplier_units': supplier})))


def replay_receipt(entry, facts):
    owner = (entry.get('budget') or {}).get('portal_owner')
    if not isinstance(owner, str) or not owner:
        return
    amount = None
    supplier = None
    try:
        if facts.get('credit') is not None:supplier = supplier_units(facts['credit'])
    except ValueError:pass
    if facts.get('terminal_state') == 'completed' and facts.get('usage_complete') == 1:amount = supplier
    # A delayed/spooled fact must not silently disagree with an operator's
    # immutable resolution. Caller rolls back replay and retains the evidence.
    with store.transaction() as conn:
        resolution = conn.execute('SELECT r.amount_units,s.owner_id FROM portal_spend_resolutions r '
            'JOIN portal_spends s ON s.request_id=r.request_id WHERE r.request_id=?', (facts['request_id'],)).fetchone()
        if resolution and (resolution['owner_id'] != owner or (amount is not None and resolution['amount_units'] != amount)):
            raise ValueError('迟到费用回执与人工核对结果冲突，保留回执等待复核')
        settled = conn.execute('SELECT supplier_units FROM portal_settlements WHERE request_id=?',(facts['request_id'],)).fetchone()
        unknown = conn.execute('SELECT 1 FROM portal_unknown_supplier_costs WHERE request_id=?',(facts['request_id'],)).fetchone()
        if supplier is not None and ((settled and settled[0] != supplier) or unknown):
            raise ValueError('迟到供应商费用与已记录的成本状态不同，保留证据等待复核')
    db.execute('INSERT OR IGNORE INTO portal_spend_receipts VALUES(?,?,?)',
        (facts['request_id'], owner, json.dumps({'amount_units': amount, 'supplier_units': supplier})))


_SUPPLIER_FROM_CHARGE = object()


def _release(conn, row, charge, *, supplier=_SUPPLIER_FROM_CHARGE):
    if type(charge) is not int or not 0 <= charge <= 9_000_000_000_000_000_000:
        raise ValueError('settlement amount invalid')
    policy = conn.execute('SELECT policy FROM portal_spend_policies WHERE request_id=?', (row['request_id'],)).fetchone()
    capped = bool(policy and policy[0] == portalbilling.POLICY)
    if policy and not capped:
        raise ValueError('unrecognized persisted billing policy')
    if supplier is _SUPPLIER_FROM_CHARGE: supplier = charge
    if supplier is None:
        if charge != 0: raise ValueError('unknown supplier cost cannot establish a nonzero user settlement')
    elif type(supplier) is not int or not charge <= supplier <= 9_000_000_000_000_000_000:
        raise ValueError('supplier settlement amount invalid')
    if capped:
        charge = min(charge, row['held_units'])
    left = charge
    allocations = conn.execute("SELECT g.id,g.account_id,g.expires_at,a.amount_units AS held,'account' AS kind "
        'FROM portal_allocations a JOIN portal_grants g ON g.id=a.grant_id WHERE a.request_id=? '
        "UNION ALL SELECT g.id,NULL AS account_id,g.expires_at,a.amount_units AS held,'bonus' AS kind "
        'FROM portal_bonus_allocations a JOIN portal_bonus_grants g ON g.id=a.grant_id WHERE a.request_id=? '
        'ORDER BY expires_at,id,kind', (row['request_id'], row['request_id'])).fetchall()
    if capped and (sum(grant['held'] for grant in allocations) != row['held_units']
                   or any(type(grant['held']) is not int or grant['held'] < 0 for grant in allocations)):
        raise ValueError('wallet allocations do not match frozen balance')
    if capped and conn.execute("SELECT 1 FROM portal_allocations a JOIN portal_grants g ON g.id=a.grant_id WHERE a.request_id=? AND g.owner_id!=? "
            "UNION ALL SELECT 1 FROM portal_bonus_allocations a JOIN portal_bonus_grants g ON g.id=a.grant_id WHERE a.request_id=? AND g.owner_id!=? LIMIT 1",
            (row['request_id'], row['owner_id'], row['request_id'], row['owner_id'])).fetchone():
        raise ValueError('wallet allocation owner mismatch')
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
    if capped:
        if supplier is None:
            conn.execute('INSERT INTO portal_unknown_supplier_costs VALUES(?,?,?,?,?,?)',
                (row['request_id'],row['owner_id'],row['key_id'],charge,row['held_units'],store.now()))
        else:
            conn.execute('INSERT INTO portal_settlements VALUES(?,?,?,?,?,?,?,?,?)',
                (row['request_id'], row['owner_id'], row['key_id'], portalbilling.POLICY, row['held_units'],
                 supplier, charge, supplier - charge, store.now()))
    return charge


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
                charged = _release(conn, row, amount)
                conn.execute("UPDATE portal_spends SET state='settled',charged_units=?,settled_at=? WHERE request_id=?", (charged, store.now(), request_id))
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
    auto_close_client_cancelled()
    auto_close_rejected()


def _client_cancel_auto_close_enabled():
    raw = os.environ.get('WB_PORTAL_AUTO_CLOSE_CLIENT_CANCEL', '1').strip().lower()
    return raw not in ('0', 'false', 'off', 'no')


def _client_cancel_auto_close_grace():
    raw = os.environ.get('WB_PORTAL_CLIENT_CANCEL_GRACE_SEC', '60')
    try:
        return min(3600, max(0, int(raw)))
    except (TypeError, ValueError):
        return 60


def _rejected_auto_close_enabled():
    raw = os.environ.get('WB_PORTAL_AUTO_CLOSE_REJECTED', '1').strip().lower()
    return raw not in ('0', 'false', 'off', 'no')


def auto_close_rejected(limit=100):
    """Release wallet holds for requests the gateway itself rejected.

    A rejected request has a terminal gateway decision and no supplier usage;
    unlike an upstream timeout it cannot have reached the provider. Only
    portal spends with a durable request end and terminal_state=rejected are
    eligible; everything else stays in the manual queue.
    """
    if not _rejected_auto_close_enabled() or not (store.config.DATA_DIR / 'user-portal.sqlite3').exists():
        return 0
    limit = max(0, min(500, int(limit)))
    if not limit:
        return 0
    with store.transaction() as conn:
        quarantine = conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
        if quarantine and quarantine[0] == 'true':
            return 0
        rows = [dict(row) for row in conn.execute(
            "SELECT s.request_id FROM portal_spends s JOIN portal_request_ends e ON e.request_id=s.request_id "
            "WHERE s.state='unknown' AND e.owner_id=s.owner_id AND e.key_id=s.key_id AND e.worker=s.worker "
            "ORDER BY e.ended_at,s.created_at,s.request_id LIMIT ?", (limit,))]
    if not rows or replay_problem():
        return 0
    closed = 0
    logger = logging.getLogger(__name__)
    for row in rows:
        log = db.query_one(
            'SELECT terminal_state FROM request_logs WHERE request_id=? ORDER BY ts DESC LIMIT 1',
            (row['request_id'],))
        if not log or log['terminal_state'] != 'rejected':
            continue
        try:
            resolve_unknown(row['request_id'], 0, AUTO_CLOSE_ACTOR,
                            '网关拒绝请求，未到达供应商；自动释放用户余额',
                            'request_logs.terminal_state=rejected')
            closed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning('rejected auto-close deferred request_id=%s error=%s',
                           row['request_id'], type(exc).__name__)
    return closed


def auto_close_client_cancelled(limit=100):
    """Close only proven client-cancelled unknowns, never guess supplier cost.

    The source of truth for closure is a matching ``portal_request_ends`` row,
    not an elapsed timeout. The final request log must also prove a client
    cancellation. A pending ledger replay or disagreement with a committed
    receipt defers the item to the existing manual reconciliation path.
    """
    if not _client_cancel_auto_close_enabled() or not (store.config.DATA_DIR / 'user-portal.sqlite3').exists():
        return 0
    limit = max(0, min(500, int(limit)))
    if not limit:
        return 0
    cutoff = store.now() - _client_cancel_auto_close_grace()
    with store.transaction() as conn:
        quarantine = conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
        if quarantine and quarantine[0] == 'true':
            return 0
        # Persist a keyset cursor, not OFFSET: unresolved non-cancellations and
        # deferred/conflicting rows must not starve the remainder of the queue.
        saved = conn.execute("SELECT value FROM portal_settings WHERE key='client_cancel_scan_cursor_v1'").fetchone()
        cursor = None
        if saved:
            try:
                value = json.loads(saved[0])
                if (isinstance(value, list) and len(value) == 3
                        and type(value[0]) is int and type(value[1]) is int
                        and isinstance(value[2], str) and 0 < len(value[2]) <= 128):
                    cursor = value
            except (ValueError, TypeError):
                pass
        select = (
            "SELECT s.request_id,e.ended_at,s.created_at FROM portal_spends s "
            "JOIN portal_request_ends e ON e.request_id=s.request_id "
            "WHERE s.state='unknown' AND e.owner_id=s.owner_id "
            "AND e.key_id=s.key_id AND e.worker=s.worker AND e.ended_at<=? ")
        # Freeze an upper key for each sweep. Otherwise a continuously growing
        # tail can prevent wraparound forever and starve late/backdated ends.
        # Do not restart on every old unresolved row: that would starve the tail.
        saved_upper=conn.execute("SELECT value FROM portal_settings WHERE key='client_cancel_scan_upper_v1'").fetchone()
        upper=None
        if saved_upper:
            try:
                value=json.loads(saved_upper[0])
                if (isinstance(value,list) and len(value)==3 and type(value[0]) is int
                        and type(value[1]) is int and isinstance(value[2],str)
                        and 0<len(value[2])<=128):upper=value
            except (TypeError,ValueError):pass
        if upper is None:
            row=conn.execute(select+'ORDER BY e.ended_at DESC,s.created_at DESC,s.request_id DESC LIMIT 1',
                             (cutoff,)).fetchone()
            upper=[row['ended_at'],row['created_at'],row['request_id']] if row else None
        bounded='AND (e.ended_at,s.created_at,s.request_id) <= (?,?,?) ' if upper else ''
        bounds=tuple(upper or ())
        order = "ORDER BY e.ended_at,s.created_at,s.request_id LIMIT ?"
        after = "AND (e.ended_at,s.created_at,s.request_id) > (?,?,?) "
        rows = [dict(row) for row in conn.execute(
            select + bounded + (after if cursor else '') + order,
            (cutoff, *bounds, *cursor, limit) if cursor else (cutoff, *bounds, limit))]
        if not rows and cursor:
            # Finish this bounded sweep; the next call starts a fresh one and
            # includes ends inserted behind the old cursor.
            upper=None
        next_cursor = ([rows[-1]['ended_at'], rows[-1]['created_at'], rows[-1]['request_id']]
                       if len(rows) == limit else None)
        if next_cursor==upper:next_cursor=None
        if next_cursor is None:upper=None
        conn.execute("INSERT INTO portal_settings(key,value) VALUES('client_cancel_scan_cursor_v1',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(next_cursor),))
        conn.execute("INSERT INTO portal_settings(key,value) VALUES('client_cancel_scan_upper_v1',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(upper),))
    if not rows or replay_problem():
        return 0
    closed = 0
    logger = logging.getLogger(__name__)
    for row in rows:
        log = db.query_one(
            'SELECT terminal_state,status FROM request_logs WHERE request_id=? ORDER BY ts DESC LIMIT 1',
            (row['request_id'],))
        if not log or (log['terminal_state'] != 'client_cancelled' and log['status'] != 499):
            continue
        try:
            resolve_unknown(row['request_id'], 0, AUTO_CLOSE_ACTOR, AUTO_CLOSE_REASON, AUTO_CLOSE_EVIDENCE)
            closed += 1
        except Exception as exc:  # bounded per-row failure; recovery keeps running
            logger.warning('client-cancel auto-close deferred request_id=%s error=%s',
                           row['request_id'], type(exc).__name__)
    return closed


def resolve_unknown(request_id, amount, actor, reason, evidence):
    """Record operator-confirmed supplier cost with its evidence.

    ``amount`` is the verified supplier amount, not an arbitrary user debit or
    waiver. The user's actual debit is separately capped by ``_release``.
    Automatic cancellation closeout confirms no supplier amount at all.
    """
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
            supplier = charge if charge > 0 else None
            if receipt:
                payload=json.loads(receipt['payload'])
                supplier=payload.get('supplier_units',payload.get('amount_units'))
                # A human can confirm a positive cost with the recorded evidence;
                # waiving the user's charge does not confirm zero supplier cost.
                if supplier is None and actor != AUTO_CLOSE_ACTOR and charge>0:supplier=charge
            elif actor == AUTO_CLOSE_ACTOR:
                supplier=None
            charged = _release(conn, row, charge, supplier=supplier)
            conn.execute('INSERT INTO portal_spend_resolutions VALUES(?,?,?,?,?,?)', (request_id, charge, actor, reason.strip(), evidence.strip(), store.now()))
            conn.execute("UPDATE portal_spends SET state='settled',charged_units=?,settled_at=? WHERE request_id=?", (charged, store.now(), request_id))


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
        from . import spoolidentity
        if not spoolidentity.check(directory)['ok']:
            return '费用回执通道身份不可核实，先恢复原目录和回执再处理'
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
        FROM portal_bonus_grants b JOIN portal_users u ON u.id=b.owner_id
        UNION ALL SELECT s.request_id,s.owner_id,u.username,'platform_overrun',s.platform_units,NULL,NULL,
            'system','单次费用超出已冻结余额，差额由平台承担，不形成用户欠款',s.request_id,s.created_at,NULL
        FROM portal_settlements s JOIN portal_users u ON u.id=s.owner_id WHERE s.platform_units>0"""
    with store.transaction() as conn:
        total = conn.execute('SELECT COUNT(*) FROM (' + union + ')').fetchone()[0]
        rows = [dict(row) for row in conn.execute('SELECT * FROM (' + union + ') ORDER BY created_at DESC,id DESC,kind DESC LIMIT 30 OFFSET ?', ((page - 1) * 30,))]
    return {'items': rows, 'total': total, 'page': page, 'page_size': 30}


def platform_costs_page(page=1):
    page = max(1, min(10000, page))
    with store.transaction() as conn:
        total = conn.execute('SELECT COUNT(*) FROM portal_settlements WHERE platform_units>0').fetchone()[0]
        rows = [dict(row) for row in conn.execute('SELECT s.*,u.username FROM portal_settlements s '
            'JOIN portal_users u ON u.id=s.owner_id WHERE s.platform_units>0 '
            'ORDER BY s.created_at DESC,s.request_id DESC LIMIT 30 OFFSET ?', ((page - 1) * 30,))]
        units = conn.execute('SELECT COALESCE(SUM(platform_units),0) FROM portal_settlements').fetchone()[0]
        unknown = conn.execute('SELECT COUNT(*) FROM portal_unknown_supplier_costs').fetchone()[0]
        unknown_items=[dict(row) for row in conn.execute('SELECT s.*,u.username FROM portal_unknown_supplier_costs s '
            'JOIN portal_users u ON u.id=s.owner_id ORDER BY s.created_at DESC,s.request_id DESC LIMIT 30 OFFSET ?',((page-1)*30,))]
    return {'items': rows, 'total': total, 'page': page, 'page_size': 30, 'platform_units': units,
            'unknown_supplier_costs':unknown,'unknown_items':unknown_items,'cost_complete':unknown==0,
            'cost_scope':'recorded supplier evidence; historical cancellations need separate reconciliation'}


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
