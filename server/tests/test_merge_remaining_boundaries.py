"""Synthetic regressions for the remaining audit gaps, without real providers."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import httpcore
import httpx
import pytest


def load_script(name):
    path=Path(__file__).resolve().parents[2]/'deploy'/name
    spec=importlib.util.spec_from_file_location('remaining_'+path.stem,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    from server import config, db
    with db._lock:
        db._close_connection_locked()
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'manager.db')
    monkeypatch.setattr(config,'AUTH_DIR',tmp_path/'auths')
    db.connect()
    yield db
    with db._lock:
        db._close_connection_locked()


def test_realm_filter_precedes_page_and_counts_more_than_two_thousand(local_db):
    from server import historylogs
    conn=local_db.connect()
    with conn:
        conn.executemany('INSERT INTO task_logs(ts,uid,kind,level,credits,message,dedup_key) VALUES(?,?,?,?,?,?,?)',
            [(10000+i,'global-uid','checkin','ok',1,'fixture',f'global-{i}') for i in range(2500)] +
            [(i,'cn-uid','checkin','ok',1,'fixture',f'cn-{i}') for i in range(2100)])
    mapping={'global-uid':'global','cn-uid':'cn'}
    page=historylogs.task_page(mapping=mapping,realm='cn',limit=5,offset=2000,uid=None,kind=None,days=None)
    assert len(page['logs'])==5 and page['total']==2100 and page['stats']['total']==2100
    assert {r['uid'] for r in page['logs']}=={'cn-uid'}
    merged=historylogs.checkin_page(mapping=mapping,realm='cn',limit=5,offset=2000,uid=None,days=None)
    assert merged['total']==2100 and len(merged['items'])==5


def test_unknown_realm_and_ambiguous_prefix_are_not_guessed(local_db):
    from server import historylogs
    for index,uid in enumerate(('abcdefgh','deleted','abcdefgh-cn','abcdefgh-global','')):
        local_db.execute('INSERT INTO task_logs(ts,uid,kind,level,credits,message,dedup_key) VALUES(?,?,?,?,?,?,?)',
                         (1,uid,'checkin','ok',0,'fixture',str(index)))
    mapping={'abcdefgh-cn':'cn','abcdefgh-global':'global'}
    unknown=historylogs.task_page(mapping=mapping,realm='unknown',limit=10,offset=0,uid=None,kind=None,days=None)
    assert {r['uid'] for r in unknown['logs']}=={'abcdefgh','deleted'}
    cn=historylogs.task_page(mapping=mapping,realm='cn',limit=10,offset=0,uid=None,kind=None,days=None)
    assert {r['uid'] for r in cn['logs']}=={'abcdefgh-cn',''}


def test_half_open_allows_one_owned_probe_and_cancel_reopens(monkeypatch):
    from server.qtgcore.adapters.base import Breaker
    async def run():
        breaker=Breaker(1,1)
        breaker.failure()
        breaker.opened_at-=2
        release=asyncio.Event()
        entered=[]
        async def request():
            if not breaker.allow():return
            entered.append(True)
            try:await release.wait()
            finally:breaker.cancel_probe()
        tasks=[asyncio.create_task(request()) for _ in range(30)]
        await asyncio.sleep(0)
        assert len(entered)==1
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        assert breaker._probe_owner is None and not breaker.allow()
        breaker.opened_at-=2
        assert breaker.allow()
        breaker.success()
        assert breaker.allow() and breaker.failures==0
    asyncio.run(run())


def test_dns_failure_and_mixed_answers_fail_before_connect(monkeypatch):
    from server.services import pinnedprobe
    async def run():
        loop=asyncio.get_running_loop()
        async def failed(*a,**kw):raise socket.gaierror('synthetic DNS failure')
        monkeypatch.setattr(loop,'getaddrinfo',failed)
        with pytest.raises(ValueError,match='DNS'):
            await pinnedprobe.resolve_public('fixture.example',443)
        async def mixed(*a,**kw):return [(None,None,None,None,('8.8.8.8',443)),(None,None,None,None,('127.0.0.1',443))]
        monkeypatch.setattr(loop,'getaddrinfo',mixed)
        with pytest.raises(ValueError,match='非公网'):
            await pinnedprobe.resolve_public('fixture.example',443)
    asyncio.run(run())


def test_pinned_tcp_receives_only_validated_ip_and_retains_original_http_host():
    from server.services import pinnedprobe
    calls=[]
    class Backend:
        async def connect_tcp(self,host,port,**kw):calls.append((host,port));return 'synthetic-stream'
    async def run():
        backend=pinnedprobe.PinnedBackend('fixture.example',443,('8.8.8.8',),Backend())
        assert await backend.connect_tcp('fixture.example',443)=='synthetic-stream'
        with pytest.raises(httpcore.ConnectError):await backend.connect_tcp('other.example',443)
        assert calls==[('8.8.8.8',443)]
    asyncio.run(run())


def test_failed_junit_does_not_replace_freeze(tmp_path):
    freeze=load_script('freeze_from_junit.py')
    junit=tmp_path/'result.xml';target=tmp_path/'frozen.json'
    target.write_bytes(b'existing freeze')
    for body in ('<testsuite><testcase name="bad"><failure/></testcase></testsuite>','<testsuite/>'):
        junit.write_text(body)
        assert freeze.main([str(junit),str(target)])==3
        assert target.read_bytes()==b'existing freeze'
    junit.write_text('<testsuite><testcase name="valid"/></testsuite>')
    assert freeze.main([str(junit),str(target)])==0
    assert json.loads(target.read_text())['expected_collected']==1


def test_prompt_probe_cannot_reuse_credential_on_changed_origin(tmp_path,monkeypatch):
    probe=load_script('probe_prompt_trigger.py')
    profile=tmp_path/'profile';profile.write_text('baseURL: https://fixture.example/v1\napiKey: synthetic-key\n')
    monkeypatch.setattr(probe,'PROFILE',profile)
    with pytest.raises(ValueError,match='覆盖地址'):
        probe.resolve_credentials('https://different.example/v1',None,None)
    monkeypatch.delenv('NO_SUCH_PROBE_KEY',raising=False)
    with pytest.raises(ValueError,match='为空'):
        probe.resolve_credentials('https://different.example/v1',None,'NO_SUCH_PROBE_KEY')
    assert probe.resolve_credentials('https://different.example/v1','explicit-synthetic-key',None)[1]=='explicit-synthetic-key'


def test_retired_asset_tool_has_no_deletion_side_effect(tmp_path,monkeypatch):
    tool=load_script('asset_lifecycle.py')
    sentinel=tmp_path/'keep';sentinel.write_bytes(b'must survive')
    report={'confirm_token':'synthetic','active_release':None,'groups':[{'name':'data','items':[
        {'action':'prune','path':str(sentinel),'size':12}]}]}
    assert tool.apply_prune(report,'synthetic')==3
    assert sentinel.read_bytes()==b'must survive'


def test_pool_empty_missing_and_unsafe_builds_never_pass(tmp_path):
    pool=load_script('web_pool_manifest.py')
    (tmp_path/'static').mkdir()
    assert pool.main(['--pool',str(tmp_path),'--verify'])==2
    assert pool.main(['--pool',str(tmp_path),'--verify','--build','missing'])==2
    for name in ('../outside','/outside','a/b','a\\b','..'):
        with pytest.raises(ValueError):pool.build_dir(tmp_path,name)


@pytest.mark.parametrize('body,expected',[
    ('<html>gateway login</html>',False),('{}',False),
    ('{"error":{"message":"provider unavailable"}}',False),
    ('{"choices":[]}',False),
    ('{"choices":[{"finish_reason":"stop","message":{"content":"pong"}}]}',True),
    ('{"choices":[{"finish_reason":"length","message":{"reasoning_content":"thinking"}}]}',True),
    ('{"choices":[{"finish_reason":"stop","message":{"content":""}}]}',False),
])
def test_prompt_probe_requires_model_evidence_not_just_http_success(monkeypatch,body,expected):
    import io
    probe=load_script('probe_prompt_trigger.py')
    class Response(io.BytesIO):status=200
    monkeypatch.setattr(probe,'_open_request',lambda *a,**k:Response(body.encode()))
    result=probe.probe('https://fixture.example/v1','synthetic-only','fixture','fixture')
    assert result['ok'] is expected


def test_prompt_probe_does_not_redirect_credentials_and_redacts_echoed_key(monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    probe=load_script('probe_prompt_trigger.py')
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            seen.append(('first',self.headers.get('Authorization')))
            self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/must-not-follow')
            self.send_header('Content-Length','0');self.end_headers()
        def do_GET(self):
            seen.append(('redirected',self.headers.get('Authorization')))
            self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        result=probe.probe(f'http://127.0.0.1:{server.server_port}/v1','synthetic-secret-only','fixture','fixture')
        assert result['status']==302 and not result['ok']
        assert seen==[('first','Bearer synthetic-secret-only')]
    finally:server.shutdown();server.server_close();thread.join(5)
    import io
    class Response(io.BytesIO):status=200
    monkeypatch.setattr(probe,'_open_request',lambda *a,**k:Response(b'{"error":"synthetic-secret-only"}'))
    result=probe.probe('http://127.0.0.1/v1','synthetic-secret-only','fixture','fixture')
    assert not result['ok'] and 'synthetic-secret-only' not in result['body']
