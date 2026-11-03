"""Isolated user-portal state. No migration or rewrite of the gateway ledger."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from . import config

SCALE = 1_000_000
TZ = timezone(timedelta(hours=8))
SCHEMA = '''
CREATE TABLE IF NOT EXISTS portal_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_users(
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, pwd_hash TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, sv INTEGER NOT NULL DEFAULT 1,
 created_at INTEGER NOT NULL, failed_logins INTEGER NOT NULL DEFAULT 0,
 blocked_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portal_accounts(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES portal_users(id),
 platform TEXT NOT NULL, realm TEXT NOT NULL, provider_uid TEXT NOT NULL,
 label TEXT NOT NULL, created_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', initial_state TEXT NOT NULL DEFAULT 'pending',
 initial_at INTEGER, UNIQUE(platform,realm,provider_uid)
);
CREATE INDEX IF NOT EXISTS portal_accounts_owner ON portal_accounts(owner_id,created_at);
CREATE TABLE IF NOT EXISTS portal_grants(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES portal_users(id),
 account_id TEXT NOT NULL REFERENCES portal_accounts(id), source TEXT NOT NULL,
 source_ref TEXT NOT NULL, amount_units INTEGER NOT NULL CHECK(amount_units>0),
 remaining_units INTEGER NOT NULL CHECK(remaining_units>=0 AND remaining_units<=amount_units),
 received_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 UNIQUE(account_id,source,source_ref,expires_at)
);
CREATE INDEX IF NOT EXISTS portal_grants_expiry ON portal_grants(owner_id,expires_at);
CREATE TABLE IF NOT EXISTS portal_checkins(
 account_id TEXT NOT NULL REFERENCES portal_accounts(id), day TEXT NOT NULL,
 state TEXT NOT NULL, started_at INTEGER NOT NULL, confirmed_at INTEGER,
 actor TEXT NOT NULL, baseline TEXT NOT NULL DEFAULT '{}',
 credited_units INTEGER NOT NULL DEFAULT 0, code INTEGER,
 PRIMARY KEY(account_id,day)
);
CREATE TABLE IF NOT EXISTS portal_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, account_id TEXT,
 kind TEXT NOT NULL, amount_units INTEGER NOT NULL, ts INTEGER NOT NULL,
 expires_at INTEGER, source_ref TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS portal_auth_sessions(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_sv INTEGER NOT NULL,
 upstream_state TEXT NOT NULL, expires_at INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'waiting', account_id TEXT, last_poll INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portal_key_links(
 key_id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL, key_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_keys(
 key_id INTEGER PRIMARY KEY, owner_sv INTEGER NOT NULL, label TEXT NOT NULL,
 created_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portal_pool_links(
 account_id TEXT PRIMARY KEY REFERENCES portal_accounts(id), filename TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_pool_intents(
 account_id TEXT PRIMARY KEY REFERENCES portal_accounts(id), filename TEXT NOT NULL UNIQUE,
 marker TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_spends(
 request_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, key_id INTEGER NOT NULL,
 worker TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL,
 held_units INTEGER NOT NULL, charged_units INTEGER, settled_at INTEGER
);
CREATE INDEX IF NOT EXISTS portal_spends_owner ON portal_spends(owner_id,state);
CREATE TABLE IF NOT EXISTS portal_spend_policies(
 request_id TEXT PRIMARY KEY REFERENCES portal_spends(request_id), policy TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS portal_spends_key_state ON portal_spends(owner_id,key_id,state);
CREATE TABLE IF NOT EXISTS portal_settlements(
 request_id TEXT PRIMARY KEY REFERENCES portal_spends(request_id), owner_id TEXT NOT NULL,
 key_id INTEGER NOT NULL, policy TEXT NOT NULL, held_units INTEGER NOT NULL CHECK(held_units>=0),
 supplier_units INTEGER NOT NULL CHECK(supplier_units>=0),
 charged_units INTEGER NOT NULL CHECK(charged_units>=0 AND charged_units<=held_units),
 platform_units INTEGER NOT NULL CHECK(platform_units>=0), created_at INTEGER NOT NULL,
 CHECK(supplier_units=charged_units+platform_units)
);
CREATE TRIGGER IF NOT EXISTS portal_settlements_no_update BEFORE UPDATE ON portal_settlements
 BEGIN SELECT RAISE(ABORT,'settlements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portal_settlements_no_delete BEFORE DELETE ON portal_settlements
 BEGIN SELECT RAISE(ABORT,'settlements are immutable'); END;
CREATE TABLE IF NOT EXISTS portal_allocations(
 request_id TEXT NOT NULL REFERENCES portal_spends(request_id),
 grant_id TEXT NOT NULL REFERENCES portal_grants(id), amount_units INTEGER NOT NULL,
 PRIMARY KEY(request_id,grant_id)
);
CREATE TABLE IF NOT EXISTS portal_debts(owner_id TEXT PRIMARY KEY, amount_units INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS portal_spend_resolutions(
 request_id TEXT PRIMARY KEY, amount_units INTEGER NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL, evidence TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_request_ends(
 request_id TEXT PRIMARY KEY REFERENCES portal_spends(request_id), owner_id TEXT NOT NULL,
 key_id INTEGER NOT NULL, worker TEXT NOT NULL, ended_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_debt_actions(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES portal_users(id),
 kind TEXT NOT NULL CHECK(kind IN ('repay','waive')),
 amount_units INTEGER NOT NULL CHECK(amount_units>0),
 before_units INTEGER NOT NULL, after_units INTEGER NOT NULL CHECK(after_units>=0),
 actor TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS portal_debt_actions_owner ON portal_debt_actions(owner_id,created_at);
CREATE TABLE IF NOT EXISTS portal_debt_allocations(
 action_id TEXT NOT NULL REFERENCES portal_debt_actions(id),
 grant_id TEXT NOT NULL REFERENCES portal_grants(id), amount_units INTEGER NOT NULL CHECK(amount_units>0),
 PRIMARY KEY(action_id,grant_id)
);
CREATE TABLE IF NOT EXISTS portal_bonus_grants(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES portal_users(id),
 amount_units INTEGER NOT NULL CHECK(amount_units>0),
 remaining_units INTEGER NOT NULL CHECK(remaining_units>=0 AND remaining_units<=amount_units),
 received_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL, valid_days INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS portal_bonus_expiry ON portal_bonus_grants(owner_id,expires_at);
CREATE TABLE IF NOT EXISTS portal_bonus_allocations(
 request_id TEXT NOT NULL REFERENCES portal_spends(request_id),
 grant_id TEXT NOT NULL REFERENCES portal_bonus_grants(id), amount_units INTEGER NOT NULL CHECK(amount_units>0),
 PRIMARY KEY(request_id,grant_id)
);
CREATE TABLE IF NOT EXISTS portal_debt_bonus_allocations(
 action_id TEXT NOT NULL REFERENCES portal_debt_actions(id),
 grant_id TEXT NOT NULL REFERENCES portal_bonus_grants(id), amount_units INTEGER NOT NULL CHECK(amount_units>0),
 PRIMARY KEY(action_id,grant_id)
);
'''


def now() -> int:
    return int(time.time())


def day(at: int | None = None) -> str:
    return datetime.fromtimestamp(now() if at is None else at, TZ).strftime('%Y-%m-%d')


@contextmanager
def transaction():
    path = config.DATA_DIR / 'user-portal.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('用户账本路径异常')
    # Exclusive creation sets permissions before any credential hash is written.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=FULL')
        conn.executescript(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def secret() -> str:
    with transaction() as conn:
        conn.execute('INSERT OR IGNORE INTO portal_settings VALUES(?,?)', ('session_secret', secrets.token_hex(48)))
        return conn.execute('SELECT value FROM portal_settings WHERE key=?', ('session_secret',)).fetchone()[0]


def units(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError('积分数值无效')
    try:
        number = Decimal(str(value))
        scaled = number * SCALE
        if not number.is_finite() or scaled < 0 or scaled > 10**15 or scaled != scaled.to_integral_value():
            raise ValueError('积分数值超出范围或精度')
        return int(scaled)
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError('积分数值无效') from exc


def snapshot(ok, total, expiries, *, at=None) -> dict[int, int]:
    """Only complete, finite, expiry-backed balances may become user grants."""
    at = now() if at is None else at
    if ok is not True or not isinstance(expiries, list):
        raise ValueError('上游余额尚未核实')
    expected = units(total)
    buckets = {}
    for item in expiries:
        if not isinstance(item, dict):
            raise ValueError('积分到期明细不完整')
        expiry = item.get('at')
        if isinstance(expiry, bool) or not isinstance(expiry, int) or not at < expiry <= at + 200 * 366 * 86400:
            raise ValueError('积分到期时间不完整')
        amount = units(item.get('amount'))
        buckets[expiry] = buckets.get(expiry, 0) + amount
    if sum(buckets.values()) != expected:
        raise ValueError('余额与到期明细不一致，暂不入账')
    return {expiry: amount for expiry, amount in buckets.items() if amount}


def user(user_id: str) -> dict | None:
    with transaction() as conn:
        row = conn.execute('SELECT * FROM portal_users WHERE id=?', (user_id,)).fetchone()
        return dict(row) if row else None


def require_account(conn, account_id: str, owner_id: str | None = None):
    row = conn.execute('SELECT * FROM portal_accounts WHERE id=?', (account_id,)).fetchone()
    if row is None or (owner_id is not None and row['owner_id'] != owner_id):
        raise LookupError('账号不存在')
    return row


def require_not_quarantined(conn):
    row = conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
    if row and row[0] == 'true':
        raise ValueError('恢复副本处于隔离核对状态，禁止调用、签到、扫码入池或新增额度')


def reserve_account(owner_id: str, uid: str, label: str, owner_sv: int | None = None) -> dict:
    with transaction() as conn:
        require_not_quarantined(conn)
        owner = conn.execute('SELECT enabled,sv FROM portal_users WHERE id=?', (owner_id,)).fetchone()
        if not owner or not owner['enabled'] or (owner_sv is not None and owner['sv'] != owner_sv):
            raise ValueError('用户授权已失效，未绑定账号')
        row = conn.execute("SELECT * FROM portal_accounts WHERE platform='workbuddy' AND realm='cn' AND provider_uid=?", (uid,)).fetchone()
        if row:
            if row['owner_id'] != owner_id:
                raise ValueError('该账号已绑定，不能重复领取或转移归属')
            return dict(row)
        if conn.execute('SELECT COUNT(*) FROM portal_accounts WHERE owner_id=?', (owner_id,)).fetchone()[0] >= 100:
            raise ValueError('当前用户账号数量已达到本地候选上限，请联系管理员')
        account_id = secrets.token_hex(16)
        conn.execute('INSERT INTO portal_accounts(id,owner_id,platform,realm,provider_uid,label,created_at) VALUES(?,?,?,?,?,?,?)',
                     (account_id, owner_id, 'workbuddy', 'cn', uid, label[:80], now()))
        return dict(require_account(conn, account_id, owner_id))


def _grant(conn, account, source, reference, amount, received_at, expires_at):
    if amount <= 0:
        return
    identifier = hashlib.sha256(f'{account["id"]}|{source}|{reference}|{expires_at}'.encode()).hexdigest()
    inserted = conn.execute('INSERT OR IGNORE INTO portal_grants VALUES(?,?,?,?,?,?,?,?,?)',
        (identifier, account['owner_id'], account['id'], source, reference, amount, amount, received_at, expires_at)).rowcount
    if inserted:
        conn.execute('INSERT INTO portal_events(owner_id,account_id,kind,amount_units,ts,expires_at,source_ref) VALUES(?,?,?,?,?,?,?)',
                     (account['owner_id'], account['id'], source, amount, received_at, expires_at, identifier))


def initial_credit(account_id: str, buckets: dict[int, int], owner_sv: int | None = None) -> None:
    with transaction() as conn:
        require_not_quarantined(conn)
        account = require_account(conn, account_id)
        if account['initial_state'] == 'confirmed':
            return
        at = now()
        owner = conn.execute('SELECT enabled,sv FROM portal_users WHERE id=?', (account['owner_id'],)).fetchone()
        if not owner or not owner['enabled'] or (owner_sv is not None and owner['sv'] != owner_sv):
            raise ValueError('用户已停用，初始积分未提交')
        for expiry, amount in buckets.items():
            _grant(conn, account, 'initial', 'initial', amount, at, expiry)
        conn.execute("UPDATE portal_accounts SET initial_state='confirmed',initial_at=?,status='active' WHERE id=?", (at, account_id))


def claim_checkin(account_id: str, actor: str) -> tuple[bool, dict]:
    with transaction() as conn:
        require_not_quarantined(conn)
        account = require_account(conn, account_id)
        owner = conn.execute('SELECT enabled FROM portal_users WHERE id=?', (account['owner_id'],)).fetchone()
        if not owner or not owner['enabled']:
            raise ValueError('用户已停用，不能发起新的签到')
        if account['status'] != 'active' or account['initial_state'] != 'confirmed':
            raise ValueError('初始积分待核实，暂不发起签到以免重复计算')
        today = day()
        prior = conn.execute("SELECT 1 FROM portal_checkins WHERE account_id=? AND day!=? AND state IN ('running','unknown') LIMIT 1",
                             (account_id, today)).fetchone()
        if prior:
            raise ValueError('此前签到仍在执行或结果未知，请先核对，不跨日重派')
        existing = conn.execute('SELECT * FROM portal_checkins WHERE account_id=? AND day=?', (account_id, today)).fetchone()
        if existing and existing['state'] != 'failed':
            return False, dict(existing)
        at = now()
        # Retry only a definite pre-dispatch failure. Unknown/running is never taken over by TTL.
        conn.execute("INSERT INTO portal_checkins(account_id,day,state,started_at,actor) VALUES(?,?,'running',?,?) "
                     "ON CONFLICT(account_id,day) DO UPDATE SET state='running',started_at=excluded.started_at,actor=excluded.actor,baseline='{}',code=NULL",
                     (account_id, today, at, actor))
        return True, {'day': today, 'started_at': at, 'state': 'running'}


def checkin_state(account_id: str, today: str, state: str, *, code=None, baseline=None):
    with transaction() as conn:
        if baseline is None:
            conn.execute('UPDATE portal_checkins SET state=?,code=?,confirmed_at=CASE WHEN ? IN (0,10001) THEN ? ELSE confirmed_at END WHERE account_id=? AND day=?',
                         (state, code, code, now(), account_id, today))
        else:
            conn.execute('UPDATE portal_checkins SET baseline=? WHERE account_id=? AND day=?',
                         (json.dumps(baseline), account_id, today))


def finish_checkin(account_id, today, code, before, after, started_at):
    with transaction() as conn:
        account = require_account(conn, account_id)
        row = conn.execute('SELECT * FROM portal_checkins WHERE account_id=? AND day=?', (account_id, today)).fetchone()
        if row is None or row['state'] == 'confirmed':
            return
        deltas = {expiry: amount - before.get(expiry, 0) for expiry, amount in after.items()
                  if amount > before.get(expiry, 0)}
        # Never mint a fixed 100 on an already-signed response or a balance guess.
        proven = (code == 0 and sum(deltas.values()) == 100 * SCALE
                  and sum(after.values()) - sum(before.values()) == 100 * SCALE)
        if proven:
            capped = {}
            for expiry, amount in deltas.items():
                expiry = min(expiry, started_at + 30 * 86400)
                capped[expiry] = capped.get(expiry, 0) + amount
            for expiry, amount in capped.items():
                _grant(conn, account, 'checkin', today, amount, started_at, expiry)
        conn.execute('UPDATE portal_checkins SET state=?,confirmed_at=?,code=?,credited_units=? WHERE account_id=? AND day=?',
                     ('confirmed' if proven else 'confirmed_uncredited', now(), code,
                      100 * SCALE if proven else 0, account_id, today))


def expire(conn, owner_id: str, at: int):
    rows = conn.execute('SELECT * FROM portal_grants WHERE owner_id=? AND expires_at<=? AND remaining_units>0', (owner_id, at)).fetchall()
    for row in rows:
        conn.execute('INSERT OR IGNORE INTO portal_events(owner_id,account_id,kind,amount_units,ts,expires_at,source_ref) VALUES(?,?,?,?,?,?,?)',
                     (owner_id, row['account_id'], 'expired', -row['remaining_units'], at, row['expires_at'], 'expiry:' + row['id']))
        conn.execute('UPDATE portal_grants SET remaining_units=0 WHERE id=?', (row['id'],))
    for row in conn.execute('SELECT id,remaining_units,expires_at FROM portal_bonus_grants WHERE owner_id=? AND expires_at<=? AND remaining_units>0', (owner_id, at)).fetchall():
        conn.execute('INSERT OR IGNORE INTO portal_events(owner_id,kind,amount_units,ts,expires_at,source_ref) VALUES(?,?,?,?,?,?)',
            (owner_id, 'expired', -row['remaining_units'], at, row['expires_at'], 'bonus-expiry:' + row['id']))
        conn.execute('UPDATE portal_bonus_grants SET remaining_units=0 WHERE id=?', (row['id'],))


def spendable_grants(conn, owner_id, at):
    return conn.execute("SELECT g.id,g.remaining_units,g.expires_at,'account' AS kind FROM portal_grants g "
        'JOIN portal_pool_links p ON p.account_id=g.account_id WHERE g.owner_id=? AND g.expires_at>? AND g.remaining_units>0 '
        "UNION ALL SELECT id,remaining_units,expires_at,'bonus' AS kind FROM portal_bonus_grants "
        'WHERE owner_id=? AND expires_at>? AND remaining_units>0 ORDER BY expires_at,id,kind', (owner_id, at, owner_id, at)).fetchall()


def overview(owner_id: str) -> dict:
    at = now()
    with transaction() as conn:
        expire(conn, owner_id, at)
        accounts = [dict(row) for row in conn.execute(
            'SELECT a.id,a.label,a.created_at,a.status,a.initial_state,c.state AS checkin_state,c.confirmed_at,c.credited_units '
            'FROM portal_accounts a LEFT JOIN portal_checkins c ON c.account_id=a.id AND c.day=? '
            'WHERE a.owner_id=? ORDER BY a.created_at DESC,a.id', (day(at), owner_id))]
        grants = [dict(row) for row in conn.execute(
            'SELECT source,amount_units,remaining_units,received_at,expires_at FROM portal_grants '
            'WHERE owner_id=? AND remaining_units>0 ORDER BY expires_at,id', (owner_id,))]
        grants += [dict(row) for row in conn.execute("SELECT 'admin_bonus' AS source,amount_units,remaining_units,received_at,expires_at "
            'FROM portal_bonus_grants WHERE owner_id=? AND remaining_units>0', (owner_id,))]
        grants.sort(key=lambda row: (row['expires_at'], row['received_at'], row['source']))
        events = [dict(row) for row in conn.execute('SELECT kind,amount_units,ts,expires_at FROM portal_events WHERE owner_id=? ORDER BY id DESC LIMIT 100', (owner_id,))]
        held = conn.execute("SELECT COALESCE(SUM(held_units),0) FROM portal_spends WHERE owner_id=? AND state IN ('reserved','forwarded','unknown')", (owner_id,)).fetchone()[0]
        unpooled = conn.execute('SELECT COALESCE(SUM(g.remaining_units),0) FROM portal_grants g LEFT JOIN portal_pool_links p ON p.account_id=g.account_id '
            'WHERE g.owner_id=? AND g.expires_at>? AND p.account_id IS NULL', (owner_id, at)).fetchone()[0]
        debt = conn.execute('SELECT amount_units FROM portal_debts WHERE owner_id=?', (owner_id,)).fetchone()
        quarantined = conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
        quarantined = bool(quarantined and quarantined[0] == 'true')
        pooled = {row[0] for row in conn.execute('SELECT account_id FROM portal_pool_links')}
        for account in accounts:
            account['pool_state'] = 'public' if account['id'] in pooled else 'pending'
        from . import portalbilling, portalpolicy
        return {'accounts': accounts, 'account_count': len(accounts),
                'active_account_count': sum(row['status'] == 'active' for row in accounts),
                'balance_units': sum(row['remaining_units'] for row in grants),
                'expiring_7d_units': sum(row['remaining_units'] for row in grants if row['expires_at'] <= at + 7 * 86400),
                'today_checkin_units': sum(row['credited_units'] or 0 for row in accounts),
                'grants': grants, 'events': events, 'scale': SCALE, 'snapshot_at': at,
                'held_units': held, 'debt_units': debt[0] if debt else 0,
                'callable_units': sum(row['remaining_units'] for row in grants) - unpooled,
                'pending_pool_units': unpooled,
                'pool_enabled': portalpolicy.enabled() and not quarantined,
                'restore_quarantined': quarantined,
                'dispatch_enabled': portalpolicy.enabled() and not quarantined and portalbilling.dispatch_ready(conn),
                'allowed_models': list(portalpolicy.MODELS),
                'dispatch_note': '恢复副本处于隔离核对状态，历史余额不可直接启用调用。' if quarantined else
                    ('公共池统一调度，按调用者实际消耗扣分；余额为零拒绝新请求，不提供透支。'
                     if portalbilling.dispatch_ready(conn) else portalbilling.MESSAGE)
                    if portalpolicy.enabled() else '公共池调用尚未启用，调用保持关闭。'}


def automatic_enabled() -> bool:
    with transaction() as conn:
        row = conn.execute("SELECT value FROM portal_settings WHERE key='automatic_checkin'").fetchone()
        return bool(row and row[0] == 'true')
