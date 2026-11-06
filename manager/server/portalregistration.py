"""Invitation-only signup: bounded work, persistent device binding, shared limits.

A browser credential is not physical-device attestation. Invitations remain the
hard admission boundary when cookies are cleared or another browser is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3

from fastapi import HTTPException
from . import iputil, portalstore as store, security
from .services.asyncwork import joined_thread

COOKIE='zh-registration-device'
BODY_LIMIT=8192
MAX_RATE_ROWS=4096
_active=0

SCHEMA='''
CREATE TABLE IF NOT EXISTS portal_registration_invites(
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
 expires_at INTEGER NOT NULL, created_by TEXT NOT NULL, used_at INTEGER,
 used_by TEXT UNIQUE REFERENCES portal_users(id), revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portal_registration_devices(
 device_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE REFERENCES portal_users(id),
 registered_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_registration_limits(
 scope TEXT NOT NULL, window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
 count INTEGER NOT NULL, PRIMARY KEY(scope,window_start)
);
CREATE INDEX IF NOT EXISTS portal_registration_limit_expiry ON portal_registration_limits(window_end);
CREATE INDEX IF NOT EXISTS portal_username_casefold ON portal_users(lower(username));
CREATE TRIGGER IF NOT EXISTS portal_registered_device_no_update BEFORE UPDATE ON portal_registration_devices
 BEGIN SELECT RAISE(ABORT,'registered device bindings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portal_registered_device_no_delete BEFORE DELETE ON portal_registration_devices
 BEGIN SELECT RAISE(ABORT,'registered device bindings are immutable'); END;
'''


def secret():
    # Independent of login/session rotation. Paired backups preserve this secret
    # and device bindings together; never expose it in public responses/logs.
    with store.transaction() as conn:
        row=conn.execute("SELECT value FROM portal_settings WHERE key='registration_secret'").fetchone()
        if row:
            if not re.fullmatch('[a-f0-9]{96}',row[0]):raise ValueError('Registration identity is invalid')
            return row[0]
        if (conn.execute('SELECT 1 FROM portal_registration_devices LIMIT 1').fetchone()
                or conn.execute('SELECT 1 FROM portal_registration_limits LIMIT 1').fetchone()):
            raise ValueError('Registration identity is missing; recover its paired state')
        store.require_not_quarantined(conn)
        value=secrets.token_hex(48)
        conn.execute('INSERT INTO portal_settings VALUES(?,?)',('registration_secret',value))
        return value


def device(raw, signing_secret):
    if not isinstance(raw,str) or not 1<=len(raw)<=512:return None
    if not re.fullmatch(r'v1\.[a-f0-9]{64}\.[a-f0-9]{64}',raw):return None
    _,nonce,signature=raw.split('.')
    expected=hmac.new(signing_secret.encode(),('registration-cookie-v1:'+nonce).encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):return None
    return hmac.new(signing_secret.encode(),('device:'+nonce).encode(),hashlib.sha256).hexdigest()


def issue_device(signing_secret):
    nonce=secrets.token_hex(32)
    signature=hmac.new(signing_secret.encode(),('registration-cookie-v1:'+nonce).encode(),hashlib.sha256).hexdigest()
    return f'v1.{nonce}.{signature}'


def scoped(value, signing_secret):
    return hmac.new(signing_secret.encode(),value.encode(),hashlib.sha256).hexdigest()


def network(address):
    try:
        ip=ipaddress.ip_address(address)
        return str(ipaddress.ip_network(f'{ip}/{64 if ip.version==6 else 24}',strict=False))
    except ValueError:return 'unknown-peer'


def take_limits(signing_secret, entries):
    stamp=store.now();blocked=False
    with store.transaction() as conn:
        store.require_not_quarantined(conn)
        conn.execute('DELETE FROM portal_registration_limits WHERE window_end<=?',(stamp,))
        pending=[];new_rows=0
        for scope,seconds,maximum in entries:
            key=scoped(scope,signing_secret);start=stamp//seconds*seconds
            row=conn.execute('SELECT count FROM portal_registration_limits WHERE scope=? AND window_start=?',(key,start)).fetchone()
            if row and row['count']>=maximum:blocked=True
            if row is None:new_rows+=1
            pending.append((key,start,start+seconds))
        if conn.execute('SELECT COUNT(*) FROM portal_registration_limits').fetchone()[0]+new_rows>MAX_RATE_ROWS:blocked=True
        if not blocked:
            for key,start,end in pending:
                conn.execute('INSERT INTO portal_registration_limits VALUES(?,?,?,1) '
                    'ON CONFLICT(scope,window_start) DO UPDATE SET count=count+1',(key,start,end))
    if blocked:raise HTTPException(429,'注册尝试过多，请稍后重试',headers={'Retry-After':'600'})


def policy(request):
    with store.transaction() as conn:
        row=conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
        if row and row[0]=='true':
            return {'enabled':False,'invite_required':True,'device_registered':False,
                    'minimum_password_length':12,'device_note':'恢复隔离期间暂停注册。'},None
    signing_secret=secret()
    raw=request.cookies.get(COOKIE,'')
    identifier=device(raw,signing_secret)
    new_cookie=None
    if identifier is None:
        address=iputil.client_ip(request)
        take_limits(signing_secret,[(f'device-issue:{address}',600,10),('device-issue:global',60,120)])
        raw=issue_device(signing_secret)
        identifier=device(raw,signing_secret)
        new_cookie=raw
    with store.transaction() as conn:
        row=conn.execute('SELECT 1 FROM portal_registration_devices WHERE device_hash=?',(identifier,)).fetchone()
        quarantine=conn.execute("SELECT value FROM portal_settings WHERE key='restore_quarantine'").fetchone()
    return {'enabled':not bool(quarantine and quarantine[0]=='true'),'invite_required':True,
            'device_registered':bool(row),'minimum_password_length':12,
            'device_note':'本浏览器只能注册一个账号；邀请码仅限一次成功注册。'},new_cookie


def create_invites(admin, *, count=1, days=7):
    if type(count) is not int or not 1<=count<=20 or type(days) is not int or not 1<=days<=30:
        raise HTTPException(400,'每次生成1至20个邀请码，有效期1至30天')
    result=[];stamp=store.now()
    with security.authorization_guard(admin),store.transaction() as conn:
        store.require_not_quarantined(conn)
        for _ in range(count):
            token='zh-invite-'+secrets.token_urlsafe(24)
            identifier=secrets.token_hex(16)
            conn.execute('INSERT INTO portal_registration_invites(id,token_hash,created_at,expires_at,created_by) VALUES(?,?,?,?,?)',
                (identifier,hashlib.sha256(token.encode()).hexdigest(),stamp,stamp+days*86400,admin['username']))
            result.append({'id':identifier,'code':token,'expires_at':stamp+days*86400})
    return {'items':result,'message':'邀请码明文仅显示这一次；每个邀请码仅能注册一个账号。'}


def list_invites():
    with store.transaction() as conn:
        return {'items':[dict(row) for row in conn.execute('SELECT id,created_at,expires_at,used_at,revoked '
            'FROM portal_registration_invites ORDER BY created_at DESC,id LIMIT 100')]}


def revoke_invite(identifier,admin):
    with security.authorization_guard(admin),store.transaction() as conn:
        store.require_not_quarantined(conn)
        changed=conn.execute('UPDATE portal_registration_invites SET revoked=1 WHERE id=? AND used_at IS NULL',(identifier,)).rowcount
    if not changed:raise HTTPException(409,'邀请码不存在或已经使用')
    return {'ok':True}


def precheck(body,identifier,signing_secret):
    if set(body)-{'username','password','invite'}:raise HTTPException(400,'注册字段无效')
    name,password,token=body.get('username'),body.get('password'),body.get('invite')
    if not isinstance(name,str) or not re.fullmatch('[a-zA-Z0-9_-]{3,40}',name):
        raise HTTPException(400,'用户名需为3至40位字母、数字、下划线或短横线')
    name=name.lower()
    if name in {'admin','administrator','root','system'}:raise HTTPException(400,'请选择其他用户名')
    if not isinstance(password,str) or not 12<=len(password)<=128:
        raise HTTPException(400,'注册密码需为12至128位')
    problem=security.managed_user_password_problem(password,name)
    if problem:raise HTTPException(400,problem)
    if not isinstance(token,str) or not re.fullmatch('zh-invite-[A-Za-z0-9_-]{32}',token):
        raise HTTPException(400,'注册信息或邀请码无效')
    hashed=hashlib.sha256(token.encode()).hexdigest()
    with store.transaction() as conn:
        store.require_not_quarantined(conn)
        if conn.execute('SELECT 1 FROM portal_registration_devices WHERE device_hash=?',(identifier,)).fetchone():
            raise HTTPException(409,'此浏览器已经注册过账号，请直接登录')
        invitation=conn.execute('SELECT 1 FROM portal_registration_invites WHERE token_hash=? AND revoked=0 AND used_at IS NULL AND expires_at>?',
                                (hashed,store.now())).fetchone()
        exists=conn.execute('SELECT 1 FROM portal_users WHERE lower(username)=?',(name,)).fetchone()
        if not invitation or exists:raise HTTPException(400,'注册信息或邀请码无效')
    return name,password,hashed


def commit_user(name,hashed_password,invite_hash,identifier):
    try:
        with store.transaction() as conn:
            store.require_not_quarantined(conn)
            if conn.execute('SELECT 1 FROM portal_registration_devices WHERE device_hash=?',(identifier,)).fetchone():
                raise HTTPException(409,'此浏览器已经注册过账号，请直接登录')
            if conn.execute('SELECT 1 FROM portal_users WHERE lower(username)=?',(name,)).fetchone():
                raise HTTPException(400,'注册信息或邀请码无效')
            invite=conn.execute('SELECT id FROM portal_registration_invites WHERE token_hash=? AND revoked=0 AND used_at IS NULL AND expires_at>?',
                                 (invite_hash,store.now())).fetchone()
            if not invite:raise HTTPException(400,'注册信息或邀请码无效')
            user_id=secrets.token_hex(16);stamp=store.now()
            conn.execute('INSERT INTO portal_users(id,username,pwd_hash,created_at) VALUES(?,?,?,?)',(user_id,name,hashed_password,stamp))
            conn.execute('INSERT INTO portal_registration_devices VALUES(?,?,?)',(identifier,user_id,stamp))
            changed=conn.execute('UPDATE portal_registration_invites SET used_at=?,used_by=? WHERE id=? AND used_at IS NULL AND revoked=0 AND expires_at>?',
                                 (stamp,user_id,invite['id'],stamp)).rowcount
            if changed!=1:raise HTTPException(409,'邀请码状态已变化，请核对后重试')
    except sqlite3.IntegrityError as exc:raise HTTPException(409,'账号、设备或邀请码已经使用，请登录或核对') from exc
    return {'id':user_id,'username':name,'registered':True,'message':'注册成功，请使用此账号登录。'}


async def register(request):
    global _active
    if _active>=2:raise HTTPException(429,'注册繁忙，请稍后重试',headers={'Retry-After':'10'})
    _active+=1
    try:
        signing_secret=await joined_thread(secret)
        identifier=device(request.cookies.get(COOKIE,''),signing_secret)
        if identifier is None:raise HTTPException(400,'请先打开注册页面，再提交注册')
        address=iputil.client_ip(request)
        await joined_thread(take_limits,signing_secret,[(f'register:ip:{address}',600,6),
            (f'register:network:{network(address)}',600,30),(f'register:device:{identifier}',600,4),
            ('register:global',60,60),('register:global-hour',3600,300)])
        if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
            raise HTTPException(415,'注册仅接受JSON请求')
        raw=bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw)+len(chunk)>BODY_LIMIT:raise HTTPException(413,'注册请求过大')
                    raw.extend(chunk)
        except TimeoutError as exc:raise HTTPException(408,'注册请求读取超时') from exc
        try:body=json.loads(raw)
        except (ValueError,UnicodeError,RecursionError) as exc:raise HTTPException(400,'注册格式无效') from exc
        if not isinstance(body,dict):raise HTTPException(400,'注册格式无效')
        name,password,invite_hash=await joined_thread(precheck,body,identifier,signing_secret)
        password_hash=await joined_thread(security.make_hash,password)
        return await joined_thread(commit_user,name,password_hash,invite_hash,identifier)
    except ValueError as exc:raise HTTPException(409,'注册暂不可用，请稍后核对') from exc
    finally:_active-=1
