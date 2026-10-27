"""Real Uvicorn + Nginx + TLS + three fake platform streams, local VM only."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for the declared local runtime condition')


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def main():
    lab = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent:
        raise RuntimeError('Restricted to the disposable local Linux VM')
    qa = json.loads((lab / 'current-qa.json').read_text())
    source = Path(qa['workspace']).resolve()
    assert source.is_relative_to(lab / 'snapshots') and not qa['production_connected']
    root = lab / 'drain-runs' / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    root.chmod(0o755)
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(source / 'tests'), str(source / 'manager')]
    from fake_provider.server import FakeProvider
    from zhihui_harness.isolation import IsolatedEnv
    provider = FakeProvider(chunk_delay=.3, chunk_count=80).start()
    iso = IsolatedEnv.create(source, keep=True, base_dir=root)
    port = free_port()
    env = iso.build_env(provider, extra={
        'WB2API_BASE': provider.base_url, 'WB2API_KEY': 'synthetic-upstream',
        'WB2API_MODE': 'native', 'WB_MANAGER_HOST': '127.0.0.1', 'WB_MANAGER_PORT': str(port),
        'WB_STATIC_DIR': str(source / 'src/three-platform/web/out'),
        'WB_UPSTREAM_TIMEOUT': '120', 'WB_TOTAL_TIMEOUT_S': '120',
        'WB_FIRST_BYTE_TIMEOUT_S': '60', 'WB_DRAIN_TIMEOUT_SEC': '45',
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'})
    os.environ.update(env)
    from server import budgetowner, config, db, keysvc, security, tokensvc
    config.ensure_dirs()
    security.save_users({'secret': 'qa-signing-' * 8, 'users': [], 'api_keys': []})
    db.connect()
    keys = {name: keysvc.create_key('drain-' + name, platforms=[name], quota=100000)
            for name in ('workbuddy', 'qoder', 'trae')}
    cancel_key = keysvc.create_key('intentional-cancel', platforms=['workbuddy'], quota=100000)
    admin = tokensvc.create_token('drain-observer', 'admin')['token']
    budgetowner.close()
    with db._lock:
        db._close_connection_locked()
    secret_file = root / 'upstream-token'
    secret_file.write_text('synthetic-upstream')
    secret_file.chmod(0o600)
    launcher = root / 'launch_manager.py'
    launcher.write_text('import sys\nsys.dont_write_bytecode=True\n'
        + 'sys.path.insert(0,' + repr(str(source / 'manager')) + ')\n'
        + 'from server.routers import gateway\n'
        + 'gateway._THREEWAY_SPEC=' + repr({name:(provider.base_url,str(secret_file))
                                          for name in ('qoder','trae')}) + '\n'
        + 'from server.serve import main\n'
        + 'main(' + repr(['--host','127.0.0.1','--port',str(port),
                         '--timeout-graceful-shutdown','45']) + ')\n')
    cert, key = root / 'tls.crt', root / 'tls.key'
    subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256',
                    '-nodes','-days','1','-subj','/CN=zhihui.lab','-addext',
                    'subjectAltName=DNS:zhihui.lab,IP:127.0.0.1','-keyout',str(key),'-out',str(cert)],
                   check=True,capture_output=True)
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1',443)) == 0:
            raise RuntimeError('Port 443 already used; do not stop an unrelated listener')
    nginx_conf = root / 'nginx.conf'
    nginx_conf.write_text(f'''user gateway-lab;
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 128; }}
http {{ access_log {root}/access.log;
 server {{ listen 127.0.0.1:443 ssl; server_name zhihui.lab;
  ssl_certificate {cert}; ssl_certificate_key {key};
  location / {{ proxy_pass http://127.0.0.1:{port}; proxy_http_version 1.1;
   proxy_set_header Host $host; proxy_set_header Connection "";
   proxy_buffering off; proxy_request_buffering off; proxy_ignore_client_abort off;
   proxy_read_timeout 120s;
  }}
 }}
}}
''')
    nginx = ['sudo','-n','/usr/sbin/nginx','-p',str(root),'-c',str(nginx_conf)]
    result = {'root':str(root),'source_sha256':qa['source_sha256'], 'production_connected':False,
              'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'checks':[]}
    child = None
    threads = []
    streams = {}
    import httpx
    control = httpx.Client(base_url='https://127.0.0.1', verify=str(cert), trust_env=False,
                           timeout=15, headers={'Host':'zhihui.lab'})
    observer = {'Authorization':'Bearer ' + admin}

    def state():
        response = control.get('/api/system/drain',headers=observer)
        response.raise_for_status()
        return response.json()

    def ready(code):
        try:
            return control.get('/api/readyz').status_code == code
        except httpx.HTTPError:
            return False

    def stream(name, api_key, first, cancel=False):
        record = streams[name] = {'lines':0,'done':False,'cancelled':cancel}
        path = '/v1/chat/completions' if name in ('workbuddy','cancel') else f'/{name}/v1/chat/completions'
        try:
            with httpx.Client(verify=str(cert),trust_env=False,timeout=90) as client:
                with client.stream('POST','https://127.0.0.1'+path,
                        headers={'Host':'zhihui.lab','Authorization':'Bearer '+api_key},
                        json={'model':'fake-model','messages':[{'role':'user','content':'qa'}],
                              'max_tokens':32,'stream':True}) as response:
                    record['status'] = response.status_code
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith('data:'):
                            record['lines'] += 1
                            first.set()
                            if cancel:
                                break
                        if line.strip() == 'data: [DONE]':
                            record['done'] = True
        except Exception as error:
            record['error'] = type(error).__name__ + ': ' + str(error)
            first.set()

    try:
        with (root / 'manager.log').open('w') as log:
            child = subprocess.Popen([qa['python'],'-u',str(launcher)], env=env,
                                     stdout=log,stderr=subprocess.STDOUT)
        result['manager_pid'] = child.pid
        subprocess.run([*nginx,'-t'],check=True,capture_output=True)
        subprocess.run(nginx,check=True,capture_output=True)
        wait_for(lambda: ready(200),120)
        print('READY real Manager + Nginx/TLS',flush=True)
        for name, entry in keys.items():
            first = threading.Event()
            thread = threading.Thread(target=stream,args=(name,entry['key'],first))
            threads.append(thread);thread.start()
            assert first.wait(60), name + ' did not emit a frame'
            assert streams[name].get('status') == 200 and 'error' not in streams[name], streams[name]
        cancel_first = threading.Event()
        cancelled = threading.Thread(target=stream,args=('cancel',cancel_key['key'],cancel_first,True))
        threads.append(cancelled);cancelled.start()
        assert cancel_first.wait(30)
        cancelled.join(15)
        assert not cancelled.is_alive() and 'error' not in streams['cancel'], streams['cancel']
        wait_for(lambda: state()['in_flight'].get('inference_stream') == 3,15)
        print('Three live bodies counted; intentional client cancellation observed',flush=True)
        before = provider.stats.snapshot()['total']
        os.kill(child.pid,signal.SIGUSR1)
        wait_for(lambda: ready(503),15)
        denied = control.get('/v1/models',headers={'Authorization':'Bearer '+keys['workbuddy']['key']})
        assert denied.status_code == 503 and denied.json()['error']['code'] == 'gateway_draining'
        assert provider.stats.snapshot()['total'] == before
        assert state()['total_in_flight'] == 3
        os.kill(child.pid,signal.SIGUSR2)
        wait_for(lambda: ready(200),15)
        assert control.get('/v1/models',headers={'Authorization':'Bearer '+keys['workbuddy']['key']}).status_code == 200
        os.kill(child.pid,signal.SIGUSR1)
        wait_for(lambda: ready(503),15)
        assert state()['total_in_flight'] == 3, 'Need live bodies when SIGTERM is sent'
        sent = time.monotonic()
        os.kill(child.pid,signal.SIGTERM)
        child.wait(timeout=90)
        elapsed = time.monotonic() - sent
        for thread in threads:
            thread.join(15)
            assert not thread.is_alive()
        assert all(streams[name].get('done') and 'error' not in streams[name] for name in keys), streams
        assert elapsed > 3, 'The fixture must exercise streams longer than the production 3s grace'
        log_text = (root / 'manager.log').read_text()
        assert 'Application shutdown complete' in log_text, 'No successful lifespan shutdown evidence'
        assert child.returncode in (0,-signal.SIGTERM), child.returncode
        with sqlite3.connect('file:'+str(config.DB_PATH)+'?mode=ro',uri=True) as connection:
            connection.row_factory = sqlite3.Row
            chats = [dict(row) for row in connection.execute(
                "SELECT key_id,status,actual_platform,terminal_state,usage_complete FROM request_logs "
                "WHERE endpoint LIKE '%chat/completions'")]
            for name, entry in keys.items():
                rows = [row for row in chats if row['key_id']==entry['id']]
                assert len(rows)==1 and rows[0]['status']==200 and rows[0]['actual_platform']==name, rows
                assert rows[0]['terminal_state']=='completed' and rows[0]['usage_complete'], rows
                assert connection.execute('SELECT used_tokens FROM api_keys WHERE id=?',(entry['id'],)).fetchone()[0]==12
            rows = [row for row in chats if row['key_id']==cancel_key['id']]
            assert len(rows)==1 and rows[0]['status']==499 and not rows[0]['usage_complete'], rows
            unresolved = [dict(row) for row in connection.execute(
                'SELECT key_id,state FROM budget_reservations WHERE key_id=?',(cancel_key['id'],))]
            assert unresolved and all(row['state']=='unknown' for row in unresolved), unresolved
            assert not connection.execute('SELECT request_id FROM usage_events GROUP BY request_id HAVING count(*)>1').fetchall()
        result.update(status='passed',streams=streams,request_logs=chats,unknown_reservations=unresolved,
                      sigterm_exit=child.returncode,sigterm_wait_s=round(elapsed,3),planned_stream_cuts=0,
                      admission_interruption_s=None,admission_note='No replacement-instance handoff was performed',
                      fake_provider=provider.stats.snapshot())
        result['checks'] = ['three_platform_streams_complete_through_sigterm','live_body_counts',
                            'pre_drain_rejects_without_upstream_io','drain_abort_resumes_admission',
                            'real_client_cancel_499_unknown_hold','exact_completed_token_debits',
                            'lifespan_shutdown_complete']
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=10)
                result['forced_fixture_cleanup'] = True
        for thread in threads:
            thread.join(15)
        if (root / 'nginx.pid').exists():
            subprocess.run([*nginx,'-s','quit'],check=True,capture_output=True,timeout=30)
        control.close()
        provider.stop()
        result.setdefault('status','failed')
        result['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (root / 'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    raise SystemExit(main())
