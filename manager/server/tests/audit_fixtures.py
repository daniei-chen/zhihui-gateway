"""Independent, synthetic-only finance audit. Run ONLY via deploy/isolated_runner.py."""
from __future__ import annotations

import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tarfile
import threading
import time

import pytest


@pytest.fixture
def state(tmp_path, monkeypatch):
    from server import budget, budgetowner, config, db, ledgerreplay, observability

    root = tmp_path / 'private-state'
    root.mkdir()
    with db._lock:
        db._close_connection_locked()
    budgetowner.close()
    monkeypatch.setattr(config, 'DATA_DIR', root)
    monkeypatch.setattr(config, 'DB_PATH', root / 'manager.db')
    monkeypatch.setattr(config, 'USERS_FILE', root / 'users.json')
    monkeypatch.setenv('WB_LEDGER_SPOOL_DIR', str(root / 'ledger-spool'))
    monkeypatch.setenv('WB_PORTAL_PUBLIC_POOL_ENABLED', '1')
    monkeypatch.setenv('WB_PORTAL_AUTO_CLOSE_CLIENT_CANCEL', '0')
    monkeypatch.setattr(observability, '_middleware_ref', None)
    monkeypatch.setattr(ledgerreplay, '_PROBE_CACHE', {'at': 0.0, 'key': '', 'ok': None, 'reason': ''})
    db.connect()
    budget.initialize()
    yield root
    budgetowner.close()
    with db._lock:
        db._close_connection_locked()


def portal_key():
    from server import db, keysvc, portalbilling, portalpolicy, portalstore as store

    with db.transaction() as conn:
        made = keysvc.create_key('synthetic portal', models=list(portalpolicy.MODELS),
                                 realm='cn', platforms=['workbuddy'], _conn=conn, _portal=True)
        conn.execute('UPDATE api_keys SET source=? WHERE id=?', (portalpolicy.SOURCE, made['id']))
    with store.transaction() as conn:
        conn.execute('INSERT INTO portal_users(id,username,pwd_hash,created_at) VALUES(?,?,?,?)',
                     ('synthetic-user', 'synthetic-user', 'not-a-real-password-hash', store.now()))
        conn.execute('INSERT INTO portal_key_links VALUES(?,?,?)',
                     (made['id'], 'synthetic-user', keysvc._hash(made['key'])))
        conn.execute('INSERT INTO portal_keys VALUES(?,?,?,?,0)',
                     (made['id'], 1, 'synthetic-key', store.now()))
        conn.execute('INSERT INTO portal_bonus_grants VALUES(?,?,?,?,?,?,?,?,?)',
                     ('a' * 32, 'synthetic-user', 10_000_000, 10_000_000,
                      store.now(), store.now() + 86400, 'admin', 'synthetic testing only', 1))
        conn.execute('INSERT INTO portal_settings VALUES(?,?)', ('billing_policy', portalbilling.POLICY))
    return made


@pytest.fixture
def provider():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            body = {'id': 'synthetic-chat', 'object': 'chat.completion', 'model': 'cn:deepseek-v4-flash',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'synthetic answer'},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 17, 'completion_tokens': 5, 'total_tokens': 22,
                              'credit': self.server.credit}}
            if request.get('stream'):
                event = dict(body, object='chat.completion.chunk')
                event['choices'] = [{'index': 0, 'delta': {'content': 'synthetic answer'},
                                     'finish_reason': 'stop'}]
                raw = ('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode()
            else:
                raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if request.get('stream') else 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.server.calls += 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.credit = 0.1234567
    server.calls = 0
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


