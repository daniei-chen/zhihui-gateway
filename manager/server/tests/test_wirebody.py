"""Exact transformed bytes, admission lifetimes, and pre-forward budget safety."""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

from server import observability, wirebody
from server.routers import gateway


class EncodingTests(unittest.TestCase):
    def test_matches_httpx_json_byte_contract_for_supported_values(self):
        import httpx
        for body in ({}, {'text':'中文 / 🌊\n"\\','items':[None,True,1,1.25]},
                     {'messages':[{'role':'user','content':'x'*200000}]}):
            expected=httpx.Request('POST','http://fixture',json=body).content
            self.assertEqual(wirebody.encode(body,len(expected)),expected)
            with self.assertRaises(wirebody.TooLarge):wirebody.encode(body,len(expected)-1)

    def test_multibyte_and_escape_expansion_use_utf8_bytes_not_character_count(self):
        for text in ('汉'*100, '\x00'*100):
            with self.subTest(text_length=len(text)), self.assertRaises(wirebody.TooLarge):
                wirebody.encode({'text':text},150)

    def test_invalid_json_values_are_rejected_without_payload_echo(self):
        cyclic={};cyclic['private-marker']=cyclic
        for body in ({'private-marker':float('nan')},{'private-marker':float('inf')},
                     {'private-marker':'\ud800'}, {'private-marker':object()}, cyclic):
            with self.assertRaises(wirebody.Invalid)as caught:wirebody.encode(body,1024)
            self.assertNotIn('private-marker',str(caught.exception))

    def test_reservation_is_incremental_and_failure_never_becomes_json_error(self):
        sizes=[]
        wire=wirebody.encode({'text':'x'*150000},200000,reserve=sizes.append)
        self.assertEqual(sizes[-1],len(wire))
        self.assertGreater(len(sizes),3)
        def unavailable(size):raise observability.CapacityError()
        with self.assertRaises(observability.CapacityError):
            wirebody.encode({'text':'x'},1024,reserve=unavailable)


class ForwardPreparationTests(unittest.TestCase):
    def setUp(self):
        self.state=observability.RequestAudit('wire-fixture','/v1/chat/completions','POST',
                                             'openai',time.monotonic())
        self.token=observability.current.set(self.state)
        self.addCleanup(observability.current.reset,self.token)
        self.state.actual_platform='workbuddy'
        self.patches=[patch('server.budget.prepare'),patch('server.budget.forwarded'),
                      patch('server.ledger.forwarding'),
                      patch('server.services.wb2api.load_upstream_config',return_value={})]
        self.prepare,self.forwarded,self.ledger,self.config=[p.start()for p in self.patches]
        for p in self.patches:self.addCleanup(p.stop)

    def test_oversize_is_rejected_before_reservation_or_forwarding_marker(self):
        with patch.object(gateway,'max_forward_body_bytes',return_value=64):
            result=gateway._prepare_forward({'id':1},{'model':'m','text':'x'*100})
        self.assertEqual(result.status_code,413)
        self.assertEqual(json.loads(result.body)['error']['code'],'forward_payload_too_large')
        self.prepare.assert_not_called();self.forwarded.assert_not_called();self.ledger.assert_not_called()
        self.assertEqual(self.state.error_code,'gateway.request.forward_too_large')

    def test_capacity_failure_precedes_budget_and_has_retry_after(self):
        def fail(size):raise observability.CapacityError()
        self.state.reserve_wire_body=fail
        result=gateway._prepare_forward({'id':1},{'model':'m'})
        self.assertEqual(result.status_code,503);self.assertEqual(result.headers['Retry-After'],'1')
        self.prepare.assert_not_called();self.forwarded.assert_not_called()

    def test_same_encoding_is_reused_for_budget_and_actual_wire(self):
        body={'model':'m','messages':[],'max_tokens':100}
        result=gateway._prepare_forward({'id':1},body)
        self.assertEqual(result,self.prepare.call_args.kwargs['encoded_body'])
        self.assertEqual(json.loads(result),body)
        self.forwarded.assert_called_once()

    def test_budget_clamped_output_is_reserialized_not_stale_on_wire(self):
        body={'model':'m','messages':[],'max_tokens':10000}
        self.prepare.side_effect=lambda key,payload,**kw:payload.update(max_tokens=40)
        result=gateway._prepare_forward({'id':1},body)
        self.assertEqual(json.loads(result)['max_tokens'],40)
        self.assertEqual(json.loads(self.prepare.call_args.kwargs['encoded_body'])['max_tokens'],10000)
        self.forwarded.assert_called_once()


class WireCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_transformed_load_and_cancel_release_both_reservations(self):
        env={'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES':'1000','WB_GATEWAY_MIN_BODY_RESERVATION_BYTES':'1'}
        started=asyncio.Event()
        async def app(scope,receive,send):
            await receive()
            observability.current.get().reserve_wire_body(400)
            await send({'type':'http.response.start','status':200,'headers':[]})
            started.set();await asyncio.Event().wait()
        async def receive():return {'type':'http.request','body':b'x'*200,'more_body':False}
        async def send(message):pass
        def scope():return {'type':'http','path':'/v1/chat/completions','method':'POST','headers':[]}
        with patch.dict('os.environ',env),patch('server.db.execute'),patch('server.budget.finish'), \
                patch.object(gateway,'preauthorize_body',return_value=None):
            middleware=observability.RequestAuditMiddleware(app)
            first=asyncio.create_task(middleware(scope(),receive,send))
            await asyncio.wait_for(started.wait(),3)
            try:
                self.assertEqual(middleware.inflight_body_bytes,600)
                messages=[]
                async def capture(message):messages.append(message)
                await middleware(scope(),receive,capture)
                self.assertEqual(messages[0]['status'],503)
                self.assertEqual(middleware.inflight_body_bytes,600)
            finally:
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):await first
        self.assertEqual((middleware.active,middleware.large,middleware.inflight_body_bytes),(0,0,0))

    async def test_retained_input_and_output_share_budget_until_stream_finishes(self):
        env={'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES':'1000','WB_GATEWAY_MIN_BODY_RESERVATION_BYTES':'1'}
        ready,release=asyncio.Event(),asyncio.Event()
        async def app(scope,receive,send):
            await receive()
            reserve=observability.current.get().reserve_wire_body
            reserve(300)
            reserve(300)
            await send({'type':'http.response.start','status':200,'headers':[]})
            ready.set();await release.wait()
            await send({'type':'http.response.body','body':b'done'})
        async def receive():return {'type':'http.request','body':b'x'*200,'more_body':False}
        async def send(message):pass
        with patch.dict('os.environ',env),patch('server.db.execute'),patch('server.budget.finish'), \
                patch.object(gateway,'preauthorize_body',return_value=None):
            middleware=observability.RequestAuditMiddleware(app)
            task=asyncio.create_task(middleware({'type':'http','path':'/v1/chat/completions',
                'method':'POST','headers':[]},receive,send))
            await ready.wait()
            try:self.assertEqual(middleware.inflight_body_bytes,500)
            finally:release.set();await task
        self.assertEqual(middleware.inflight_body_bytes,0)

    async def test_transformed_rejection_releases_all_partial_reservations(self):
        env={'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES':'1000','WB_GATEWAY_MIN_BODY_RESERVATION_BYTES':'1'}
        async def app(scope,receive,send):
            await receive()
            reserve=observability.current.get().reserve_wire_body
            reserve(300)
            with self.assertRaises(observability.CapacityError):reserve(901)
            await send({'type':'http.response.start','status':503,'headers':[]})
            await send({'type':'http.response.body','body':b'capacity'})
        async def receive():return {'type':'http.request','body':b'x'*200,'more_body':False}
        async def send(message):pass
        with patch.dict('os.environ',env),patch('server.db.execute'),patch('server.budget.finish'), \
                patch.object(gateway,'preauthorize_body',return_value=None):
            middleware=observability.RequestAuditMiddleware(app)
            await middleware({'type':'http','path':'/v1/chat/completions',
                'method':'POST','headers':[]},receive,send)
        self.assertEqual(middleware.inflight_body_bytes,0)
        self.assertEqual(middleware.capacity_rejections,1)


class ProtocolWireTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        from server import config,db,budget,budgetowner,keysvc
        self.config,self.db,self.budgetowner=config,db,budgetowner
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.original=(config.DB_PATH,config.USERS_FILE,config.STATIC_DIR)
        root=Path(self.tmp.name)
        config.DB_PATH,config.USERS_FILE,config.STATIC_DIR=root/'db',root/'users',root/'no-static'
        config.USERS_FILE.write_text(json.dumps({'secret':'synthetic-wire-secret','users':[
            {'username':'admin','role':'admin','sv':1,'pwd_hash':'synthetic'}]}))
        db._conn=None;db.connect()
        budget.initialize()
        self.token=keysvc.create_key(name='wire-only-fixture')['key']
        from server.main import app
        self.client=TestClient(app)
        self.addCleanup(self.restore)

    def restore(self):
        self.client.close();self.budgetowner.close()
        if self.db._conn is not None:self.db._conn.close()
        self.db._conn=None
        self.config.DB_PATH,self.config.USERS_FILE,self.config.STATIC_DIR=self.original

    def payload(self,protocol,stream):
        body={'model':'m','max_tokens':10,'stream':stream}
        if protocol=='responses':body['input']='hello'
        else:body['messages']=[{'role':'user','content':'hello'}]
        return body

    def test_all_protocols_reject_transformed_body_without_provider_or_budget_unknown(self):
        for protocol,path in (('chat','/v1/chat/completions'),('messages','/v1/messages'),
                              ('responses','/v1/responses')):
            for stream in (False,True):
                with self.subTest(protocol=protocol,stream=stream), \
                        patch.object(gateway,'max_forward_body_bytes',return_value=32), \
                        patch.object(self.config,'http_client',side_effect=AssertionError('provider invoked')):
                    response=self.client.post(path,json=self.payload(protocol,stream),
                        headers={'Authorization':'Bearer '+self.token})
                    self.assertEqual(response.status_code,413,response.text)
                    self.assertEqual(response.json()['error']['code'],'forward_payload_too_large')
                    if protocol=='messages':self.assertEqual(response.json()['type'],'error')
                    rid=response.headers['x-request-id']
                    self.assertIsNone(self.db.query_one(
                        'SELECT state FROM budget_reservations WHERE request_id=?',(rid,)))
                    outcome=self.db.query_one('SELECT error_code FROM request_outcomes WHERE request_id=?',(rid,))
                    self.assertEqual(outcome['error_code'],'gateway.request.forward_too_large')

    def test_protocol_conversion_expansion_is_checked_after_small_input(self):
        from server.routers import anthropic,responses
        for protocol,path,module,name in (
                ('messages','/v1/messages',anthropic,'to_openai_request'),
                ('responses','/v1/responses',responses,'to_chat_request')):
            original=getattr(module,name)
            def expand(*args,**kwargs):
                value=original(*args,**kwargs)
                value['messages'].append({'role':'system','content':'x'*2000})
                return value
            with self.subTest(protocol=protocol),patch.object(module,name,side_effect=expand), \
                    patch.object(gateway,'max_forward_body_bytes',return_value=1500), \
                    patch.object(self.config,'http_client',side_effect=AssertionError('provider invoked')):
                body=self.payload(protocol,False)
                self.assertLess(len(json.dumps(body)),1500)
                response=self.client.post(path,json=body,headers={'Authorization':'Bearer '+self.token})
                self.assertEqual(response.status_code,413,response.text)
                self.assertEqual(response.json()['error']['code'],'forward_payload_too_large')

    def test_anthropic_capacity_error_preserves_protocol_and_retry_contract(self):
        def unavailable(size):raise observability.CapacityError()
        original=wirebody.encode
        def encode(*args,**kwargs):
            kwargs['reserve']=unavailable
            return original(*args,**kwargs)
        with patch.object(wirebody,'encode',side_effect=encode), \
                patch.object(self.config,'http_client',side_effect=AssertionError('provider invoked')):
            response=self.client.post('/v1/messages',json=self.payload('messages',False),
                headers={'Authorization':'Bearer '+self.token})
        self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(response.headers['Retry-After'],'1')
        self.assertEqual(response.json()['type'],'error')
        self.assertEqual(response.json()['error']['type'],'api_error')

    def test_daily_budget_clamp_changes_actual_wire_and_client_headers(self):
        import httpx
        from server import keysvc
        token=keysvc.create_key(name='daily-wire-fixture',daily_token_quota=5000)['key']
        sent=[]
        def respond(request):
            sent.append(json.loads(request.content))
            self.assertEqual(request.headers['Content-Type'],'application/json')
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'ok'}}],
                'usage':{'prompt_tokens':2,'completion_tokens':1}})
        body=self.payload('chat',False);body['max_tokens']=10000
        with patch.object(self.config,'http_client',lambda *a,**k:
                httpx.AsyncClient(transport=httpx.MockTransport(respond))):
            response=self.client.post('/v1/chat/completions',json=body,
                headers={'Authorization':'Bearer '+token})
        self.assertEqual(response.status_code,200,response.text)
        effective=int(response.headers['x-manager-max-tokens-effective'])
        self.assertLess(effective,10000)
        self.assertEqual(sent[0]['max_tokens'],effective)

    def test_successful_wire_is_the_validated_http_payload_for_each_protocol(self):
        import httpx
        sent=[];checked=[]
        original=wirebody.encode
        def encode(*args,**kwargs):
            value=original(*args,**kwargs);checked.append(value);return value
        reply={'id':'fixture','choices':[{'message':{'role':'assistant','content':'ok'},
                'finish_reason':'stop'}],'usage':{'prompt_tokens':2,'completion_tokens':1}}
        def respond(request):
            self.assertEqual(request.headers['Content-Type'],'application/json')
            self.assertEqual(request.content,checked[-1])
            sent.append(request.content)
            payload=json.loads(request.content)
            if payload.get('stream'):
                data={'choices':[{'delta':{'content':'ok'},'finish_reason':'stop'}],
                      'usage':reply['usage']}
                return httpx.Response(200,headers={'content-type':'text/event-stream'},
                    content=('data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n').encode())
            return httpx.Response(200,json=reply)
        for protocol,path in (('chat','/v1/chat/completions'),('messages','/v1/messages'),
                              ('responses','/v1/responses')):
            for stream in (False,True):
                with self.subTest(protocol=protocol,stream=stream),patch.object(wirebody,'encode',side_effect=encode),patch.object(self.config,'http_client',
                        lambda *a,**k:httpx.AsyncClient(transport=httpx.MockTransport(respond))):
                    response=self.client.post(path,json=self.payload(protocol,stream),
                        headers={'Authorization':'Bearer '+self.token})
                    self.assertEqual(response.status_code,200,response.text)
                    actual=sent[-1]
                    self.assertEqual(actual,wirebody.encode(json.loads(actual),len(actual)))
        self.assertEqual(len(sent),6)

    def test_gzip_clients_reach_each_real_protocol_once_with_uncompressed_validated_output(self):
        import gzip
        import httpx
        sent=[]
        def respond(request):
            self.assertNotIn('content-encoding',request.headers)
            payload=json.loads(request.content)
            sent.append(payload)
            usage={'prompt_tokens':2,'completion_tokens':1,'credit':0}
            if payload.get('stream'):
                value={'choices':[{'delta':{'content':'ok'},'finish_reason':'stop'}],'usage':usage}
                return httpx.Response(200,headers={'content-type':'text/event-stream'},
                    content=('data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n').encode())
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'ok'},
                                                     'finish_reason':'stop'}],'usage':usage})
        for protocol,path in (('chat','/v1/chat/completions'),('messages','/v1/messages'),
                              ('responses','/v1/responses')):
            for stream in (False,True):
                body=self.payload(protocol,stream)
                if protocol=='responses':body['input']='hello'*10000
                else:body['messages'][0]['content']='hello'*10000
                wire=gzip.compress(json.dumps(body).encode(),compresslevel=1)
                with self.subTest(protocol=protocol,stream=stream),patch.object(self.config,'http_client',
                        lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(respond))):
                    response=self.client.post(path,content=wire,headers={'Authorization':'Bearer '+self.token,
                        'Content-Type':'application/json','Content-Encoding':'gzip'})
                    self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(sent),6)

    def test_compressed_expansion_is_refused_before_any_real_protocol_or_budget_forward(self):
        import gzip
        body=gzip.compress(b'{"padding":"'+b'x'*100000+b'"}')
        for path in ('/v1/chat/completions','/v1/messages','/v1/responses'):
            with self.subTest(path=path),patch.object(gateway,'max_body_bytes',return_value=10000), \
                 patch.object(self.config,'http_client',side_effect=AssertionError('provider invoked')):
                response=self.client.post(path,content=body,headers={'Authorization':'Bearer '+self.token,
                    'Content-Type':'application/json','Content-Encoding':'gzip'})
                self.assertEqual(response.status_code,413,response.text)
                self.assertIsNone(self.db.query_one('SELECT state FROM budget_reservations WHERE request_id=?',
                    (response.headers['x-request-id'],)))


if __name__=='__main__':unittest.main()
