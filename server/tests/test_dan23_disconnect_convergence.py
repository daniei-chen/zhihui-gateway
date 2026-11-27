"""DAN-23：流式断连必须收敛到确定结算，不能把有额度密钥锁成终态 429。

真实 ASGI + 真实上游桩：上游发一帧正文就挂住，客户端在流中途断开。
断连是调用方自己就能制造的状态（按 Esc、超时重连），所以它不能成为
「该密钥此后不可用」的开关。
"""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from server.tests.audit_fixtures import state  # noqa: F401  隔离数据目录


def _upstream(*, usage):
    """上游桩：发一帧正文后挂住，等客户端先走。"""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            self.server.calls += 1
            if not request.get('stream'):
                raw = json.dumps({'id': 'synthetic-chat', 'object': 'chat.completion',
                                  'model': 'cn:deepseek-v4-flash',
                                  'choices': [{'index': 0, 'finish_reason': 'stop',
                                               'message': {'role': 'assistant', 'content': 'ok'}}],
                                  'usage': {'prompt_tokens': 17, 'completion_tokens': 5,
                                            'total_tokens': 22, 'credit': 0.000001}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            event = {'id': 'synthetic-chat', 'object': 'chat.completion.chunk',
                     'model': 'cn:deepseek-v4-flash',
                     'choices': [{'index': 0, 'delta': {'content': 'synthetic answer'},
                                  'finish_reason': None}]}
            if usage:
                event['usage'] = {'prompt_tokens': 17, 'completion_tokens': 5,
                                  'total_tokens': 22, 'credit': 0.123456}
            frame = ('data: ' + json.dumps(event) + chr(10) + chr(10)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.write(('%x' % len(frame)).encode() + CRLF + frame + CRLF)
            self.wfile.flush()
            self.server.release.wait(20)
            try:
                self.wfile.write(b'0' + CRLF + CRLF)
            except OSError:
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.calls = 0
    server.daemon_threads = True
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


CRLF = bytes((13, 10))


def _client(app, key, body):
    """真实 ASGI 调用；第一帧正文发给客户端时断开（模拟流中途断连）。"""
    raw = json.dumps(body).encode()
    headers = []
    delivered = []

    async def run():
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': raw, 'more_body': False}
            await asyncio.Event().wait()

        async def send(message):
            if message['type'] == 'http.response.start':
                headers.extend(message['headers'])
            if message['type'] == 'http.response.body' and message.get('body'):
                delivered.append(message['body'])
                raise OSError('synthetic downstream disconnected mid-stream')

        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
                 'path': '/v1/chat/completions', 'raw_path': b'/v1/chat/completions',
                 'root_path': '', 'query_string': b'', 'client': ('127.0.0.1', 10000),
                 'server': ('127.0.0.1', 80),
                 'headers': [(b'host', b'127.0.0.1'), (b'content-type', b'application/json'),
                             (b'content-length', str(len(raw)).encode()),
                             (b'authorization', ('Bearer ' + key).encode())]}
        from starlette.requests import ClientDisconnect
        try:
            await app(scope, receive, send)
        except (OSError, ClientDisconnect):
            pass

    asyncio.run(run())
    return dict(headers).get(b'x-request-id', b'').decode(), delivered


def _app(provider, monkeypatch):
    from fastapi import FastAPI
    from server import config, observability
    from server.routers import gateway

    monkeypatch.setattr(config, 'WB2API_BASE', 'http://127.0.0.1:%d' % provider.server_port)
    monkeypatch.setenv('WB_SSE_HEARTBEAT_SECONDS', '0')
    app = FastAPI()
    app.include_router(gateway.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    return app


def _aged(seconds=1000):
    """把断连那条预留推回结算窗口之前（15 分钟窗口是 A-002 的判据）。"""
    from server import db
    db.execute('UPDATE budget_reservations SET created_at=created_at-?, updated_at=updated_at-?',
               (seconds, seconds))


def test_disconnect_after_complete_usage_settles_at_the_measured_amount(state, monkeypatch):
    """上游已经回完整用量、客户端才走：费用是确定的，必须直接结清。

    这条不是「猜」—— 回执里 prompt/completion/credit 都在，和客户端是否
    挂断无关。留成 unknown 只会让它进人工核对队列，而队列里没有事实可核。
    """
    from server import db, keysvc

    provider = _upstream(usage=True)
    try:
        key = keysvc.create_key('dan23-known-usage', quota=1_000_000, quota_credit=10)
        app = _app(provider, monkeypatch)
        rid, delivered = _client(app, key['key'], {'model': 'deepseek-v4-flash', 'stream': True,
                                                   'messages': [{'role': 'user', 'content': 'hi'}]})
    finally:
        provider.release.set()
    assert delivered and b'usage' in delivered[0]
    log = db.query_one('SELECT terminal_state,usage_complete,credit FROM request_logs WHERE request_id=?', (rid,))
    assert log['terminal_state'] == 'client_cancelled' and log['usage_complete'] == 1
    row = db.query_one('SELECT state,charged_tokens,charged_credit FROM budget_reservations WHERE request_id=?', (rid,))
    assert row['charged_tokens'] == 22 and row['charged_credit'] == 0.123456
    assert row['state'] == 'settled'

def test_stream_disconnect_does_not_permanently_lock_key(state, monkeypatch):
    """上游没回用量就断连：预留只能是「待核对」，不能变成密钥的终态。

    这条走真实断连：上游只发了一帧正文、还没发 usage 帧，客户端就挂了。
    结算窗口（A-002 的 15 分钟）过去之后仍应 fail-closed —— 没有证据就
    不能当作零用量放行。但它必须有出口：处理期限（默认 7 天）一到，
    `archive_overdue_unknowns` 把这条标成「已归档待核对」，密钥随之恢复。
    """
    from fastapi.testclient import TestClient
    from server import budget, db, keysvc

    provider = _upstream(usage=False)
    try:
        key = keysvc.create_key('dan23-no-usage', quota=1_000_000, quota_credit=10)
        app = _app(provider, monkeypatch)
        rid, _ = _client(app, key['key'], {'model': 'deepseek-v4-flash', 'stream': True,
                                           'messages': [{'role': 'user', 'content': 'hi'}]})
    finally:
        provider.release.set()
    row = db.query_one('SELECT state FROM budget_reservations WHERE request_id=?', (rid,))
    assert row['state'] == 'unknown'

    def call():
        with TestClient(app) as client:
            return client.post('/v1/chat/completions',
                               json={'model': 'deepseek-v4-flash',
                                     'messages': [{'role': 'user', 'content': 'again'}]},
                               headers={'Authorization': 'Bearer ' + key['key']})

    # 结算窗口内：瞬态的 budget_pending（A-002 的 15 分钟窗口）。
    assert call().status_code == 429
    _aged()
    # 窗口外、处理期限内：仍然 fail-closed（不拿「旧」当「没发生」）。
    blocked = call()
    assert blocked.status_code == 429 and 'credit_quota_unconfirmed' in blocked.text
    # 处理期限到：归档（生产里由 operational 的对账轮次每 15 分钟调用一次）。
    _aged(8 * 86400)
    assert budget.archive_overdue_unknowns(int(time.time())) == 1
    recovered = call()
    assert recovered.status_code == 200, recovered.text
    still = db.query_one('SELECT state,resolution FROM budget_reservations WHERE request_id=?', (rid,))
    assert still['state'] == 'unknown'          # 不伪造结清，仍可人工核对
    assert still['resolution'].startswith(budget.ARCHIVE_RESOLUTION_PREFIX)


def test_cancelled_unknown_stops_holding_the_token_estimate_after_the_deadline(state, monkeypatch):
    """没有证据的 unknown 也不能永久占着 token 估算。

    估算（输入字节 + max_tokens）不是扣费。把一条「查不到证据」的旧行
    无限期挂在 remaining 里，等于给密钥凭空记了一笔从未发生的用量：
    断连次数攒够了，配额就被这笔不存在的账吃光。
    """
    from fastapi.testclient import TestClient
    from server import budget, db, keysvc

    provider = _upstream(usage=False)
    try:
        key = keysvc.create_key('dan23-hold', quota=20000)
        keysvc.touch(key, '127.0.0.1', 15000, 0)
        app = _app(provider, monkeypatch)
        rid, _ = _client(app, key['key'], {'model': 'deepseek-v4-flash', 'stream': True,
                                           'messages': [{'role': 'user', 'content': 'hi'}]})
    finally:
        provider.release.set()

    def call():
        with TestClient(app) as client:
            return client.post('/v1/chat/completions',
                               json={'model': 'deepseek-v4-flash',
                                     'messages': [{'role': 'user', 'content': 'again'}]},
                               headers={'Authorization': 'Bearer ' + key['key']})
    _aged()
    blocked = call()
    assert blocked.status_code == 429 and 'insufficient_quota' in blocked.text
    _aged(8 * 86400)
    assert budget.archive_overdue_unknowns(int(time.time())) == 1
    recovered = call()
    assert recovered.status_code == 200, recovered.text
    held = db.query_one("SELECT COALESCE(SUM(MAX(0,reserved_tokens-charged_tokens)),0) n "
                        "FROM budget_reservations WHERE key_id=? AND state='unknown'", (key['id'],))
    assert held['n'] > 0        # 估算还在行上，只是不再占额度
