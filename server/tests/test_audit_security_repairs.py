"""Private HTTP regressions for the seven confirmed control-plane findings."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.tests.audit_fixtures import state


@pytest.fixture
def control(state,monkeypatch):
    from server import config,security,tokensvc
    from server.main import app
    monkeypatch.setattr(config,'AUTH_DIR',state/'auths')
    monkeypatch.setattr(config,'UPSTREAM_CONFIG',state/'upstream.json')
    config.AUTH_DIR.mkdir()
    config.UPSTREAM_CONFIG.write_text('{}')
    for name,value in (('_fail',{}),('_user_fail',{}),('_token_fail',{}),('_FAILS_LOADED',True)):
        monkeypatch.setattr(security,name,value)
    security.save_users({'role_policy_version':2,'secret':'synthetic-session-secret-audit',
       'users':[{'username':'admin','role':'admin','pwd_hash':security.make_hash('SyntheticRoot!2026'),'sv':1},
                {'username':'limited','role':'account_admin','pwd_hash':security.make_hash('SyntheticLimited!2026'),'sv':1}]})
    credentials={name:security.issue_token(name,role) for name,role in [('admin','admin'),('limited','account_admin')]}
    credentials['readonly']=tokensvc.create_token('synthetic-readonly','readonly')['token']
    with TestClient(app) as client:
        yield SimpleNamespace(client=client,credentials=credentials,app=app,root=state)


def cookie(control,role='admin'):
    from server import config
    return {'cookie':config.COOKIE_NAME+'='+control.credentials[role]}


@pytest.mark.parametrize('identity',['limited','readonly'])
def test_redis_url_credentials_are_redacted_in_actual_read_api(control,identity):
    from server import config
    from server.services import wb2api
    raw='rediss://default:synthetic-private-password@redis.example.invalid:6380/0'
    config.UPSTREAM_CONFIG.write_text(json.dumps({'upstash':{'url':raw,'token':'synthetic-rest-secret'}}))
    headers=cookie(control,identity) if identity=='limited' else {'authorization':'Bearer '+control.credentials[identity]}
    response=control.client.get('/api/settings/upstream',headers=headers)
    assert response.status_code==200,response.text
    assert 'synthetic-private-password' not in response.text
    assert 'synthetic-rest-secret' not in response.text
    assert response.json()['upstash']['has_url_credentials']
    masked=response.json()['upstash']['url']
    wb2api.save_upstream_config({'upstash':{'url':masked}})
    assert json.loads(config.UPSTREAM_CONFIG.read_text())['upstash']['url']==raw
    with pytest.raises(ValueError):
        wb2api.save_upstream_config({'upstash':{'url':masked.replace('redis.example.invalid','other.example.invalid')}})


def test_token_success_cannot_unlock_password_login(control):
    from server import security
    for _ in range(security.MAX_FAILS):security.record_fail('testclient','admin')
    body={'username':'admin','password':'synthetic-wrong-password'}
    assert control.client.post('/api/login',json=body).status_code==429
    for _ in range(2):
        assert control.client.get('/api/me',headers={'authorization':'Bearer '+control.credentials['readonly']}).status_code==200
        assert control.client.post('/api/login',json=body).status_code==429
        assert security._fail['testclient'][0]>=security.MAX_FAILS


def test_bad_token_failures_cannot_lock_password_login_and_survive_reload(control):
    from server import security,tokensvc
    for _ in range(security.MAX_FAILS):
        assert control.client.get('/api/me',headers={'authorization':'Bearer '+tokensvc.TOKEN_PREFIX+'synthetic-invalid'}).status_code==401
    assert security.token_login_blocked('testclient')
    assert not security.login_blocked('testclient','admin')
    security._token_fail.clear();security._FAILS_LOADED=False
    assert security.token_login_blocked('testclient')
    assert not security.login_blocked('testclient','admin')
    assert control.client.post('/api/login',json={'username':'admin','password':'synthetic-wrong-password'}).status_code==401
    assert control.client.get('/api/me',headers={'authorization':'Bearer '+control.credentials['readonly']}).status_code==429
    security.clear_fail('testclient','admin')
    assert security.token_login_blocked('testclient')


def test_new_auth_files_use_private_shared_group_mode(tmp_path,monkeypatch):
    from server.services import tencent
    target=tmp_path/'workbuddy-synthetic.json'
    requested=[];real_chmod=tencent.os.chmod
    def capture(path,mode):
        requested.append(mode);return real_chmod(path,mode)
    monkeypatch.setattr(tencent.os,'chmod',capture)
    tencent._atomic_write_json(target,{'auth':{'access_token':'synthetic'}})
    assert requested[-1]==0o640
    assert not requested[-1]&0o007
    if os.name!='nt':assert target.stat().st_mode&0o777==0o640


def test_new_auth_directory_is_private_and_existing_mode_is_preserved(tmp_path,monkeypatch):
    from server.services import tencent
    target=tmp_path/'new-auths'
    requested=[];real=tencent.os.chmod
    def capture(path,mode):requested.append(mode);return real(path,mode)
    monkeypatch.setattr(tencent.os,'chmod',capture)
    tencent._ensure_auth_directory(target)
    assert requested==[0o750]
    if os.name!='nt':assert target.stat().st_mode&0o777==0o750
    requested.clear();tencent._ensure_auth_directory(target)
    assert requested==[]


@pytest.fixture
def qtg(control,monkeypatch):
    from server.qtgcore.routers import compat
    from server.qtgcore.models import Account
    from server.qtgcore.service import Aggregate
    aggregate=Aggregate()
    aggregate.qoder_accounts=[Account(platform='qoder',uid='synthetic',name='Synthetic',realm='cn',credits_remain=100)]
    calls=[]
    async def build(_runtime,force=False):calls.append(force);return aggregate
    monkeypatch.setattr(compat,'build_aggregate',build)
    monkeypatch.setattr(control.app.state,'rt',SimpleNamespace(),raising=False)
    return calls


def test_qtg_export_has_a_real_manifest_contract_and_no_store(control,qtg):
    response=control.client.get('/api/accounts/export?platform=qoder',headers=cookie(control))
    assert response.status_code==200,response.text
    assert isinstance(response.json(),list) and response.json()[0]['uid']=='synthetic'
    assert 'filename="accounts.json"' in response.headers['content-disposition']
    assert 'no-store' in response.headers['cache-control']


def test_internal_qtg_path_also_never_caches(control,qtg):
    response=control.client.get('/__qtg/api/accounts/export',headers=cookie(control))
    assert 'no-store' in response.headers['cache-control']


def test_account_admin_cannot_force_refresh_through_post_alias(control,qtg):
    response=control.client.post('/api/accounts/qoder:synthetic/credits?platform=qoder',headers=cookie(control,'limited'))
    assert response.status_code==403,response.text
    assert qtg==[]


def test_qtg_cached_get_never_performs_force_refresh(control,qtg):
    for suffix in ('','&force=false'):
        result=control.client.get('/api/accounts/qoder:synthetic/credits?platform=qoder'+suffix,headers=cookie(control,'limited'))
        assert result.status_code==200,result.text
        assert result.json()['cached'] is True
        assert 'no-store' in result.headers['cache-control']
    assert qtg==[False,False]
    denied=control.client.get('/api/accounts/qoder:synthetic/credits?platform=qoder&force=true',headers=cookie(control,'limited'))
    assert denied.status_code==403
    assert qtg==[False,False]


def test_unsupported_group_ownership_never_silently_rewrites_auth(tmp_path,monkeypatch):
    from server.services import tencent
    target=tmp_path/'synthetic-auth.json'
    target.write_text('{"synthetic":"old"}')
    monkeypatch.setenv('WB_AUTH_SHARED_GID','123')
    monkeypatch.delattr(tencent.os,'chown',raising=False)
    with pytest.raises(ValueError,match='POSIX ownership'):
        tencent._atomic_write_json(target,{'synthetic':'new'})
    assert target.read_text()=='{"synthetic":"old"}'
    assert list(tmp_path.iterdir())==[target]
