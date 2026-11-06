"""Local HTTP and cross-connection tests; no real users, keys or provider calls."""
import asyncio
import concurrent.futures
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.tests.audit_fixtures import state

PASSWORD='Cobalt!river_Sample904'


@pytest.fixture
def web(state,monkeypatch):
    from server import config,portalregistration as registration,security
    from server.routers import portal
    monkeypatch.setattr(config,'TRUST_PROXY',False)
    monkeypatch.setattr(registration,'_active',0)
    security.save_users({'secret':'synthetic-'+'s'*64,'role_policy_version':2,'users':[
        {'username':'admin','role':'admin','sv':1,'pwd_hash':'not-a-login-hash'},
        {'username':'reader','role':'account_admin','sv':1,'pwd_hash':'not-a-login-hash'}]})
    app=FastAPI();app.include_router(portal.router);app.include_router(portal.admin_router)
    admin={'cookie':f'{config.COOKIE_NAME}={security.issue_token("admin","admin")}','origin':'http://testserver'}
    return app,admin


def issue(web,count=1):
    app,admin=web
    with TestClient(app) as client:
        result=client.post('/api/portal-admin/invitations',headers=admin,json={'count':count})
    assert result.status_code==200,result.text
    return result.json()['items']


def signup(client,code,name='alice',**kwargs):
    return client.post('/api/portal/register',headers={'origin':'http://testserver'},
                       json={'username':name,'password':PASSWORD,'invite':code},**kwargs)


def test_invited_signup_is_atomic_zero_credit_and_password_is_hashed(web):
    from server import portalregistration as registration,portalstore as store,security
    app,_=web;code=issue(web)[0]['code']
    with TestClient(app) as client:
        policy=client.get('/api/portal/registration')
        assert policy.status_code==200 and policy.json()['invite_required'] is True
        assert 'httponly' in policy.headers['set-cookie'].lower() and 'samesite=strict' in policy.headers['set-cookie'].lower()
        result=signup(client,code,'Alice')
        assert result.status_code==201,result.text
        assert result.json()['username']=='alice' and 'password' not in result.json()
        assert client.get('/api/portal/me').status_code==401
        login=client.post('/api/portal/login',headers={'origin':'http://testserver'},json={'username':'alice','password':PASSWORD})
        assert login.status_code==200,login.text
        assert client.get('/api/portal/me').json()['username']=='alice'
        assert client.get('/api/portal/registration').json()['device_registered'] is True
    with store.transaction() as conn:
        user=dict(conn.execute('SELECT * FROM portal_users').fetchone())
        assert security.verify_pwd(PASSWORD,user['pwd_hash']) and PASSWORD not in user['pwd_hash']
        assert conn.execute('SELECT COUNT(*) FROM portal_registration_devices').fetchone()[0]==1
        invite=dict(conn.execute('SELECT * FROM portal_registration_invites').fetchone())
        assert invite['used_by']==user['id'] and invite['used_at'] is not None and code not in json.dumps(invite)
        assert conn.execute('SELECT COUNT(*) FROM portal_grants').fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM portal_bonus_grants').fetchone()[0]==0
    assert registration._active==0


def test_one_device_cannot_register_another_account_even_with_another_invite(web):
    from server import portalstore as store
    app,_=web;codes=issue(web,2)
    with TestClient(app) as client:
        client.get('/api/portal/registration')
        assert signup(client,codes[0]['code']).status_code==201
        assert signup(client,codes[1]['code'],'bob').status_code==409
    with store.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM portal_registration_invites WHERE used_at IS NOT NULL').fetchone()[0]==1


def test_one_invite_two_concurrent_devices_commits_exactly_one_user(web):
    from server import portalstore as store
    app,_=web;code=issue(web)[0]['code']
    async def run():
        transport=httpx.ASGITransport(app)
        async with httpx.AsyncClient(transport=transport,base_url='http://testserver') as a,httpx.AsyncClient(transport=transport,base_url='http://testserver') as b:
            await asyncio.gather(a.get('/api/portal/registration'),b.get('/api/portal/registration'))
            return await asyncio.gather(*[client.post('/api/portal/register',headers={'origin':'http://testserver'},json={
                'username':name,'password':PASSWORD,'invite':code}) for client,name in [(a,'alice'),(b,'bob')]])
    results=asyncio.run(run())
    assert sorted(result.status_code for result in results) in ([201,400],[201,409])
    with store.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM portal_registration_devices').fetchone()[0]==1


def test_two_invites_same_device_concurrently_register_exactly_one_account(web):
    from server import portalregistration as registration,portalstore as store
    app,_=web;codes=issue(web,2)
    with TestClient(app) as browser:
        browser.get('/api/portal/registration')
        credential=browser.cookies.get(registration.COOKIE)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
            return await asyncio.gather(*[client.post('/api/portal/register',headers={'origin':'http://testserver',
                'cookie':f'{registration.COOKIE}={credential}'},json={'username':name,'password':PASSWORD,'invite':code['code']})
                for name,code in zip(('alice','bob'),codes)])
    results=asyncio.run(run())
    assert sorted(item.status_code for item in results)==[201,409]
    with store.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM portal_registration_invites WHERE used_at IS NOT NULL').fetchone()[0]==1


def test_missing_device_signing_identity_is_never_silently_recreated(web):
    from server import portalstore as store
    app,_=web;code=issue(web)[0]['code']
    with TestClient(app) as client:
        client.get('/api/portal/registration')
        assert signup(client,code).status_code==201
        with store.transaction() as conn:
            original=conn.execute("SELECT value FROM portal_settings WHERE key='registration_secret'").fetchone()[0]
            conn.execute("DELETE FROM portal_settings WHERE key='registration_secret'")
        assert client.get('/api/portal/registration').status_code==409
        with store.transaction() as conn:
            assert conn.execute("SELECT 1 FROM portal_settings WHERE key='registration_secret'").fetchone() is None
            conn.execute('INSERT INTO portal_settings VALUES(?,?)',('registration_secret',original))
        assert client.get('/api/portal/registration').json()['device_registered'] is True


def test_device_write_failure_rolls_back_user_and_invite_consumption(web):
    from server import portalstore as store
    app,_=web;code=issue(web)[0]['code']
    with TestClient(app) as client:
        client.get('/api/portal/registration')
        with store.transaction() as conn:
            conn.execute("CREATE TRIGGER synthetic_device_fault BEFORE INSERT ON portal_registration_devices BEGIN SELECT RAISE(ABORT,'synthetic fault'); END")
        assert signup(client,code).status_code==409
    with store.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0]==0
        assert conn.execute('SELECT used_at FROM portal_registration_invites').fetchone()[0] is None


@pytest.mark.parametrize('kind',['invalid','expired','revoked','missing_device','forged_device','cross_origin'])
def test_invalid_invite_device_and_origin_fail_before_password_hash(web,monkeypatch,kind):
    from server import portalregistration as registration,portalstore as store,security
    app,_=web;invitation=issue(web)[0];code=invitation['code']
    if kind in ('expired','revoked'):
        with store.transaction() as conn:
            conn.execute('UPDATE portal_registration_invites SET expires_at=?,revoked=? WHERE id=?',
                         (store.now()-1 if kind=='expired' else store.now()+100, int(kind=='revoked'),invitation['id']))
    def forbidden(*args):raise AssertionError('invalid signup reached expensive password hashing')
    monkeypatch.setattr(security,'make_hash',forbidden)
    with TestClient(app) as client:
        if kind!='missing_device':client.get('/api/portal/registration')
        if kind=='forged_device':client.cookies.set(registration.COOKIE,'invalid-signature')
        result=client.post('/api/portal/register',headers={'origin':'http://other.invalid' if kind=='cross_origin' else 'http://testserver'},json={
            'username':'alice','password':PASSWORD,'invite':'zh-invite-'+'x'*32 if kind=='invalid' else code})
    assert result.status_code in (400,403),result.text
    with store.transaction() as conn:assert conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0]==0


def test_rate_limit_survives_reload_and_ignores_forged_forwarded_ip(web):
    from server import portalregistration as registration,portalstore as store
    app,_=web;statuses=[]
    for index in range(7):
        if index==6:
            import importlib
            importlib.reload(registration)
        with TestClient(app) as client:
            assert client.get('/api/portal/registration').status_code==200
            result=client.post('/api/portal/register',headers={'origin':'http://testserver',
                'x-real-ip':f'203.0.113.{index}','x-forwarded-for':f'203.0.113.{index}'},
                json={'username':f'person{index}','password':PASSWORD,'invite':'invalid'})
            statuses.append(result.status_code)
    assert statuses==[400]*6+[429]
    assert result.headers['retry-after']=='600'
    # Limits live in SQLite, not a process-local counter or browser storage.
    with store.transaction() as conn:
        rows=conn.execute('SELECT scope,count FROM portal_registration_limits').fetchall()
        assert rows and all('203.0.113' not in row['scope'] for row in rows)
    assert registration._active==0


@pytest.mark.parametrize('mode',['oversized','fields','quarantine','capacity'])
def test_registration_body_and_recovery_boundaries(web,monkeypatch,mode):
    from server import portalregistration as registration,portalstore as store
    app,_=web;code=issue(web)[0]['code']
    with TestClient(app) as client:
        client.get('/api/portal/registration')
        if mode=='oversized':
            result=client.post('/api/portal/register',headers={'origin':'http://testserver','content-type':'application/json'},content=b'x'*9000)
            assert result.status_code==413
        elif mode=='fields':
            result=client.post('/api/portal/register',headers={'origin':'http://testserver'},json={'username':'alice','password':PASSWORD,'invite':code,'role':'admin'})
            assert result.status_code==400
        elif mode=='quarantine':
            with store.transaction() as conn:conn.execute("INSERT OR REPLACE INTO portal_settings VALUES('restore_quarantine','true')")
            assert signup(client,code).status_code==409
        else:
            monkeypatch.setattr(registration,'_active',2)
            assert signup(client,code).status_code==429
    with store.transaction() as conn:assert conn.execute('SELECT COUNT(*) FROM portal_users').fetchone()[0]==0


def test_invitation_api_is_session_admin_only_and_never_reveals_codes_again(web):
    from server import config,security
    app,admin=web;invitation=issue(web)[0]
    with TestClient(app) as client:
        assert client.post('/api/portal-admin/invitations',headers={'origin':'http://testserver'},json={}).status_code==401
        reader={'cookie':f'{config.COOKIE_NAME}={security.issue_token("reader","account_admin")}','origin':'http://testserver'}
        assert client.post('/api/portal-admin/invitations',headers=reader,json={}).status_code==403
        listing=client.get('/api/portal-admin/invitations',headers=admin)
        assert listing.status_code==200 and invitation['code'] not in listing.text and 'token_hash' not in listing.text
        assert client.delete('/api/portal-admin/invitations/'+invitation['id'],headers=admin).status_code==200


def make_user(name='alice'):
    from server import portalstore as store
    with store.transaction() as conn:
        conn.execute('INSERT INTO portal_users(id,username,pwd_hash,created_at) VALUES(?,?,?,?)',(name,name,'synthetic-unused',store.now()))
    return store.user(name)


def test_concurrent_default_key_initialization_issues_one_secret_and_no_orphans(state):
    from server import db,portalkeys,portalstore as store
    user=make_user()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results=list(executor.map(lambda _:portalkeys.adopt_or_create(user,create=True),range(5)))
    assert len({item['id'] for item in results})==1 and sum(bool(item['key']) for item in results)==1
    assert db.query_one('SELECT COUNT(*) FROM api_keys')[0]==1
    assert db.query_one('SELECT name,enabled FROM api_keys')['name']=='alice'
    with store.transaction() as conn:assert conn.execute('SELECT COUNT(*) FROM portal_key_links').fetchone()[0]==1


def test_existing_default_key_keeps_identity_usage_and_admin_disabled_state(state):
    from server import db,keysvc,portalkeys
    user=make_user();first=portalkeys.adopt_or_create(user,create=True)
    db.execute('UPDATE api_keys SET name=?,used_credit_units=?,used_tokens=?,enabled=0 WHERE id=?',('old label',123457,22,first['id']))
    before=dict(db.query_one('SELECT * FROM api_keys WHERE id=?',(first['id'],)))
    result=portalkeys.adopt_or_create(user,create=True)
    after=dict(db.query_one('SELECT * FROM api_keys WHERE id=?',(first['id'],)))
    assert result['existing'] is True and result['key'] is None and result['id']==first['id']
    for field in ('key_hash','used_credit_units','used_tokens','enabled'):assert after[field]==before[field]
    assert after['name']=='alice' and db.query_one('SELECT COUNT(*) FROM api_keys')[0]==1


def test_default_key_issuance_is_serialized_between_processes(state):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from server import db
    make_user()
    source=Path(__file__).resolve().parents[2]
    script='''
import json,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from server import config,db,portalkeys,portalstore
config.DATA_DIR=Path(sys.argv[2]);config.DB_PATH=config.DATA_DIR/'manager.db'
db.connect()
result=portalkeys.adopt_or_create(portalstore.user('alice'),create=True)
print(json.dumps({'id':result['id'],'secret_issued':bool(result['key'])}))
'''
    children=[subprocess.Popen([sys.executable,'-B','-X','utf8','-c',script,str(source),str(state)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=dict(os.environ)) for _ in range(2)]
    results=[]
    try:
        for child in children:
            stdout,stderr=child.communicate(timeout=20)
            assert child.returncode==0,stderr.decode('utf-8','replace')
            results.append(json.loads(stdout))
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.wait(timeout=5)
    assert len({item['id'] for item in results})==1 and sum(item['secret_issued'] for item in results)==1
    assert db.query_one('SELECT COUNT(*) FROM api_keys')[0]==1


def test_key_main_commit_precedes_publishing_any_portal_link(state,monkeypatch):
    from contextlib import contextmanager
    from server import db,portalkeys,portalstore as store
    user=make_user();original=db.transaction
    calls=0
    @contextmanager
    def failing():
        nonlocal calls
        calls+=1
        with original() as conn:
            yield conn
            if calls==2:raise RuntimeError('synthetic main commit failure before allocation publishes')
    monkeypatch.setattr(db,'transaction',failing)
    with pytest.raises(RuntimeError,match='synthetic main commit failure'):
        portalkeys.adopt_or_create(user,create=True)
    assert db.query_one('SELECT COUNT(*) FROM api_keys')[0]==0
    with store.transaction() as conn:assert conn.execute('SELECT COUNT(*) FROM portal_key_links').fetchone()[0]==0


def test_owned_label_adoption_changes_only_metadata_without_reissuing_keys(state):
    from server import db,portalkeys
    user=make_user();made=portalkeys.adopt_or_create(user,create=True)
    db.execute('UPDATE api_keys SET name=?,used_credit_units=? WHERE id=?',('old custom label',123457,made['id']))
    before=dict(db.query_one('SELECT * FROM api_keys WHERE id=?',(made['id'],)))
    assert portalkeys.adopt_existing_labels()=={'renamed_owned_keys':1,'created_keys':0,'revoked_keys':0}
    after=dict(db.query_one('SELECT * FROM api_keys WHERE id=?',(made['id'],)))
    assert after['name']=='alice'
    assert {key:value for key,value in before.items() if key!='name'}=={key:value for key,value in after.items() if key!='name'}


def test_failed_wallet_revoke_keeps_main_disabled_and_cannot_issue_replacement(state,monkeypatch):
    from contextlib import contextmanager
    from server import db,portalkeys,portalstore as store
    user=make_user();made=portalkeys.adopt_or_create(user,create=True)
    original=store.transaction;calls=0
    @contextmanager
    def failing():
        nonlocal calls
        calls+=1
        with original() as conn:
            yield conn
            if calls==2:raise RuntimeError('synthetic wallet revoke commit failed')
    monkeypatch.setattr(store,'transaction',failing)
    with pytest.raises(RuntimeError,match='wallet revoke commit failed'):portalkeys.revoke(user,made['id'])
    assert db.query_one('SELECT enabled FROM api_keys WHERE id=?',(made['id'],))[0]==0
    reused=portalkeys.adopt_or_create(user,create=True)
    assert reused['id']==made['id'] and reused['existing'] and reused['key'] is None
    assert db.query_one('SELECT COUNT(*) FROM api_keys')[0]==1
    assert portalkeys.revoke(user,made['id'])=={'ok':True}
    with store.transaction() as conn:
        assert conn.execute('SELECT revoked FROM portal_keys WHERE key_id=?',(made['id'],)).fetchone()[0]==1


def test_revoke_and_default_initialization_share_the_same_mutation_lock(state):
    from server import db,portalkeys,portalstore as store
    user=make_user();made=portalkeys.adopt_or_create(user,create=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        jobs=[executor.submit(portalkeys.revoke,user,made['id'])]
        jobs.extend(executor.submit(portalkeys.adopt_or_create,user,create=True) for _ in range(4))
        for job in jobs:job.result(timeout=10)
    assert db.query_one('SELECT enabled FROM api_keys WHERE id=?',(made['id'],))[0]==0
    with store.transaction() as conn:
        assert conn.execute('SELECT revoked FROM portal_keys WHERE key_id=?',(made['id'],)).fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM portal_keys WHERE revoked=0').fetchone()[0]<=1
    assert db.query_one('SELECT COUNT(*) FROM api_keys')[0]<=2


def test_quarantine_login_metadata_skips_mutation_lock_and_policy_creates_no_identity(state,monkeypatch):
    from server import db,portalkeys,portalregistration as registration,portalstore as store
    from starlette.requests import Request
    user=make_user();made=portalkeys.adopt_or_create(user,create=True)
    db.execute('UPDATE api_keys SET name=? WHERE id=?',('legacy unchanged',made['id']))
    with store.transaction() as conn:conn.execute('INSERT INTO portal_settings VALUES(?,?)',('restore_quarantine','true'))
    def forbidden():raise AssertionError('read-only quarantine must not acquire mutation lock')
    monkeypatch.setattr(portalkeys,'issuance_guard',forbidden)
    assert portalkeys.adopt_or_create(user,create=False) is None
    payload,cookie=registration.policy(Request({'type':'http','headers':[],'client':('127.0.0.1',1)}))
    assert payload['enabled'] is False and cookie is None
    assert db.query_one('SELECT name FROM api_keys WHERE id=?',(made['id'],))[0]=='legacy unchanged'
    with store.transaction() as conn:
        assert conn.execute("SELECT 1 FROM portal_settings WHERE key='registration_secret'").fetchone() is None


def test_existing_limit_rows_remain_usable_at_capacity_and_expiry_frees_slots(state,monkeypatch):
    from fastapi import HTTPException
    from server import portalregistration as registration,portalstore as store
    monkeypatch.setattr(registration,'MAX_RATE_ROWS',2)
    signing_secret=registration.secret()
    limits=[('synthetic-one',60,3),('synthetic-two',60,3)]
    registration.take_limits(signing_secret,limits)
    registration.take_limits(signing_secret,limits)
    with pytest.raises(HTTPException) as error:registration.take_limits(signing_secret,[('new-scope',60,3)])
    assert error.value.status_code==429
    with store.transaction() as conn:assert sorted(row[0] for row in conn.execute('SELECT count FROM portal_registration_limits'))==[2,2]
    now=store.now();monkeypatch.setattr(store,'now',lambda:now+61)
    registration.take_limits(signing_secret,[('new-scope',60,3)])
    with store.transaction() as conn:assert conn.execute('SELECT COUNT(*) FROM portal_registration_limits').fetchone()[0]==1


def test_full_application_registration_login_and_single_default_key(web):
    from server import db,main,portalstore as store
    _,admin=web
    # Deliberately do not run application lifespan/background jobs; every DB,
    # credential and upstream endpoint remains the isolated fixture's state.
    client=TestClient(main.app)
    try:
        assert client.get('/portal/').status_code==200
        assert client.get('/login/').status_code==200
        made=client.post('/api/portal-admin/invitations',headers=admin,json={})
        assert made.status_code==200,made.text
        code=made.json()['items'][0]['code']
        assert client.get('/api/portal/registration').status_code==200
        registered=signup(client,code)
        assert registered.status_code==201,registered.text
        login=client.post('/api/portal/login',headers={'origin':'http://testserver'},json={'username':'alice','password':PASSWORD})
        assert login.status_code==200,login.text
        created=client.post('/api/portal/keys',headers={'origin':'http://testserver'},json={})
        assert created.status_code==200,created.text
        existing=client.post('/api/portal/keys',headers={'origin':'http://testserver'},json={})
        assert existing.status_code==200 and existing.json()['id']==created.json()['id']
        assert existing.json()['existing'] and existing.json()['key'] is None
        listing=client.get('/api/portal/keys')
        assert 'no-store' in {value.strip().lower() for value in listing.headers['cache-control'].split(',')}
        assert listing.json()['single_key'] and len(listing.json()['items'])==1
        assert listing.json()['items'][0]['label']=='alice' and not listing.json()['can_initialize']
        assert db.query_one('SELECT COUNT(*) FROM api_keys')[0]==1
        with store.transaction() as conn:
            assert conn.execute('SELECT COUNT(*) FROM portal_grants').fetchone()[0]==0
            assert conn.execute('SELECT COUNT(*) FROM portal_bonus_grants').fetchone()[0]==0
    finally:client.close()


def test_full_app_body_guard_limits_registration_before_route_or_large_buffer():
    async def case():
        from server import main
        from server.bodyguard import APIBodyGuard
        middleware=next(item for item in main.app.user_middleware if item.cls is APIBodyGuard)
        for path in ('/api/portal/register','/api/portal/register/'):
            reached=[];sent=[]
            async def inner(*args):reached.append(True)
            async def receive():return {'type':'http.request','body':b'x'*8193,'more_body':False}
            async def send(message):sent.append(message)
            guard=APIBodyGuard(inner,**middleware.kwargs)
            await guard({'type':'http','path':path,'method':'POST','headers':[]},receive,send)
            assert sent[0]['status']==413 and not reached and guard.active==0
            assert b'8 KiB' in sent[1]['body']

    asyncio.run(case())


def test_registration_body_deadline_covers_outer_guard_and_releases_slot():
    async def case():
        from server import main
        from server.bodyguard import APIBodyGuard
        middleware=next(item for item in main.app.user_middleware if item.cls is APIBodyGuard)
        assert middleware.kwargs['path_timeouts']['/api/portal/register']==5
        sent=[];reached=[]
        async def inner(*args):reached.append(True)
        async def receive():await asyncio.Future()
        async def send(message):sent.append(message)
        options=dict(middleware.kwargs,path_timeouts={'/api/portal/register':.01})
        guard=APIBodyGuard(inner,**options)
        await guard({'type':'http','path':'/api/portal/register/','method':'POST','headers':[]},receive,send)
        assert sent[0]['status']==408 and not reached and guard.active==0

    asyncio.run(case())


def test_quarantined_overview_filters_expired_credit_without_changing_evidence(state):
    from server import portalstore as store
    user=make_user();stamp=store.now()
    with store.transaction() as conn:
        conn.execute('INSERT INTO portal_settings VALUES(?,?)',('restore_quarantine','true'))
        for identifier,expiry in [('expired',stamp-1),('valid',stamp+3600)]:
            conn.execute('INSERT INTO portal_bonus_grants VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier,user['id'],1000000,1000000,stamp-86400,expiry,'synthetic','test only',1))
        before=[tuple(row) for row in conn.execute('SELECT * FROM portal_bonus_grants ORDER BY id')]
        event_count=conn.execute('SELECT COUNT(*) FROM portal_events').fetchone()[0]
    result=store.overview(user['id'])
    assert result['restore_quarantined'] and not result['dispatch_enabled']
    assert result['balance_units']==1000000 and len(result['grants'])==1
    with store.transaction() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM portal_bonus_grants ORDER BY id')]==before
        assert conn.execute('SELECT COUNT(*) FROM portal_events').fetchone()[0]==event_count
