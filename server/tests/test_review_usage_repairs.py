"""R1: malformed supplier evidence must not become free usage or poison replay."""
from contextlib import contextmanager
import json
import math
import time
import unittest
from unittest import mock

from server import budget, budgetowner, config, db, keysvc, ledgerreplay, metering, observability, operational
from server.routers import gateway
from server.tests._db_fixture import isolate_database


class UsageRepairs(unittest.TestCase):
    def setUp(self):
        root = isolate_database(self)
        for name, value in {'DATA_DIR': root, 'USERS_FILE': root/'users.json'}.items():
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        config.USERS_FILE.write_text(json.dumps({'secret': 'synthetic-'+'x'*64,
            'role_policy_version': 2, 'users': [{'username': 'admin', 'role': 'admin', 'sv': 1}], 'api_keys': []}))
        for name, value in {'ledger_write_healthy': True, 'ledger_spool_failed': False}.items():
            patch = mock.patch.object(operational, name, value)
            patch.start(); self.addCleanup(patch.stop)
        self.addCleanup(budgetowner.close)
        budget.initialize()

    @contextmanager
    def request(self, request_id, *, forwarded=True):
        key = keysvc.resolve(keysvc.create_key('synthetic-review-repair', quota=100000)['key'])
        state = observability.RequestAudit(request_id, '/v1/chat/completions', 'POST', 'openai', time.monotonic())
        state.actual_platform = 'workbuddy'
        token = observability.current.set(state)
        try:
            budget.prepare(key, {'model': 'synthetic', 'messages': [], 'max_tokens': 16})
            if forwarded:
                budget.forwarded()
            yield key, state
        finally:
            observability.current.reset(token)

    def record(self, key, usage, **kwargs):
        gateway._record(key, '127.0.0.1', 'synthetic', 'cn:synthetic', 200,
            observability.usage_int(usage.get('prompt_tokens')),
            observability.usage_int(usage.get('completion_tokens')),
            1, 'synthetic-review-repair', None, False, usage=usage, **kwargs)

    def assert_unknown(self, request_id):
        self.assertEqual(db.query_one('SELECT usage_complete FROM request_logs WHERE request_id=?', (request_id,))[0], 0)
        self.assertEqual(db.query_one('SELECT state FROM budget_reservations WHERE request_id=?', (request_id,))[0], 'unknown')
        self.assertEqual(ledgerreplay.backlog(), 0)
        self.assertEqual(ledgerreplay.dead_letters(), 0)

    def test_invalid_tokens_preserve_unknown_in_real_record_and_budget(self):
        values = [None, 'invalid', True, False, -1, 1.5, 1.0, math.inf, math.nan,
                  '1.5', '1e5', 2**63, [1], {}]
        for number, value in enumerate(values):
            for field in ('prompt_tokens', 'completion_tokens'):
                with self.subTest(field=field, value=repr(value)):
                    rid = f'bad-token-{field}-{number}'
                    with self.request(rid) as (key, _state):
                        self.record(key, {'prompt_tokens': 1, 'completion_tokens': 2, field: value})
                    self.assert_unknown(rid)

    def test_valid_zero_and_string_integers_are_known(self):
        for number, (pt, ct) in enumerate([(0, 0), (2, 3), ('0', '0'), ('2', '3')]):
            rid = f'valid-usage-{number}'
            with self.request(rid) as (key, _state):
                self.record(key, {'prompt_tokens': pt, 'completion_tokens': ct})
            row = db.query_one('SELECT state,charged_tokens FROM budget_reservations WHERE request_id=?', (rid,))
            self.assertEqual(tuple(row), ('settled', int(pt)+int(ct)))
            self.assertEqual(db.query_one('SELECT usage_complete FROM request_logs WHERE request_id=?', (rid,))[0], 1)

    def test_bad_credit_never_creates_poison_receipts(self):
        values = ['Infinity', '-Infinity', 'NaN', math.inf, math.nan, -1,
                  True, '9e999', 10**400, metering.MAX_CREDIT_VALUE+1, {}, []]
        for field in ('credit', 'credits'):
            for number, value in enumerate(values):
                with self.subTest(field=field, value=repr(value)):
                    rid = f'bad-credit-{field}-{number}'
                    usage = {'prompt_tokens': 1, 'completion_tokens': 2, field: value}
                    self.assertIsNone(gateway._usage_credit(usage))
                    with self.request(rid) as (key, _state):
                        self.record(key, usage)
                    self.assert_unknown(rid)
                    self.assertIsNone(db.query_one('SELECT credit FROM request_logs WHERE request_id=?', (rid,))[0])
                    self.assertFalse(ledgerreplay.admission_block()['blocked'])

    def test_explicit_credit_and_cache_fields_are_also_validated(self):
        with self.request('explicit-credit') as (key, _state):
            self.record(key, {'prompt_tokens': 1, 'completion_tokens': 2,
                             'prompt_cache_hit_tokens': math.inf}, credit=math.inf)
        self.assert_unknown('explicit-credit')
        self.assertIsNone(db.query_one('SELECT cache_hit_tokens FROM request_logs WHERE request_id=?', ('explicit-credit',))[0])

    def test_invalid_usage_spool_replays_unknown_exactly_once(self):
        for forwarded in (False, True):
            rid = f'invalid-replay-{forwarded}'
            with self.request(rid, forwarded=forwarded) as (key, _state):
                with mock.patch.object(db, 'add_request_log', side_effect=OSError('synthetic disk failure')):
                    self.record(key, {'prompt_tokens': None, 'completion_tokens': 'bad', 'credit': 'Infinity'})
                self.assertEqual(ledgerreplay.backlog(), 1)
            result = ledgerreplay.replay_batch()
            self.assertEqual(result['applied'], 1, result)
            self.assert_unknown(rid)
            self.assertEqual(ledgerreplay.replay_batch()['applied'], 0)
            self.assertEqual(db.query_one('SELECT COUNT(*) FROM usage_events WHERE request_id=?', (rid,))[0], 1)
            self.assertFalse(ledgerreplay.admission_block()['blocked'])

    def test_real_dead_letter_guard_is_retained(self):
        from pathlib import Path
        directory = Path(ledgerreplay.spool_dir())/'dead'
        directory.mkdir(parents=True, exist_ok=True)
        (directory/'synthetic-corrupt.json').write_text('{"synthetic":"unresolved"}')
        self.assertTrue(ledgerreplay.admission_block()['blocked'])

    def test_partial_usage_does_not_erase_valid_reported_evidence(self):
        with self.request('partial-valid') as (key, _state):
            self.record(key, {'prompt_tokens': 7, 'completion_tokens': 'bad', 'credit': '0.25'})
        self.assert_unknown('partial-valid')
        row = db.query_one('SELECT prompt_tokens,completion_tokens,credit FROM request_logs WHERE request_id=?', ('partial-valid',))
        self.assertEqual(tuple(row), (7, 0, 0.25))

    def test_legacy_partial_caller_counts_survive_credit_override_without_claiming_complete(self):
        with self.request('partial-caller') as (key, _state):
            gateway._record(key,'127.0.0.1','synthetic','cn:synthetic',200,7,3,1,None,None,False,credit=0.25)
        row=db.query_one('SELECT prompt_tokens,completion_tokens,usage_complete FROM request_logs WHERE request_id=?',('partial-caller',))
        self.assertEqual(tuple(row),(7,3,0))
        self.assertEqual(tuple(db.query_one('SELECT state,charged_tokens FROM budget_reservations WHERE request_id=?',('partial-caller',))),('unknown',10))

    def test_parser_and_stored_complete_flag_agree(self):
        for usage in ({'prompt_tokens': 0, 'completion_tokens': 0},
                      {'prompt_tokens': 1, 'completion_tokens': 2, 'credit': 'Infinity'},
                      {'prompt_tokens': '1', 'completion_tokens': '2'},
                      {'prompt_tokens': False, 'completion_tokens': 2}):
            with self.subTest(usage=repr(usage)):
                measured = metering.parse_usage(usage)
                self.assertEqual(measured.complete, usage in (
                    {'prompt_tokens': 0, 'completion_tokens': 0},
                    {'prompt_tokens': '1', 'completion_tokens': '2'}))


class UsageHTTPRepairs(unittest.IsolatedAsyncioTestCase):
    setUp = UsageRepairs.setUp

    async def test_all_three_protocols_preserve_unknown_usage_streaming_and_nonstreaming(self):
        import httpx
        from fastapi import FastAPI
        from server import platformpolicy
        from server.routers import anthropic, responses
        app = FastAPI()
        for router in (gateway.router, anthropic.router, responses.router):
            app.include_router(router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        calls = []
        usage = {'prompt_tokens': None, 'completion_tokens': 'invalid', 'credit': 'Infinity'}
        def provider(request):
            body = json.loads(request.content)
            calls.append(body)
            if body.get('stream'):
                frames = [{'choices':[{'index':0,'delta':{'content':'synthetic'},'finish_reason':None}]},
                          {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':usage}]
                raw = ''.join('data: '+json.dumps(frame)+'\n\n' for frame in frames)+'data: [DONE]\n\n'
                return httpx.Response(200,content=raw,headers={'content-type':'text/event-stream'})
            return httpx.Response(200,json={'id':'synthetic','model':'synthetic','choices':[
                {'index':0,'message':{'role':'assistant','content':'synthetic'},'finish_reason':'stop'}],'usage':usage})
        def client(*args,**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(provider))
        with mock.patch.object(config,'http_client',side_effect=client), mock.patch.object(gateway,'RATE_MAX_PER_MIN',0):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as caller:
                for protocol in ('chat/completions','messages','responses'):
                    for stream in (False,True):
                        with self.subTest(protocol=protocol,stream=stream):
                            made = keysvc.create_key('synthetic-http-review',quota=100000)
                            body={'model':'synthetic','stream':stream}
                            body.update({'input':'hi','max_output_tokens':8} if protocol=='responses' else
                                        {'messages':[{'role':'user','content':'hi'}],'max_tokens':8})
                            result = await caller.post('/v1/'+protocol,json=body,headers={'authorization':'Bearer '+made['key']})
                            self.assertEqual(result.status_code,200,result.text)
                            self.assertIn('synthetic',result.text)
                            row=db.query_one('SELECT request_id,usage_complete,credit FROM request_logs WHERE key_id=?',(made['id'],))
                            self.assertIsNotNone(row)
                            self.assertEqual(row['usage_complete'],0)
                            self.assertIsNone(row['credit'])
                            self.assertEqual(db.query_one('SELECT state FROM budget_reservations WHERE request_id=?',(row['request_id'],))[0],'unknown')
        self.assertEqual(len(calls),6)
        self.assertEqual(ledgerreplay.backlog(),0)
        self.assertEqual(ledgerreplay.dead_letters(),0)
