"""G-02：回执重放必须认 DAN-23 契约，charged_partial 必须有归档出口。

审计 `docs/AUDIT-2026-11-28.md` 的 D-02（handoff `02-budget-ledger.md` 的 D-01）：

* 同一份「完整 usage + client_cancelled」回执，实时路径落 `settled`
  （`server/budget.py` 的 `settle`），崩溃恢复后的重放路径却只认
  `terminal_state == 'completed'`（`server/ledgerreplay.py` 的 `_settle`），
  于是落成 `unknown` 且带 `charged_tokens`；
* `charged_tokens > 0` 让 `_classify` 归为 `charged_partial`，而它不在
  `EVIDENCELESS_CLASSES` 里 → `archive_overdue_unknowns` 永不归档
  → 对设了 credit 配额的密钥，`prepare` 的 `unknown_count` 恒 ≥1
  → 永久 429 `credit_quota_unconfirmed`。

这里走真实链路造出「结算事务失败 → 回执落 spool」那一刻（与
`test_dan23_disconnect_convergence` 同一上游桩：真实 ASGI 断连，结算事务
抛 `database is locked`），之后的重放是生产里的 `replay_batch`。
"""
import asyncio
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from server.tests.audit_fixtures import state  # noqa: F401  隔离数据目录

CRLF = bytes((13, 10))
USAGE = {'prompt_tokens': 17, 'completion_tokens': 5, 'total_tokens': 22, 'credit': 0.123456}


def _upstream():
    """上游桩：流式第一帧就带完整 usage，然后挂住等客户端先走。"""
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
                                  'usage': USAGE}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            event = {'id': 'synthetic-chat', 'object': 'chat.completion.chunk',
                     'model': 'cn:deepseek-v4-flash',
                     'choices': [{'index': 0, 'delta': {'content': 'synthetic answer'},
                                  'finish_reason': None}],
                     'usage': USAGE}
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


def _crash(provider, monkeypatch, key):
    """真实断连 + 结算事务失败：回执落 spool，库里没有请求日志、预留仍是 forwarded。"""
    from server import budget, db, ledgerreplay

    app = _app(provider, monkeypatch)
    try:
        with mock.patch.object(budget, 'settle',
                               side_effect=sqlite3.OperationalError('database is locked')):
            rid, delivered = _client(app, key['key'], {'model': 'deepseek-v4-flash',
                                                       'stream': True,
                                                       'messages': [{'role': 'user',
                                                                     'content': 'hi'}]})
    finally:
        provider.release.set()
    assert delivered and b'usage' in delivered[0]
    for _ in range(50):
        if ledgerreplay.backlog() >= 1:
            break
        time.sleep(0.1)
    assert ledgerreplay.backlog() == 1, '结算失败必须留下待重放回执'
    assert db.query_one('SELECT 1 FROM request_logs WHERE request_id=?', (rid,)) is None
    row = db.query_one('SELECT state,charged_tokens,charged_credit FROM budget_reservations '
                       'WHERE request_id=?', (rid,))
    assert row['state'] in ('forwarded', 'unknown'), '结算事务回滚后预留没进终态'
    assert not row['charged_tokens'] and not row['charged_credit'], '结算回滚后一分钱都不该记'
    return app, rid


def _spool():
    from server import ledgerreplay
    files = sorted(ledgerreplay.spool_dir().glob('*.json'))
    assert len(files) == 1, files
    return files[0]


def _replay_pending():
    from server import ledgerreplay
    result = ledgerreplay.replay_batch()
    assert result['applied'] == 1 and result['failed'] == 0, result


def _replay_facts(facts):
    from server import ledgerreplay
    entry = ledgerreplay.build_entry(facts, release_id='g02-test')
    assert ledgerreplay.spill(entry)
    _replay_pending()


def _aged(seconds):
    """把预留推回结算窗口 / 处理期限之前。"""
    from server import db
    db.execute('UPDATE budget_reservations SET created_at=created_at-?, updated_at=updated_at-?',
               (seconds, seconds))


def _call(app, key):
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        return client.post('/v1/chat/completions',
                           json={'model': 'deepseek-v4-flash',
                                 'messages': [{'role': 'user', 'content': 'again'}]},
                           headers={'Authorization': 'Bearer ' + key['key']})


def _reservation(rid):
    from server import db
    return db.query_one('SELECT state,charged_tokens,charged_credit,reserved_tokens,resolution,'
                        'updated_at FROM budget_reservations WHERE request_id=?', (rid,))


def _key_usage(key_id):
    from server import db
    row = db.query_one('SELECT used_tokens,used_credit_units FROM api_keys WHERE id=?', (key_id,))
    return int(row['used_tokens'] or 0), int(row['used_credit_units'] or 0)


# ── 1. 重放路径与实时路径同一判据 ────────────────────────────────────────
def test_replayed_client_cancelled_settles_like_the_live_path(state, monkeypatch):
    """同一份「完整 usage + client_cancelled」回执，重放后必须是 settled。

    实时路径已经由 DAN-23 落 settled；崩溃恢复后改成 unknown，等于把
    「费用确定」这一个事实随进程一起丢掉。
    """
    from server import db, keysvc, ledgerreplay

    provider = _upstream()
    key = keysvc.create_key('g02-replay', quota=1_000_000, quota_credit=10)
    try:
        _, rid = _crash(provider, monkeypatch, key)
    finally:
        provider.release.set()
    receipt = ledgerreplay._read_receipt(_spool())['facts']
    assert receipt['terminal_state'] == 'client_cancelled' and receipt['usage_complete'] == 1

    _replay_pending()

    log = db.query_one('SELECT terminal_state,usage_complete FROM request_logs WHERE request_id=?',
                       (rid,))
    assert log['terminal_state'] == 'client_cancelled' and log['usage_complete'] == 1
    row = _reservation(rid)
    assert row['state'] == 'settled', '重放路径必须与 budget.settle 同一判据'
    assert row['charged_tokens'] == 22 and row['charged_credit'] == 0.123456
    assert _key_usage(key['id']) == (22, 123456), '重放只记一次用量，不重复扣'


# ── 2. 恢复后密钥不再被锁 ────────────────────────────────────────────────
def test_recovery_does_not_lock_a_credit_quota_key(state, monkeypatch):
    """恢复路径产出的 unknown 不能变成 credit 配额密钥的终态 429。"""
    from server import keysvc

    provider = _upstream()
    key = keysvc.create_key('g02-lock', quota=1_000_000, quota_credit=10)
    try:
        app, rid = _crash(provider, monkeypatch, key)
    finally:
        provider.release.set()
    _replay_pending()
    assert _reservation(rid)['state'] == 'settled'

    _aged(1000)                         # 越过 A-002 的 15 分钟在途窗口
    admitted = _call(app, key)
    assert admitted.status_code == 200, admitted.text


# ── 3. charged_partial 的归档出口 ────────────────────────────────────────
def test_overdue_charged_partial_is_archived_without_touching_charged_usage(state, monkeypatch):
    """已计入 charged 的 unknown 超过处理期限后必须能归档，且不许改钱。

    上游回了完整 usage、终态是错误：费用确定、结论未知。重放后它带着
    `charged_tokens=22 / charged_credit=0.123456` 停在 unknown（`charged_partial`），
    此前没有任何自动出口 —— 密钥永久 429，且队列里没有任何事实可核。
    归档只能「标已归档待核对 + 释放剩余估算」，已计入的钱一分不能动。
    """
    from server import budget, db, keysvc

    provider = _upstream()
    key = keysvc.create_key('g02-partial', quota=1_000_000, quota_credit=10)
    try:
        app, rid = _crash(provider, monkeypatch, key)
    finally:
        provider.release.set()
    # 同一份回执改成「上游回了 usage 然后报错」：终态不是 completed/client_cancelled。
    path = _spool()
    entry = json.loads(path.read_text(encoding='utf-8'))
    entry['facts'].update(terminal_state='upstream_error', status=502, http_status=502)
    path.write_text(json.dumps(entry), encoding='utf-8')
    _replay_pending()

    row = _reservation(rid)
    assert row['state'] == 'unknown'
    assert row['charged_tokens'] == 22 and row['charged_credit'] == 0.123456
    charged_before = (row['charged_tokens'], row['charged_credit'])
    usage_before = _key_usage(key['id'])

    _aged(1000)
    blocked = _call(app, key)
    assert blocked.status_code == 429 and 'credit_quota_unconfirmed' in blocked.text

    _aged(8 * 86400)
    now = int(time.time())
    updated_before = _reservation(rid)['updated_at']
    assert budget.archive_overdue_unknowns(now) == 1, 'charged_partial 必须有归档出口'
    archived = _reservation(rid)
    assert archived['state'] == 'unknown', '归档不是结清：不伪造 state'
    assert (archived['charged_tokens'], archived['charged_credit']) == charged_before, \
        '归档不许改动已计入的 charged（不重复计费、不退费）'
    assert _key_usage(key['id']) == usage_before, '归档不许改动密钥已记用量'
    assert archived['resolution'].startswith(budget.ARCHIVE_RESOLUTION_PREFIX)
    assert archived['updated_at'] == updated_before, '不许把旧行刷成在途'
    assert budget.archive_overdue_unknowns(now) == 0, '归档必须幂等'
    assert db.query_one("SELECT count(*) n FROM audit_logs WHERE action='budget.archive'"
                        )['n'] == 1

    summary = budget.unresolved_summary(now=now)['summary']
    assert summary['archived_pending'] == 1
    assert summary['needs_human'] == 0, '已归档的不能继续稀释告警'

    recovered = _call(app, key)
    assert recovered.status_code == 200, recovered.text
    usage_after_admit = _key_usage(key['id'])

    assert budget.resolve(rid, 22, 0.123456, 'ops', '人工核对：上游已回 usage 22 token')
    resolved = _reservation(rid)
    assert resolved['state'] == 'settled', '归档行仍可人工 resolve'
    assert _key_usage(key['id']) == usage_after_admit, '结清到已计入的值不重复扣'


# ── 4. 放宽的边界：只认「完整 usage」，不认其他未知 ──────────────────────
def _facts(rid, key_id, **overrides):
    now = int(time.time())
    base = {'ts': now, 'key_id': key_id, 'ip': '127.0.0.1', 'model': 'fake-model',
            'mapped_model': 'fake-model', 'status': 499, 'prompt_tokens': 17,
            'completion_tokens': 5, 'latency_ms': 10, 'ua': 'pytest', 'stream': 1,
            'credit': 0.123456, 'realm': 'cn', 'request_id': rid,
            'endpoint': '/v1/chat/completions', 'method': 'POST', 'protocol': 'openai',
            'http_status': 499, 'terminal_state': 'client_cancelled',
            'usage_source': 'upstream', 'usage_complete': 1, 'platform': 'workbuddy'}
    base.update(overrides)
    return base


def _forwarded(rid, key_id):
    from server import db
    now = int(time.time())
    db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
               'reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               (rid, key_id, now, 'forwarded', 4096, 0.0, now, 'g02-replay', 1, now + 60))


def test_replay_does_not_settle_incomplete_usage(state):
    """usage 不完整时仍是 unknown —— 放宽只针对「费用确定」。"""
    from server import keysvc

    key = keysvc.create_key('g02-incomplete', realm='cn')
    _forwarded('g02-incomplete', key['id'])
    _replay_facts(_facts('g02-incomplete', key['id'], usage_complete=0))
    row = _reservation('g02-incomplete')
    assert row['state'] == 'unknown'
    assert row['charged_tokens'] == 22      # 用量照记，结论仍待核对


def test_replay_keeps_credit_quota_key_unknown_when_credit_is_absent(state):
    """credit 配额密钥缺 credit 字段：与 budget.settle 一样不能判 settled。"""
    from server import keysvc

    key = keysvc.create_key('g02-nocredit', quota=1_000_000, quota_credit=10)
    _forwarded('g02-nocredit', key['id'])
    _replay_facts(_facts('g02-nocredit', key['id'], credit=None))
    assert _reservation('g02-nocredit')['state'] == 'unknown'


def test_replay_still_settles_completed(state):
    """completed 的老契约不许回退。"""
    from server import keysvc

    key = keysvc.create_key('g02-completed', realm='cn')
    _forwarded('g02-completed', key['id'])
    _replay_facts(_facts('g02-completed', key['id'], terminal_state='completed',
                         status=200, http_status=200))
    row = _reservation('g02-completed')
    assert row['state'] == 'settled' and row['charged_tokens'] == 22


# ── 5. 归档面不许再放宽到 usage_present ──────────────────────────────────
def test_usage_present_unknown_is_still_never_archived(state):
    """有上游 usage 证据但不属于 charged_partial 的行，仍然留在人工队列。"""
    from server import budget, db, keysvc

    key = keysvc.create_key('g02-usage', quota=1_000_000, realm='cn')
    now = int(time.time())
    created = now - 30 * 86400
    db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
               'reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               ('g02-usage', key['id'], created, 'unknown', 4096, 0.0, created,
                'g02-replay', 1, 0))
    db.add_request_log(ts=created, key_id=key['id'], status=200, request_id='g02-usage',
                       terminal_state='completed', usage_source='upstream',
                       usage_complete=1, credit=0.1, platform='workbuddy')
    assert budget.archive_overdue_unknowns(now) == 0
    assert _reservation('g02-usage')['resolution'] is None


# ── 6. 归档行拿到证据之后，准入不能再按前缀放行 ──────────────────────────
def test_archived_row_revived_by_late_evidence_blocks_again(state):
    """归档时是「没有证据」，回执重放补上费用之后，准入必须跟着改口。

    归档的意思是「查过、没有证据、平台承担未知差额」，所以它不再阻塞准入。
    可一旦迟到回执把 charged_tokens 写上，这个结论就作废了：告警、面板都会
    把它重新计入 needs_human（`archived_now`），准入却只看 resolution 前缀
    —— 同一行两套结论，已计入但未核对的积分可以超出配额（评审指出）。
    """
    from server import budget, db, keysvc

    key = keysvc.create_key('g02-revive', quota_credit=10)
    now = int(time.time())
    created = now - 30 * 86400
    db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
               'reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               ('g02-revive', key['id'], created, 'unknown', 4096, 0.0, created, 'g02', 1, 0))
    db.execute('UPDATE budget_reservations SET resolution=? WHERE request_id=?',
               (budget.ARCHIVE_RESOLUTION_PREFIX
                + '超过 7 天处理期限仍无上游证据（分类 no_request_log，创建于 x）',
                'g02-revive'))

    _replay_facts(_facts('g02-revive', key['id'], terminal_state='upstream_error',
                         status=502, http_status=502))

    row = _reservation('g02-revive')
    assert row['state'] == 'unknown' and row['charged_tokens'] == 22
    summary = budget.unresolved_summary(now=int(time.time()))['summary']
    assert summary['needs_human'] == 1 and summary['archived_pending'] == 0

    _aged(1000)                         # 越过 A-002 的 15 分钟在途窗口
    from server import observability
    state_audit = observability.RequestAudit('g02-revive-next', '/v1/chat/completions',
                                            'POST', 'openai', time.monotonic())
    observability.current.set(state_audit)
    try:
        with pytest.raises(budget.BudgetRejected) as caught:
            budget.prepare(key, {'messages': [], 'max_tokens': 64})
    finally:
        observability.current.set(None)
    assert caught.value.code == 'credit_quota_unconfirmed', caught.value.code
