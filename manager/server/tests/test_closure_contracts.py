import asyncio
import sqlite3
import time
import unittest
from unittest import mock
from server import budget,budgetowner,config,db,ledger,observability,keysvc
from server.bodyguard import APIBodyGuard
from server.tests import test_full_upgrade
from server.upstreamschema import validate

class LedgerContracts(test_full_upgrade.DurableBudgetTests):
    def test_events_survive_log_cleanup_and_reject_mutation(self):
        with db.transaction():
            ledger.record('test',1,'fixture','cn',10,2,0.01,True,'completed','fixture')
            ledger.record('test',1,'fixture','cn',10,2,0.01,True,'completed','fixture')
        self.assertEqual(len(db.query('SELECT * FROM usage_events')),1)
        self.assertEqual(db.query_one('SELECT credit_units FROM usage_events')[0],10000)
        db.execute('DELETE FROM request_logs')
        for sql in ('DELETE FROM usage_events','UPDATE usage_events SET credit_units=0'):
            with self.assertRaises(sqlite3.IntegrityError):
                with db.transaction():db.execute(sql)
        self.assertEqual(len(db.query('SELECT * FROM usage_events')),1)
    def test_integer_credit_totals(self):
        with db.transaction():
            for _ in range(100):
                keysvc.touch(self.key,'fixture',1,0.01)
                db.bump_usage(1,'fixture',1,0,0.01)
        self.assertEqual(db.query_one('SELECT used_credit_units FROM api_keys WHERE id=1')[0],1000000)
        self.assertEqual(db.query_one('SELECT credit,credit_units FROM usage_daily')[0],1.0)
        self.assertEqual(db.query_one('SELECT credit,credit_units FROM usage_daily')[1],1000000)
    def test_event_and_summary_rollback_together(self):
        with self.assertRaises(RuntimeError):
            with db.transaction():
                ledger.record('rollback',1,'fixture','cn',1,1,0.01,True,'completed','fixture')
                keysvc.touch(self.key,'fixture',2,0.01)
                raise RuntimeError('simulated I/O failure')
        self.assertEqual(db.query_one('SELECT count(*) FROM usage_events')[0],0)
        self.assertEqual(db.query_one('SELECT used_tokens FROM api_keys WHERE id=1')[0],0)
    def test_audit_chain_survives_ui_clear(self):
        db.add_audit_log('fixture','change','fixture')
        db.clear_audit_logs()
        self.assertTrue(ledger.verify_chain())
        self.assertEqual(db.query_one('SELECT count(*) FROM admin_events')[0],1)
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction():db.execute('DELETE FROM admin_events')
    def test_expired_lease_does_not_release_live_owner(self):
        budget.prepare(self.key,{'messages':[]})
        db.execute('UPDATE budget_reservations SET lease_until=0')
        budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'reserved')
    def test_recovered_fence_cannot_forward(self):
        budget.prepare(self.key,{'messages':[]});budgetowner.close();budget.recover()
        with self.assertRaises(budget.BudgetRejected):budget.forwarded()
    def test_legacy_owner_is_frozen(self):
        db.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,updated_at) VALUES('legacy',1,1,'reserved',1)")
        budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
    def test_money_invalid_and_half_even(self):
        self.assertIsNone(ledger.units(None))
        self.assertEqual(ledger.units('0.0000005'),0)
        self.assertEqual(ledger.units('0.0000015'),2)
        for value in (True,float('nan'),float('inf'),'x'):
            with self.assertRaises(ValueError):ledger.units(value)
    def test_log_repair_cannot_replace_new_financial_events(self):
        with db.transaction():
            ledger.record('guard',1,'fixture','cn',1,1,0.01,True,'completed','fixture')
        for operation in (db.backfill_usage_from_logs,db.rebuild_usage_from_logs):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(db.query_one('SELECT count(*) FROM usage_events')[0],1)
    def test_exact_attempt_trace_is_idempotent_and_immutable(self):
        db.add_request_log(ts=1,key_id=1,ip='fixture',model='fixture',mapped_model='fixture',status=200,prompt_tokens=1,completion_tokens=1,latency_ms=1,stream=0,request_id='exact-fixture')
        event={'event':'workbuddy_attempt','request_id':'exact-fixture','attempt':2,
               'account_uid':'synthetic-account','resolved_model':'fixture','realm':'cn',
               'phase':'stream_terminal','status':200,'config_hash':'a'*64,'secret':'discarded'}
        self.assertTrue(ledger.attempt(event));self.assertTrue(ledger.attempt(event))
        self.assertEqual(db.query_one('SELECT count(*) FROM attempt_events')[0],1)
        row=db.query_one('SELECT account,account_source,account_attempt FROM request_logs WHERE request_id=?',('exact-fixture',))
        self.assertEqual(tuple(row),('synthetic-account','request_id',2))
        self.assertFalse(ledger.attempt(dict(event,request_id='')))
        self.assertFalse(ledger.attempt(dict(event,attempt=True)))
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction():db.execute('DELETE FROM attempt_events')

class BodyContracts(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,chunks,headers=(),delay=0,timeout=0.2):
        self.called=False;self.body=b''
        async def app(scope,receive,send):
            self.called=True
            self.body=(await receive())['body']
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
        pending=list(chunks)
        async def receive():
            if delay:await asyncio.sleep(delay)
            if not pending:return {'type':'http.disconnect'}
            value=pending.pop(0)
            return {'type':'http.request','body':value,'more_body':bool(pending)}
        out=[]
        async def send(msg):out.append(msg)
        await APIBodyGuard(app,maximum=10,timeout=timeout)({'type':'http','path':'/api/login','headers':headers},receive,send)
        return out[0]['status'] if out else None
    async def test_chunked_overflow(self):
        self.assertEqual(await self.exercise([b'12345',b'123456']),413);self.assertFalse(self.called)
    async def test_deceptive_length(self):
        self.assertEqual(await self.exercise([b'12345'],[(b'content-length',b'1')]),400)
    async def test_valid_chunked(self):
        self.assertEqual(await self.exercise([b'12345',b'12345']),200);self.assertEqual(self.body,b'1234512345')
    async def test_invalid_lengths(self):
        for value in (b'-1',b'x'):
            self.assertEqual(await self.exercise([b'x'],[(b'content-length',value)]),400)
        self.assertEqual(await self.exercise([b'x'],[(b'content-length',b'1'),(b'content-length',b'1')]),400)
    async def test_timeout(self):
        self.assertEqual(await self.exercise([b'x'],delay=0.05,timeout=0.01),408)

class BodyGuardConcurrency(unittest.IsolatedAsyncioTestCase):
    """并发名额只算带 body 的请求：面板一次刷新十几个 GET，不该被 503。"""

    def _guard(self, concurrency):
        self.gate = asyncio.Event()

        async def app(scope, receive, send):
            await self.gate.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        return APIBodyGuard(app, concurrency=concurrency)

    async def _one(self, guard, method, path, body=b''):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        await guard({'type': 'http', 'method': method, 'path': path, 'headers': []},
                    receive, send)
        return out[0]['status']

    async def test_parallel_gets_are_not_rejected(self):
        guard = self._guard(4)
        tasks = [asyncio.create_task(self._one(guard, 'GET', '/api/me')) for _ in range(20)]
        await asyncio.sleep(0.05)
        self.gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(results, [200] * 20, '无 body 的 GET 不该占用并发名额')

    async def test_body_requests_beyond_limit_are_rejected(self):
        guard = self._guard(2)
        tasks = [asyncio.create_task(self._one(guard, 'POST', '/api/x', b'{}')) for _ in range(3)]
        await asyncio.sleep(0.05)
        self.gate.set()
        results = sorted(await asyncio.gather(*tasks))
        self.assertEqual(results, [200, 200, 503], '带 body 的请求仍受并发名额约束')

    async def test_default_concurrency_is_generous(self):
        self.assertGreaterEqual(APIBodyGuard(object()).concurrency, 16,
                                '名额太小会让面板刷新大面积 503')


class SchemaContracts(unittest.TestCase):
    def test_reject_unknown_section_field_type(self):
        for value in ({'api_key':'x'},{'server':{'api_key':'x'}},{'pool':{'max_inflite':4}}, {'session_sticky':{'enabled':'false'}},{'pool':{'max_in_flight':True}}):
            with self.assertRaises(ValueError):validate(value)
    def test_preserve_custom_contract(self):
        validate({'pool':{'max_in_flight':6,'idle_weight_per_hour':0.5,'expiring_soon':'168h'},'session_sticky':{'enabled':True,'ttl':'24h'},'prompt':{'mode':'passthrough'}})


class PackageUploadLimitTest(unittest.IsolatedAsyncioTestCase):
    """N39：升级包路径的实际上限必须与声明长度预检一致。

    修复前：main.py 的预检按 `MAX_PACKAGE_BODY_BYTES`(64 MiB) 放行，
    `APIBodyGuard` 仍按默认 16 MiB 边读边数 → 17 MiB 的升级包在第二个关口
    被 413，用户看到"上限 64 MB 却传不上去"。实测对真实 Manager 复现。
    """

    async def _guard_status(self, guard, path, declared, body=b''):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        scope = {'type': 'http', 'path': path, 'method': 'POST',
                 'headers': [(b'content-length', str(declared).encode())]}
        await guard(scope, receive, send)
        return next(m['status'] for m in sent if m['type'] == 'http.response.start')

    async def test_package_path_gets_wider_limit(self):
        from server.main import MAX_API_BODY_BYTES, MAX_PACKAGE_BODY_BYTES, PACKAGE_UPLOAD_PATHS
        # APIBodyGuard 是 ASGI 中间件类：直接实例化后它的 __call__ 是
        # (scope, receive, send)，不能当成 (app, scope, receive, send) 用。
        # 这里传 app=None，只测 __call__ 里的上限判定（在转发前就 return）。
        guard = APIBodyGuard(
            None, maximum=MAX_API_BODY_BYTES,
            path_limits={p: MAX_PACKAGE_BODY_BYTES for p in PACKAGE_UPLOAD_PATHS})
        # 普通路径超 16 MiB → 413
        normal = await self._guard_status(guard, '/api/settings/upstream',
                                          MAX_API_BODY_BYTES + 1)
        self.assertEqual(normal, 413)
        # 升级包路径 17 MiB（>16, <64）→ 必须在声明关就放行（不是 413）
        upload = await self._guard_status(guard, PACKAGE_UPLOAD_PATHS[0], 17 * 1024 * 1024)
        self.assertNotEqual(upload, 413,
                            '17 MiB 升级包仍被 bodyguard 按 16 MiB 拒掉（N39 未修复）')
        # 超过 64 MiB → 仍然 413
        too_big = await self._guard_status(guard, PACKAGE_UPLOAD_PATHS[0],
                                           MAX_PACKAGE_BODY_BYTES + 1)
        self.assertEqual(too_big, 413)

    async def test_guard_is_wired_with_path_limits(self):
        """main.py 必须真的把 path_limits 传进去，不能只改 bodyguard 默认值。"""
        import inspect
        from server import main as main_module
        src = inspect.getsource(main_module)
        self.assertIn('path_limits=', src,
                      'main.py 没有给 APIBodyGuard 传 path_limits，N39 会复发')


class CallerRequestIdTest(unittest.TestCase):
    """N40：调用方的 X-Request-ID 必须**被保存**，但不得成为执行 ID。

    历史：修复前 `RequestAudit(uuid.uuid4().hex, ...)` 无条件生成随机 ID，哪怕
    调用方传了 X-Request-ID 也照样换掉 —— K27/K47 说的"每个 request_id 账本
    闭环"在跨系统意义上无法成立（外部系统拿自己的 ID 永远查不到本端日志）。
    当时的修复是把外部值直接当成执行 ID，结果引入 RV01（外部 ID 成为
    budget_reservations 主键 → 重发即 500、跨 key 共用命名空间、匿名 401 占号）。

    现在的契约是两件事分开：
      * `client_request_id_from_headers()` 只负责**提取并校验**调用方的关联 ID，
        不合法返回 None（本类下面这些用例）；
      * 执行 ID 一律由 `new_request_id()` 生成，两者在中间件里分别落库/回显，
        端到端行为见 `test_request_identity.py`。
    """

    def test_caller_id_is_extracted_as_correlation(self):
        from server.observability import client_request_id_from_headers
        self.assertEqual(client_request_id_from_headers({b'x-request-id': b'k4792875202'}),
                         'k4792875202')

    def test_long_caller_id_rejected(self):
        from server.observability import client_request_id_from_headers
        self.assertIsNone(client_request_id_from_headers({b'x-request-id': b'A' * 300}),
                          '超长取值必须丢弃，不能截断后采纳')

    def test_newline_in_caller_id_rejected(self):
        from server.observability import client_request_id_from_headers
        self.assertIsNone(client_request_id_from_headers({b'x-request-id': b'abc\r\nX-Injected: 1'}))

    def test_space_and_control_chars_rejected(self):
        from server.observability import client_request_id_from_headers
        for bad in (b'abc def', b'abc\x01def', b'', b'\xff\xfe'):
            with self.subTest(bad=bad):
                self.assertIsNone(client_request_id_from_headers({b'x-request-id': bad}))

    def test_allowed_charset_passes(self):
        from server.observability import client_request_id_from_headers
        for good in ('abc-123', 'a.b:c_d', 'A' * 128, '0' * 1):
            with self.subTest(good=good):
                self.assertEqual(
                    client_request_id_from_headers({b'x-request-id': good.encode()}), good)

    def test_missing_header_has_no_client_id(self):
        from server.observability import client_request_id_from_headers
        self.assertIsNone(client_request_id_from_headers({}),
                          '没给头就是没有关联 ID；不得生成一个冒充调用方的值')

    def test_execution_id_is_generated_not_derived(self):
        """执行 ID 的生成函数不接受任何外部输入，且是 32 位 uuid hex。"""
        import inspect
        import re
        from server.observability import new_request_id
        self.assertEqual(list(inspect.signature(new_request_id).parameters), [],
                         '执行 ID 不得由调用方输入派生')
        for _ in range(8):
            got = new_request_id()
            self.assertTrue(re.fullmatch(r'[0-9a-f]{32}', got), got)
