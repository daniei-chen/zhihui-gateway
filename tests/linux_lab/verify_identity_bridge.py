"""Real TLS/Nginx/auth_request/Manager/QTG identity checks. LOCAL VM ONLY."""
from __future__ import annotations

import datetime as dt
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_ready(client, port, child, health_path):
    # 90s 在 QEMU TCG 上不够：Manager 启动时做弱口令扫描，26 条候选 × 单次
    # PBKDF2-260k ≈ 2.2s（TCG 无 AES 加速）≈ 57s，加上导入与其它 startup 工作
    # 正好卡在 90s 边界，表现为「有时能起、有时超时」。这不是产品缺陷，
    # 是仿真环境的 CPU 特性，因此只放宽测试侧等待，不改产品代码。
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError('Own fixture server exited before readiness')
        try:
            if client.get(f'http://127.0.0.1:{port}{health_path}').status_code == 200:
                return
        except Exception:
            pass
        time.sleep(.2)
    raise AssertionError('Own fixture server readiness timed out')


def main(browser_lab=False):
    lab = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent:
        raise RuntimeError('Restricted to the disposable local Linux VM')
    qa = json.loads((lab / 'current-qa.json').read_text())
    source = Path(qa['workspace']).resolve()
    assert source.is_relative_to(lab / 'snapshots') and not qa['production_connected']
    root = lab / 'identity-runs' / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    root.chmod(0o755)
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(source / 'tests'), str(source / 'manager'), str(source / 'qtg/app')]
    from fake_provider.server import FakeProvider
    from zhihui_harness.isolation import IsolatedEnv
    provider = FakeProvider().start()
    iso = IsolatedEnv.create(source, keep=True, base_dir=root)
    manager_port, qtg_port = free_port(), free_port()
    shared = root / 'identity-secret'
    shared.write_text(secrets.token_urlsafe(48))
    shared.chmod(0o600)
    qtg_shared = root / 'qtg-identity-secret'
    qtg_shared.write_text(shared.read_text())
    qtg_shared.chmod(0o600)
    internal_file = root / 'qtg-internal-service-token'
    internal_file.write_text(secrets.token_urlsafe(48))
    internal_file.chmod(0o600)
    env = iso.build_env(provider, extra={
        'WB_QTG_IDENTITY_SECRET_FILE': str(shared), 'WB2API_BASE': provider.base_url,
        'WB2API_KEY': 'synthetic-upstream', 'WB2API_MODE': 'native',
        'WB_STATIC_DIR': str(root / 'empty-static'), 'WB_DRAIN_TIMEOUT_SEC': '30'})
    os.environ.update(env)
    from server import budgetowner, config, db, security, tokensvc
    config.ensure_dirs()
    security.save_users({'secret': 'qa-identity-' * 8, 'api_keys': [], 'users': [
        {'username': name, 'role': role, 'pwd_hash': security.make_hash('synthetic-password')}
        for name, role in [('gateway-admin', 'admin'), ('只读用户', 'viewer')]]})
    db.connect()
    # K26 读写令牌分离：QTG 的只读读取走 manager_read_token_file（scope=read），
    # 写操作仍用 manager_service_token_file（scope=keys）。缺任一个 QTG 会以
    # ConfigMissing(503) 失败，浏览器 fixture 也就起不来。
    tokens = {scope: tokensvc.create_token('edge-' + scope, scope)['token']
              for scope in ('keys', 'readonly', 'admin', 'read')}
    manager_token_file = root / 'manager-service-token'
    manager_token_file.write_text(tokens['keys'])
    manager_token_file.chmod(0o600)
    manager_read_token_file = root / 'manager-read-token'
    manager_read_token_file.write_text(tokens['read'])
    manager_read_token_file.chmod(0o600)
    budgetowner.close()
    with db._lock:
        db._close_connection_locked()

    admin_file = root / 'qtg-admin-token'
    admin_file.write_text(secrets.token_urlsafe(48))
    admin_file.chmod(0o600)
    settings = dict(data_dir=str(root / 'qtg-data'), static_dir=str(root / 'qtg-static'),
        admin_token_file=str(admin_file), session_secret_file=str(root / 'qtg-session-secret'),
        internal_service_token_file=str(internal_file), identity_signing_key_file=str(qtg_shared),
        extra_password_files='',
        trusted_edge_peers='127.0.0.1,::1', read_requires_auth=True,
        authz_enforce=True, human_session_required=True, csrf_strict_origin=True,
        checkin_scheduler_enabled=False,
        qoder_base=provider.base_url, trae_base=provider.base_url, wb_engine='off',
        wb_panel_base=f'http://127.0.0.1:{manager_port}',
        manager_service_token_file=str(manager_token_file),
        manager_read_token_file=str(manager_read_token_file), limits_file=str(root / 'limits.json'),
        backup_glob_qoder=str(root/'backups/qoder/*'), backup_glob_trae=str(root/'backups/trae/*'),
        notify_config_file=str(root / 'notify.json'), proxy_enabled=False)
    from qtg.security import CredentialStore
    store = CredentialStore(admin_file, Path(settings['session_secret_file']))
    stale_qtg_cookie = store.issue_session(3600, sub='old-local-admin', role='admin')
    manager_launcher = root / 'manager.py'
    upstream_token = root/'upstream-token'
    upstream_token.write_text('synthetic-upstream');upstream_token.chmod(0o600)
    def guard_prefix(label):
        return ('sys.path.insert(0,'+repr(str(source/'tests'))+')\n'
            'from pathlib import Path\nimport time,atexit,json\n'
            'from zhihui_harness import pytest_plugin as guard\n'
            'guard._STATE.update(iso_root='+repr(str(root))+',started_at=time.time(),installed=True)\n'
            'guard._install_net_guard("block")\nguard._install_path_guard(Path('+repr(str(root))+'))\n'
            'atexit.register(lambda:Path('+repr(str(root/(label+'-guard.json')))+').write_text(json.dumps(guard._STATE,default=str)))\n')
    manager_launcher.write_text('import sys\nsys.dont_write_bytecode=True\n'
        + guard_prefix('manager')
        + 'sys.path.insert(0,' + repr(str(source / 'manager')) + ')\n'
        + 'from server.routers import gateway\ngateway._THREEWAY_SPEC='
        + repr({name:(provider.base_url,str(upstream_token)) for name in ('qoder','trae')}) + '\n'
        # Capture an actual blocked stack, without changing service startup
        # behavior or weakening readiness. SIGUSR1 is reserved for drain.
        + 'import faulthandler\nfrom contextlib import asynccontextmanager\n'
        + 'from server.main import app\n_original_lifespan=app.router.lifespan_context\n'
        + '@asynccontextmanager\nasync def _diagnostic_lifespan(application):\n'
        + ' faulthandler.dump_traceback_later(30,repeat=True)\n'
        + ' try:\n'
        + '  async with _original_lifespan(application):\n'
        + '   faulthandler.cancel_dump_traceback_later()\n'
        + '   yield\n'
        + ' finally:\n'
        + '  faulthandler.cancel_dump_traceback_later()\n'
        + 'app.router.lifespan_context=_diagnostic_lifespan\n'
        + 'from server.serve import main\nmain('
        + repr(['--host','127.0.0.1','--port',str(manager_port),'--timeout-graceful-shutdown','30']) + ')\n')
    qtg_launcher = root / 'qtg.py'
    qtg_launcher.write_text('import sys\nsys.dont_write_bytecode=True\n'
        + guard_prefix('qtg')
        + 'sys.path.insert(0,' + repr(str(source / 'qtg/app')) + ')\n'
        + 'import uvicorn\nfrom qtg.config import Settings\nfrom qtg.main import create_app\n'
        + 'uvicorn.run(create_app(Settings(**' + repr(settings) + ')),host="127.0.0.1",port='
        + str(qtg_port) + ',proxy_headers=True)\n')
    cert, key = root / 'tls.crt', root / 'tls.key'
    subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256',
        '-nodes','-days','1','-subj','/CN=zhihui.lab','-addext',
        'subjectAltName=DNS:zhihui.lab,IP:127.0.0.1','-keyout',str(key),'-out',str(cert)],
        check=True,capture_output=True)
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',443)) == 0:
            raise RuntimeError('Port 443 already used; never stop an unrelated listener')
    bridge = (source / 'ops/nginx/qtg-identity-bridge.conf').read_text().replace(
        '127.0.0.1:7864', f'127.0.0.1:{manager_port}').replace('127.0.0.1:8795',f'127.0.0.1:{qtg_port}')
    bridge_file = root / 'bridge.conf'
    bridge_file.write_text(bridge)
    maps=(source/'ops/nginx/three-platform-routing.http.conf').read_text().replace(
        '127.0.0.1:7864',f'127.0.0.1:{manager_port}').replace('127.0.0.1:8795',f'127.0.0.1:{qtg_port}')
    maps_file=root/'routing.conf';maps_file.write_text(maps)
    public=(source/'ops/nginx/public-api.server.conf').read_text().replace(
        '127.0.0.1:7864',f'127.0.0.1:{manager_port}')
    public_file=root/'public.conf';public_file.write_text(public)
    # K37：静态缓存策略由两个片段承担（http 上下文的 map + server 上下文的唯一 add_header）。
    # 注意 N37 坑：location 里一旦出现 add_header，server 级 add_header 全部不继承，
    # 所以下面各 location 一律不再写 add_header。
    cache_map_file = root / 'static-cache-map.http.conf'
    cache_map_file.write_text((source / 'ops/nginx/static-cache-map.http.conf').read_text())
    cache_server_file = root / 'static-cache.server.conf'
    cache_server_file.write_text((source / 'ops/nginx/static-cache.server.conf').read_text())
    nginx_conf = root / 'nginx.conf'
    nginx_conf.write_text(f'''user gateway-lab;
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 128; }}
http {{ access_log {root}/access.log;
 include /etc/nginx/mime.types;
 default_type application/octet-stream;
 include {maps_file};
 include {cache_map_file};
 server {{ listen {"0.0.0.0" if browser_lab else "127.0.0.1"}:443 ssl; server_name zhihui.lab;
  ssl_certificate {cert}; ssl_certificate_key {key};
  error_page 404 /404.html;
  # K37：唯一的缓存 add_header（变量来自 http 上下文的 map）；server 级声明才能与安全头共存
  include {cache_server_file};
  # 安全头：fixture 等价于生产边缘；与缓存头同时存在才能证明 N37 坑没有复发
  add_header X-Content-Type-Options nosniff always;
  add_header X-Frame-Options DENY always;
  add_header Referrer-Policy no-referrer always;
  add_header Content-Security-Policy-Report-Only "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'self'; img-src 'self' data: blob:; font-src 'self' data:" always;
  include {public_file};
  location /_next/ {{ root {source}/src/three-platform/web/out; }}
  location ~ ^/(?!api/|_next/)(.*)\\.txt$ {{
   root {source}/src/three-platform/web/out;
   try_files /$1/index.txt $uri =404;
   default_type text/x-component;
  }}
  location / {{ root {source}/src/three-platform/web/out;
   try_files $uri $uri/index.html $uri.html =404;
  }}
  # Fixture only: simulate auth_request incorrectly returning 200 with no claims.
  location = /__zh_empty_identity {{ internal;
   proxy_pass http://127.0.0.1:{manager_port}/api/me;
   proxy_set_header Cookie $http_cookie;
  }}
  location = /api/test-empty-auth-success {{
   auth_request /__zh_empty_identity;
   auth_request_set $zh_empty $upstream_http_x_qtg_identity;
   proxy_set_header X-QTG-Identity $zh_empty;
   proxy_set_header X-QTG-Identity-Required "1";
   proxy_pass http://127.0.0.1:{qtg_port};
  }}
  include {bridge_file};
 }}
}}
''')
    nginx = ['sudo','-n','/usr/sbin/nginx','-p',str(root),'-c',str(nginx_conf)]
    result = {'root':str(root),'source_sha256':qa['source_sha256'],
        'production_connected':False,'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'startup':[],'checks':[]}
    children, logs = [], []
    import httpx
    local = httpx.Client(trust_env=False,timeout=10)
    edge = httpx.Client(base_url='https://127.0.0.1',verify=str(cert),trust_env=False,
                       timeout=15,headers={'Host':'zhihui.lab'})
    def request(method, path, expected, *, headers=None, **kwargs):
        edge.cookies.clear()  # Never accidentally test a token using a retained admin jar.
        response = edge.request(method,path,headers={'X-QTG-Platform':'qoder',**(headers or {})},**kwargs)
        assert response.status_code == expected, (method,path,response.status_code,response.text[:800])
        assert 'x-qtg-identity' not in response.headers
        return response
    def check(name):
        result['checks'].append(name)
        print('PASS ' + name,flush=True)
    try:
        for launcher, port, health_path in ((manager_launcher,manager_port,'/api/healthz'),
                                          (qtg_launcher,qtg_port,'/healthz')):
            log = (root / (launcher.stem + '.log')).open('w')
            logs.append(log)
            child = subprocess.Popen([qa['python'],'-B',str(launcher)],cwd=source,env=env,
                                     stdout=log,stderr=subprocess.STDOUT)
            children.append(child)
            started = time.monotonic()
            startup = {'component':launcher.stem,'pid':child.pid,'port':port,
                       'launcher_sha256':hashlib.sha256(launcher.read_bytes()).hexdigest(),
                       'health_path':health_path,'ready':False}
            result['startup'].append(startup)
            try:
                wait_ready(local,port,child,health_path)
                startup['ready']=True
            except Exception as exc:
                startup['error']=type(exc).__name__+': '+str(exc)
                raise
            finally:
                startup['elapsed_s']=round(time.monotonic()-started,3)
                (root/'startup.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        subprocess.run([*nginx,'-t'],check=True,capture_output=True)
        subprocess.run(nginx,check=True,capture_output=True)
        check('nginx_candidate_syntax_and_real_servers')
        payload = {'sub':'forged-human','role':'admin','scopes':['*'],
            'auth_method':'edge_session','aud':'qtg','exp':int(time.time()) + 60}
        body = base64.urlsafe_b64encode(json.dumps(payload,sort_keys=True,
            separators=(',',':')).encode()).decode().rstrip('=')
        fake_key = hashlib.sha256(b'qtg-identity-v1\0' + internal_file.read_bytes()).digest()
        signature = base64.urlsafe_b64encode(hmac.new(fake_key,body.encode(),hashlib.sha256).digest()).decode().rstrip('=')
        direct_identity_url = f'http://127.0.0.1:{qtg_port}/api/v1/authz/identity'
        forged = local.get(direct_identity_url,headers={'X-QTG-Identity':f'v1.{body}.{signature}'})
        assert forged.status_code==401 and forged.json()['code']=='identity_tampered'
        plaintext = local.get(direct_identity_url,headers={
            'X-QTG-Internal-Auth':internal_file.read_text(),'X-QTG-Sub':'forged-human',
            'X-QTG-Role':'admin','X-QTG-Auth-Method':'edge_session',
            'X-QTG-Scopes':'*','X-QTG-Aud':'qtg','X-QTG-Exp':str(int(time.time())+60)})
        assert plaintext.status_code==401 and plaintext.json()['code']=='identity_signature_required'
        machine = local.get(direct_identity_url,headers={'X-QTG-Internal-Auth':internal_file.read_text()})
        assert machine.status_code==200 and machine.json()['identity']['role']=='internal_service'
        check('real_machine_credential_cannot_sign_or_claim_a_human_identity')
        cookies = {}
        for name in ('gateway-admin','只读用户'):
            response = request('POST','/api/login',200,json={'username':name,'password':'synthetic-password'})
            cookies[name] = 'wb_session=' + response.cookies['wb_session']
        admin = {'Cookie':cookies['gateway-admin']}
        viewer = {'Cookie':cookies['只读用户']}
        identity_path = '/api/v1/authz/identity'
        request('GET',identity_path,401)
        request('GET','/__zh_identity',404)
        request('GET','/api/authz/probe',404,headers=admin)
        check('anonymous_and_direct_subrequest_denied')
        request('GET','/api/claim/absent-synthetic-claim',410)
        request('GET','/api/healthz',200)
        check('public_claim_and_minimal_health_are_not_accidentally_auth_gated')
        response = request('GET',identity_path,200,headers=viewer)
        identity = response.json()['identity']
        assert (identity['sub'],identity['role'],identity['auth_method']) == ('只读用户','viewer','edge_session'), identity
        assert identity['scopes'] == ['read']
        request('GET','/api/keys',200,headers=viewer)
        check('real_viewer_read_reaches_qtg_and_manager_key_authority')
        for platform in ('workbuddy','qoder','trae'):
            endpoint='/api/system/configuration' if platform=='workbuddy' else '/api/v1/system/configuration'
            snapshot=request('GET',endpoint,200,headers={**admin,'X-QTG-Platform':platform}).json()
            assert snapshot['configuration']['service']==('manager' if platform=='workbuddy' else 'qtg'),snapshot
        check('real_three_platform_control_routing_and_protected_configuration_snapshots')
        cfg = security.load_users()
        old_time = int(time.time()) - 5 * 3600
        aged = security._sign(json.dumps({'username':'只读用户','role':'viewer',
            'sv':security.session_version(cfg,'只读用户'),'iat':old_time,'orig':old_time,
            'exp':old_time + config.SESSION_DAYS * 86400}),cfg['secret'])
        response = request('GET',identity_path,200,headers={'Cookie':'wb_session=' + aged})
        renewed = response.cookies['wb_session']
        assert security._unsign(renewed,cfg['secret'])['orig'] == old_time
        assert renewed != aged
        check('auth_request_sliding_session_cookie_reaches_browser_without_extending_origin')
        spoof = {'X-QTG-Identity':'v1.forged.signature','X-QTG-Internal-Auth':internal_file.read_text(),
                 'X-QTG-Role':'admin','X-User':'admin','X-Role':'admin',
                 'X-Forwarded-For':'203.0.113.10', 'Forwarded':'for=203.0.113.10'}
        response = request('GET',identity_path,200,headers={**viewer,**spoof})
        assert response.json()['identity']['role'] == 'viewer'
        request('GET',identity_path,401,headers=spoof)
        check('edge_overwrites_all_client_identity_and_service_headers')
        stale_viewer = {'Cookie':cookies['只读用户'] + '; qtg_session=' + stale_qtg_cookie}
        response = request('GET',identity_path,200,headers=stale_viewer)
        assert response.json()['identity']['sub'] == '只读用户'
        request('POST','/api/v1/actions/limits',403,json={'enabled':True},
                headers={**stale_viewer,'Origin':'https://zhihui.lab'})
        check('local_admin_cookie_cannot_elevate_manager_viewer')
        request('GET','/api/test-empty-auth-success',401,headers=stale_viewer)
        check('successful_empty_auth_response_cannot_fall_back_to_local_cookie')
        request('POST','/api/v1/actions/limits',403,json={'enabled':True},headers=admin)
        request('POST','/api/v1/actions/limits',403,json={'enabled':True},
                headers={**admin,'Origin':'https://evil.example'})
        request('POST','/api/v1/actions/limits',200,json={'enabled':True,'daily_token_limit':456},
                headers={**admin,'Origin':'https://zhihui.lab','X-Request-ID':'real-admin-write'})
        assert json.loads((root / 'limits.json').read_text())['daily_token_limit'] == 456
        check('csrf_and_successful_admin_write_readback')
        for scope in ('readonly','admin','keys'):
            headers = {'Authorization':'Bearer ' + tokens[scope]}
            identity = request('GET',identity_path,200,headers=headers).json()['identity']
            assert identity['auth_method'] == 'admin_token', identity
            assert identity['sub'].startswith('manager-token:')
            request('POST','/api/sessions/revoke',403,headers=headers,json={})
        check('all_management_token_classes_keep_nonhuman_authentication')
        keys_headers = {'Authorization':'Bearer ' + tokens['keys']}
        request('POST','/api/v1/actions/limits',403,headers=keys_headers,json={'enabled':False})
        request('GET','/api/v1/accounts',403,headers=keys_headers)
        response = request('POST','/api/keys',200,headers=keys_headers,
                           json={'name':'real-scoped-create','platforms':['qoder'],'quota':12345})
        key_id = response.json()['id']
        rows = request('GET','/api/keys',200,headers=keys_headers).json()
        row = next(row for row in rows if row['id'] == key_id)
        assert row['platforms'] == ['qoder'], row
        with sqlite3.connect(config.DB_PATH) as connection:
            row = connection.execute('SELECT platforms,quota FROM api_keys WHERE id=?',(key_id,)).fetchone()
            assert json.loads(row[0]) == ['qoder'] and row[1] == 12345, row
        check('keys_scope_real_create_and_authoritative_platform_quota_readback')
        for method,path in [('POST','/api/v1/actions/limits'),('GET','/api/v1/accounts')]:
            request('GET','/api/v1/authz/probe',403,headers=keys_headers,
                    params={'method':method,'path':path})
        check('probe_and_real_operation_scope_decisions_agree')
        cfg = security.load_users()
        cfg['users'][0]['role'] = 'viewer'
        security.save_users(cfg)
        response = request('GET',identity_path,200,headers=admin)
        assert response.json()['identity']['role'] == 'viewer'
        request('POST','/api/v1/actions/limits',403,json={'enabled':False},
                headers={**admin,'Origin':'https://zhihui.lab'})
        security.revoke_sessions('gateway-admin')
        request('GET',identity_path,401,headers={
            'Cookie':cookies['gateway-admin'] + '; qtg_session=' + stale_qtg_cookie})
        check('manager_role_change_and_session_revocation_apply_without_restart')
        audit = [json.loads(line) for line in (root / 'qtg-data/audit.jsonl').read_text().splitlines()]
        limits = [row for row in audit if row['action']=='limits' and row['result']=='ok']
        assert len(limits)==1 and limits[0]['actor']=='admin:gateway-admin', limits
        assert limits[0]['detail']['identity']['auth_method']=='edge_session', limits
        assert limits[0]['request_id']=='real-admin-write', limits
        key_audit = [row for row in audit if row['action']=='compat_key_create']
        assert len(key_audit)==1 and key_audit[0]['actor'].startswith('internal_service:manager-token:')
        blob = json.dumps(audit)
        for token in [*tokens.values(),shared.read_text(),internal_file.read_text(),stale_qtg_cookie]:
            assert token not in blob, 'Secret leaked into audit'
        check('qtg_success_and_denial_audit_actor_and_credential_redaction')
        assert provider.stats.snapshot()['total']==0, provider.stats.snapshot()
        check('authentication_and_control_checks_make_no_inference_provider_calls')
        rotated = secrets.token_urlsafe(48)
        shared.write_text(rotated)
        request('GET',identity_path,401,headers=viewer)
        qtg_shared.write_text(rotated)
        request('GET',identity_path,200,headers=viewer)
        check('mismatched_signing_key_denied_then_both_keys_rotated_without_restart')
        shared.unlink()
        request('GET',identity_path,500,headers=viewer)
        check('missing_bridge_secret_fails_closed_at_real_nginx')
        result['status'] = 'passed'
        result['fake_provider'] = provider.stats.snapshot()
        if browser_lab:
            # Preserve the authentication checkpoint before browser writes begin.
            (root/'auth-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            cfg=security.load_users()
            next(user for user in cfg['users'] if user['username']=='gateway-admin')['role']='admin'
            security.save_users(cfg)
            shared.write_text(rotated);shared.chmod(0o600)
            result['status']='ready_for_browser'
            ready={'root':str(root),'source_sha256':qa['source_sha256'],'production_connected':False,
                   'host_url':'https://127.0.0.1:18443','stop_file':str(root/'browser-stop'),
                   'manager_port':manager_port,'qtg_port':qtg_port,
                   'guards':'Python network and path guards; no OS-level subprocess network sandbox',
                   'note':'Synthetic accounts only; inference uses fake provider. Engine account/control behavior is not fully simulated.'}
            (root/'browser-ready.json').write_text(json.dumps(ready,indent=2))
            (lab/'browser-current.json').write_text(json.dumps(ready,indent=2))
            print('BROWSER_READY '+json.dumps(ready),flush=True)
            while not (root/'browser-stop').exists():time.sleep(1)
            result['status']='browser_fixture_stopped'
        return 0
    finally:
        if (root / 'nginx.pid').exists():
            subprocess.run([*nginx,'-s','quit'],check=True,capture_output=True,timeout=30)
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait(timeout=10)
                    result['forced_fixture_cleanup'] = True
        for log in logs:log.close()
        edge.close();local.close();provider.stop()
        result.setdefault('status','failed')
        result['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (root / 'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser-lab',action='store_true')
    raise SystemExit(main(parser.parse_args().browser_lab))
