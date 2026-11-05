"""One user-facing default key; identity remains key-id/hash, never its label."""
from __future__ import annotations

import errno
import hmac
import time
from contextlib import contextmanager, ExitStack
from fastapi import HTTPException
from . import config, db, keysvc, portalpolicy, portalstore as store
from .services import credentiallease


def owned_rows(conn, main, user):
    rows=[]
    for row in conn.execute('SELECT l.key_id,l.key_hash,k.owner_sv,k.revoked,k.created_at '
            'FROM portal_key_links l JOIN portal_keys k ON k.key_id=l.key_id '
            'WHERE l.owner_id=? ORDER BY k.created_at,l.key_id',(user['id'],)):
        live=main.execute('SELECT id,key_hash,prefix,enabled,source FROM api_keys WHERE id=?',(row['key_id'],)).fetchone()
        if live and live['source']==portalpolicy.SOURCE and hmac.compare_digest(live['key_hash'],row['key_hash']):
            rows.append(dict(row, prefix=live['prefix'],enabled=bool(live['enabled'])))
    return rows


def fresh_owner(conn,user):
    row=conn.execute('SELECT * FROM portal_users WHERE id=?',(user['id'],)).fetchone()
    if not row or not row['enabled'] or row['sv']!=user['sv']:
        raise HTTPException(status_code=401,detail='用户登录已失效')
    return dict(row)


@contextmanager
def issuance_guard():
    # Kernel-held cross-process lock; no TTL and no inferred owner death. Use
    # the real portal DB as ownership reference for its private .refresh.lock.
    until=time.monotonic()+3
    with ExitStack() as stack:
        while True:
            try:stack.enter_context(credentiallease.mutation(config.DATA_DIR/'user-portal.sqlite3'))
            except OSError as exc:
                if exc.errno!=errno.EBUSY:raise
                if time.monotonic()>=until:raise HTTPException(429,'默认密钥正在核对，请稍后刷新',headers={'Retry-After':'3'}) from exc
                time.sleep(.01)
            else:break
        yield


def rename_owned(main,conn,user,rows):
    for row in rows:
        main.execute('UPDATE api_keys SET name=? WHERE id=? AND key_hash=? AND source=?',
                     (user['username'],row['key_id'],row['key_hash'],portalpolicy.SOURCE))
        conn.execute('UPDATE portal_keys SET label=? WHERE key_id=?',(user['username'],row['key_id']))


def adopt_or_create(user, *, create=False):
    if not create:
        # A quarantined, read-only login need not wait for a mutation lease.
        with store.transaction() as conn:
            fresh_owner(conn,user)
            row=conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
            if row and row[0]=='true':return None
    with issuance_guard():
        # Keep main -> wallet ordering, including for metadata. IDs/hashes,
        # usage, history and legacy extra keys are never rotated or rewritten.
        with db.transaction() as main, store.transaction() as conn:
            user=fresh_owner(conn,user)
            quarantine=conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
            if quarantine and quarantine[0]=='true':
                if not create:return None
                store.require_not_quarantined(conn)
            rows=owned_rows(conn,main,user)
            eligible=[row for row in rows if not row['revoked'] and row['owner_sv']==user['sv']]
            rename_owned(main,conn,user,rows)
            if eligible:
                chosen=eligible[0]
                return {'id':chosen['key_id'],'key':None,'label':user['username'],'existing':True,
                        'message':'已沿用现有默认密钥；不会增发，历史明文不能重新读取。'}
            if not create:return None
        # Commit a disabled, portal-sourced main row BEFORE publishing any link
        # in the separate wallet DB. A failed main commit cannot leave a link
        # pointing at an uncommitted/reusable numeric key ID.
        with db.transaction() as main:
            made=keysvc.create_key(user['username'],models=list(portalpolicy.MODELS),realm='cn',
                                  platforms=['workbuddy'],_conn=main,_portal=True)
            main.execute('UPDATE api_keys SET source=?,enabled=0 WHERE id=?',(portalpolicy.SOURCE,made['id']))
        with store.transaction() as conn:
            store.require_not_quarantined(conn)
            user=fresh_owner(conn,user)
            conn.execute('INSERT INTO portal_key_links VALUES(?,?,?)',(made['id'],user['id'],keysvc._hash(made['key'])))
            conn.execute('INSERT INTO portal_keys VALUES(?,?,?,?,0)',(made['id'],user['sv'],user['username'],store.now()))
        with db.transaction() as main, store.transaction() as conn:
            store.require_not_quarantined(conn)
            fresh_owner(conn,user)
            main.execute('UPDATE api_keys SET enabled=1 WHERE id=? AND key_hash=? AND source=?',
                         (made['id'],keysvc._hash(made['key']),portalpolicy.SOURCE))
        return {'id':made['id'],'key':made['key'],'label':user['username'],'existing':False,
                'message':'默认密钥已启用；明文仅返回这一次，请妥善保存。'}


def revoke(user,key_id):
    with issuance_guard():
        # First durably disable in main. If wallet publication fails, the key
        # stays disabled and cannot be bypassed by requesting a replacement.
        with db.transaction() as main,store.transaction() as conn:
            store.require_not_quarantined(conn)
            fresh_owner(conn,user)
            row=next((row for row in owned_rows(conn,main,user) if row['key_id']==key_id),None)
            if row is None:raise HTTPException(404,'密钥不存在')
            main.execute('UPDATE api_keys SET enabled=0 WHERE id=? AND key_hash=? AND source=?',
                         (key_id,row['key_hash'],portalpolicy.SOURCE))
        with store.transaction() as conn:
            store.require_not_quarantined(conn)
            fresh_owner(conn,user)
            conn.execute('UPDATE portal_keys SET revoked=1 WHERE key_id=?',(key_id,))
    return {'ok':True}


def adopt_existing_labels():
    """Trusted publisher hook after verified drain, not a public API or startup job."""
    changed=0
    with issuance_guard(),db.transaction() as main,store.transaction() as conn:
        store.require_not_quarantined(conn)
        for raw in conn.execute('SELECT * FROM portal_users'):
            user=dict(raw);rows=owned_rows(conn,main,user)
            rename_owned(main,conn,user,rows)
            changed+=len(rows)
    return {'renamed_owned_keys':changed,'created_keys':0,'revoked_keys':0}
