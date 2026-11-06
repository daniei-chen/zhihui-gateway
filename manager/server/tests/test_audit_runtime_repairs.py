"""Protocol, lifecycle and resource regressions, without live provider calls."""
import asyncio
import gzip
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.tests.audit_fixtures import state


def delta(**value):
    finish=value.pop('finish',None)
    return {'choices':[{'index':0,'delta':value,'finish_reason':finish}]}


def objects(events):
    return [json.loads(line[6:]) for event in events for line in event.decode().splitlines() if line.startswith('data: ')]


def test_function_fragments_and_namespace_have_one_consistent_identity():
    from server.routers import responses as r
    bridge=r._ToolBridge([{'type':'namespace','name':name,'tools':[{'type':'function','name':'get_weather'}]} for name in ('alpha','beta')])
    tr=r._StreamTranslator('synthetic','resp_synthetic',bridge=bridge)
    events=tr.feed(delta(tool_calls=[{'index':0,'id':'call_synthetic','function':{'name':'beta__get_','arguments':'{'}}]))
    events+=tr.feed(delta(tool_calls=[{'index':0,'function':{'name':'weather','arguments':'"city":"x"}'}}],finish='tool_calls'))
    events=objects(events)
    added=next(x['item'] for x in events if x['type']=='response.output_item.added')
    done=next(x['item'] for x in events if x['type']=='response.output_item.done')
    terminal=events[-1]['response']['output'][0]
    for value in (added,done,terminal):
        assert value['name']=='get_weather' and value['namespace']=='beta'
        assert value['call_id']=='call_synthetic'
    assert done['arguments']=='{"city":"x"}'


@pytest.mark.parametrize('names',[('Bash',),('Ba','sh'),('B','Bash'),('Bash','Bash'),('B',),('Bash','ash')])
def test_final_tool_name_must_match_current_or_discovered_declaration(names):
    from server.routers import responses as r
    bridge=r._ToolBridge([{'type':'function','name':'Bash','parameters':{'type':'object'}}])
    tr=r._StreamTranslator('synthetic','resp_declared',bridge=bridge)
    events=[]
    for index,name in enumerate(names):
        events+=tr.feed(delta(tool_calls=[{'index':0,**({'id':'call_declared'} if index==0 else {}),
            'function':{'name':name,'arguments':'{}' if index==0 else ''}}],
            **({'finish':'tool_calls'} if index==len(names)-1 else {})))
    decoded=objects(events)
    valid=names not in [('B',),('Bash','ash')]
    assert (decoded[-1]['type']=='response.completed') is valid
    assert any(x['type']=='response.output_item.added' for x in decoded) is valid
    data={'choices':[{'message':{'tool_calls':[{'id':'call_declared','function':{'name':names[-1] if len(names)==1 else 'Bash','arguments':'{}'}}]},'finish_reason':'tool_calls'}]}
    if names==('B',):
        with pytest.raises(r.ToolArgumentsError):r.to_responses_object(data,'synthetic','resp_declared',bridge=bridge)


@pytest.mark.parametrize('content',[
    ['malformed block'],
    [{'type':'tool_result','tool_use_id':'synthetic','content':[{'type':'document','source':{'type':'text','data':'must not disappear'}}]}],
    [{'type':'tool_result','tool_use_id':'synthetic','content':{'type':'document','source':{'type':'text','data':'must not become repr'}}}],
])
def test_nested_or_malformed_document_input_is_rejected(content):
    from server.routers import anthropic
    with pytest.raises(ValueError,match='Unsupported input content'):
        anthropic.to_openai_request({'model':'synthetic','messages':[{'role':'user','content':content}]})


@pytest.mark.parametrize('path,payload',[
    ('/v1/responses',{'input':[{'type':'input_file','file_data':'synthetic-attachment-value'}]}),
    ('/v1/responses',{'input':[{'role':'user','content':[{'type':'input_file','file_data':'synthetic-attachment-value'}]}]}),
    ('/v1/messages',{'messages':[{'role':'user','content':[{'type':'document','source':{'type':'text','data':'synthetic-attachment-value'}}]}]}),
    ('/v1/messages',{'messages':[{'role':'user','content':[{'type':'tool_result','tool_use_id':'synthetic','content':[{'type':'document','source':{'type':'text','data':'synthetic-attachment-value'}}]}]}]}),
    ('/v1/responses',{'input':[{'type':'agent_message','content':[{'type':'encrypted_content','encrypted_content':'gAAAAAsynthetic-attachment-value'}]}]}),
    ('/v1/responses',{'input':[{'role':'user','content':[{'type':'input_image','image_url':{}}]}]}),
    ('/v1/responses',{'input':[{'role':'user','content':['synthetic-attachment-value']}]}),
    ('/v1/messages',{'messages':[{'role':'user','content':[{'type':'image','source':{'type':'unknown','data':'synthetic-attachment-value'}}]}]}),
    ('/v1/messages',{'messages':[{'role':'user','content':[{'type':'tool_result','tool_use_id':'synthetic','content':[{'type':'image','source':{'type':'unknown','data':'synthetic-attachment-value'}}]}]}]}),
])
def test_unsupported_content_has_stable_code_and_bounded_schema_path_before_spend(state,monkeypatch,path,payload):
    from server import config,keysvc,observability
    from server.routers import responses,anthropic
    def forbidden(*args,**kwargs):raise AssertionError('unsupported content reached provider')
    monkeypatch.setattr(config,'http_client',forbidden)
    app=FastAPI();app.include_router(responses.router);app.include_router(anthropic.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    key=keysvc.create_key('synthetic unsupported content')['key']
    body={'model':'m',**payload}
    if path.endswith('messages'):body['max_tokens']=16
    with TestClient(app) as client:
        response=client.post(path,headers={'authorization':'Bearer '+key},json=body)
    assert response.status_code==400,response.text
    error=response.json()['error']
    assert error['code']=='unsupported_content_type'
    assert '[]' in error['message'] and len(error['message'])<256
    assert 'synthetic-attachment-value' not in response.text


@pytest.mark.parametrize('protocol',['responses','messages'])
def test_output_limit_failure_envelope_is_separately_bounded(protocol):
    from server.outputlimits import output_limit_failure,FAILURE_ENVELOPE_BYTES
    payload=output_limit_failure(protocol,response_id='x'*100000,created_at=1,
        model='模'*100000,request_id='y'*100000,upstream_request_id='z'*100000)
    assert len(('event: response.failed\ndata: '+json.dumps(payload,ensure_ascii=False)+'\n\n').encode())<FAILURE_ENVELOPE_BYTES
    error=payload['response']['error'] if protocol=='responses' else payload['error']
    assert error['code']=='upstream_output_limit'


@pytest.mark.parametrize('shared',[False,True])
@pytest.mark.parametrize('terminal',['complete','cancel','error'])
def test_output_reservation_is_global_or_cohort_wide_and_released_after_terminal(state,monkeypatch,shared,terminal):
    from server import config,keysvc,observability
    from server.routers import gateway
    from server.sharedadmission import Coordinator
    monkeypatch.setenv('WB_MAX_OUTPUT_BYTES','65536')
    monkeypatch.setenv('WB_GATEWAY_QUEUE_WAIT_MS','0')
    monkeypatch.setenv('WB_KEY_CONCURRENCY','4')
    monkeypatch.setenv('WB_KEY_CONCURRENCY_QUOTA','1')
    monkeypatch.setattr(config,'SHARED_ADMISSION',False)
    async def run():
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def handler(request):
            calls.append(True)
            if len(calls)==1:
                entered.set();await release.wait()
                if terminal=='error':raise httpx.RemoteProtocolError('synthetic accepted disconnect')
            return httpx.Response(200,json={'id':'synthetic','choices':[{'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'credit':0}})
        monkeypatch.setattr(config,'http_client',lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        app=FastAPI();app.include_router(gateway.router)
        coordinators=[Coordinator(state/'output-cohort.db',{'active':8,'large':8,'bytes':200000,'preauth':8,
            'queue':8,'queue_per_key':8,'key_default':4,'key_quota':1}) for _ in range(2)] if shared else []
        first_wrapper=observability.RequestAuditMiddleware(app,shared=coordinators[0] if shared else None)
        second_wrapper=observability.RequestAuditMiddleware(app,shared=coordinators[1]) if shared else first_wrapper
        for wrapper in (first_wrapper,second_wrapper):
            wrapper.maximum_body_bytes=200000;wrapper.minimum_body_bytes=64
        key=keysvc.create_key('synthetic output capacity')['key']
        options={'headers':{'authorization':'Bearer '+key},'json':{'model':'m','messages':[{'role':'user','content':'hi'}]}}
        first=None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first_wrapper),base_url='http://synthetic') as a,\
                       httpx.AsyncClient(transport=httpx.ASGITransport(app=second_wrapper),base_url='http://synthetic') as b:
                first=asyncio.create_task(a.post('/v1/chat/completions',**options))
                await asyncio.wait_for(entered.wait(),3)
                rejected=await b.post('/v1/chat/completions',**options)
                assert rejected.status_code==503,rejected.text
                assert len(calls)==1  # memory refused BEFORE supplier dispatch/spend
                if terminal=='cancel':
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):await first
                else:
                    release.set();done=await first
                    assert done.status_code==(200 if terminal=='complete' else 502),done.text
                for wrapper in (first_wrapper,second_wrapper):
                    assert wrapper.active==0 and wrapper.inflight_body_bytes==0
                after=await b.post('/v1/chat/completions',**options)
                assert after.status_code==200,after.text
                assert len(calls)==2
        finally:
            release.set()
            if first is not None and not first.done():first.cancel();await asyncio.gather(first,return_exceptions=True)
            for coordinator in coordinators:coordinator.close()
    asyncio.run(run())


def test_response_budget_protocol_cannot_silently_join_legacy_coordinator(state):
    import sqlite3
    from server.sharedadmission import Coordinator,Unavailable
    policy={'active':8,'large':8,'bytes':200000,'preauth':8,'queue':8,'queue_per_key':8,'key_default':4,'key_quota':1}
    path=state/'legacy-cohort.db'
    coordinator=Coordinator(path,policy);coordinator.close()
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE admission_policy SET value=? WHERE id=1',(json.dumps(policy,sort_keys=True,separators=(',',':')),))
    with pytest.raises(Unavailable,match='policy_(changed|conflict)'):Coordinator(path,policy)


def test_thinking_text_transitions_close_previous_block_before_starting_next():
    from server.routers import anthropic
    tr=anthropic._StreamTranslator('synthetic',thinking=True)
    events=[]
    for frame in [delta(reasoning_content='first'),delta(content='one'),
                  delta(reasoning_content='second'),delta(content='two')]:events+=tr.feed(frame)
    events+=tr.finish('stop')
    active=None;started=[]
    for event in objects(events):
        if event['type']=='content_block_start':
            assert active is None
            active=event['index'];started.append(active)
        elif event['type']=='content_block_delta':assert event['index']==active
        elif event['type']=='content_block_stop':
            assert event['index']==active
            active=None
    assert active is None and started==[0,1,2,3]


@pytest.mark.parametrize('finish',['length','content_filter'])
def test_truncated_messages_tool_turn_never_reports_executable_success(finish):
    from server.routers import anthropic
    tr=anthropic._StreamTranslator('synthetic')
    events=tr.feed(delta(tool_calls=[{'index':0,'id':'synthetic','function':{'name':'run','arguments':'{}'}}]))
    events+=tr.finish(finish)
    decoded=objects(events)
    assert any(e['type']=='error' and e['error']['code']=='invalid_upstream_tool_stream' for e in decoded)
    assert not any(e['type']=='message_stop' for e in decoded)
    assert not any(e['type']=='message_delta' and e['delta']['stop_reason']=='tool_use' for e in decoded)
    assert tr.failed


@pytest.mark.parametrize('arguments',['{"command":"first","command":"second"}','{"value":NaN}','[]'])
def test_streamed_messages_tools_share_nonstream_strict_json_validation(arguments):
    from server.routers import anthropic
    tr=anthropic._StreamTranslator('synthetic')
    events=tr.feed(delta(tool_calls=[{'index':0,'id':'synthetic','function':{'name':'run','arguments':arguments}}]))
    events+=tr.finish('tool_calls')
    decoded=objects(events)
    assert any(e['type']=='error' and e['error']['code']=='invalid_upstream_tool_stream' for e in decoded)
    assert not any(e['type']=='message_stop' for e in decoded)


def test_late_function_identity_is_preserved_and_missing_identity_is_never_fabricated():
    from server.routers import responses as r
    tr=r._StreamTranslator('synthetic','resp_late')
    frames=tr.feed(delta(tool_calls=[{'index':0,'function':{'name':'run','arguments':'{'}}]))
    frames+=tr.feed(delta(tool_calls=[{'index':0,'id':'call_late','function':{'arguments':'}'}}],finish='tool_calls'))
    added=next(x['item'] for x in objects(frames) if x['type']=='response.output_item.added')
    assert added['call_id']=='call_late'
    tr=r._StreamTranslator('synthetic','resp_missing')
    frames=objects(tr.feed(delta(tool_calls=[{'index':0,'function':{'name':'run','arguments':'{}'}}],finish='tool_calls')))
    assert frames[-1]['type']=='response.failed'
    assert not any(x['type']=='response.output_item.added' for x in frames)


def test_nonstream_responses_rejects_invalid_or_missing_tool_identity():
    from server.routers import responses as r
    for call in [{'function':{'name':'run','arguments':'{}'}},
                 {'id':'call_fixture','function':{'name':'run','arguments':'{"partial":'}}]:
        with pytest.raises(r.ToolArgumentsError):
            r.to_responses_object({'choices':[{'message':{'tool_calls':[call]},'finish_reason':'tool_calls'}]},'synthetic','resp_synthetic')


@pytest.mark.parametrize('protocol',['responses','anthropic'])
def test_small_frames_cannot_accumulate_unbounded_output(monkeypatch,protocol):
    from server.routers import responses,anthropic
    from server.outputlimits import OutputLimitExceeded
    monkeypatch.setenv('WB_MAX_OUTPUT_BYTES','65536')
    tr=responses._StreamTranslator('synthetic','resp_synthetic') if protocol=='responses' else anthropic._StreamTranslator('synthetic',thinking=True)
    with pytest.raises(OutputLimitExceeded):
        for _ in range(20):tr.feed(delta(content='x'*8192))
    assert len(tr.text_buf)<65536 if protocol=='responses' else True


@pytest.mark.parametrize('arguments',['{"partial":','[]','1','{"n":NaN}','{"a":1,"a":2}'])
def test_nonstream_tools_never_invent_executable_input(arguments):
    from server.routers import anthropic
    with pytest.raises(ValueError):
        anthropic.to_anthropic_response({'choices':[{'message':{'tool_calls':[
            {'id':'call_synthetic','function':{'name':'run','arguments':arguments}}]},'finish_reason':'tool_calls'}]},'synthetic')


def test_thinking_signatures_cover_only_their_own_blocks():
    from server.routers import responses,anthropic
    tr=anthropic._StreamTranslator('synthetic',thinking=True)
    frames=[]
    for value in [delta(reasoning_content='first'),delta(content='text'),delta(reasoning_content='second'),delta(finish='stop')]:
        frames+=tr.feed(value)
    signatures=[x['delta']['signature'] for x in objects(frames) if x.get('delta',{}).get('type')=='signature_delta']
    assert [responses._decode_credential(x) for x in signatures]==['first','second']
    restored=anthropic.to_openai_request({'model':'synthetic','messages':[{'role':'assistant','content':[
        {'type':'thinking','thinking':'truncated display','signature':signature} for signature in signatures]}]})
    assert restored['messages'][0]['reasoning_content']=='firstsecond'


@pytest.mark.parametrize('protocol',['responses','anthropic'])
def test_unsupported_attachments_are_rejected_before_dispatch(state,monkeypatch,protocol):
    from server import config,keysvc,observability
    from server.routers import gateway,responses,anthropic
    app=FastAPI();app.include_router(gateway.router);app.include_router(responses.router);app.include_router(anthropic.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    calls=[]
    def forbidden(*a,**kw):calls.append(True);raise AssertionError('unsupported request reached HTTP dispatch')
    monkeypatch.setattr(config,'http_client',forbidden)
    key=keysvc.create_key('synthetic-attachments')['key']
    if protocol=='responses':
        path='/v1/responses';body={'model':'m','input':[{'role':'user','content':[{'type':'input_text','text':'read'},{'type':'input_file','file_data':'synthetic-only'}]}]}
    else:
        path='/v1/messages';body={'model':'m','max_tokens':16,'messages':[{'role':'user','content':[{'type':'text','text':'read'},{'type':'document','source':{'type':'text','data':'synthetic-only'}}]}]}
    with TestClient(app) as client:
        result=client.post(path,headers={'authorization':'Bearer '+key},json=body)
    assert result.status_code==400,result.text
    assert 'Unsupported input' in result.text
    assert 'synthetic-only' not in result.text
    assert not calls


@pytest.mark.parametrize('path',['/v1/chat/completions','/v1/responses','/v1/messages'])
@pytest.mark.parametrize('failure',['headers','body','bytes'])
def test_nonstream_all_protocols_apply_first_byte_and_body_limits(state,monkeypatch,path,failure):
    from server import config,deadline,keysvc,observability
    from server.routers import gateway,responses,anthropic
    calls=[];closed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if failure=='body':await asyncio.sleep(.1)
            yield b'x'*100000 if failure=='bytes' else b'{}'
        async def aclose(self):closed.append(True)
    async def handler(request):
        calls.append(True)
        if failure=='headers':await asyncio.sleep(.1)
        return httpx.Response(200,stream=Stream())
    monkeypatch.setattr(config,'http_client',lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(deadline,'request_budgets',lambda *a:deadline.Budgets(first_byte_s=.02,stream_idle_s=.02,total_s=.5))
    real_deadline=deadline.Deadline
    # Isolate transport timing from unrelated SQLite/thread scheduling latency.
    monkeypatch.setattr(deadline,'Deadline',lambda budgets:real_deadline(budgets,clock=lambda:0.0))
    monkeypatch.setenv('WB_MAX_OUTPUT_BYTES','65536')
    app=FastAPI();app.include_router(gateway.router);app.include_router(responses.router);app.include_router(anthropic.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    key=keysvc.create_key('synthetic-limit')['key']
    body={'model':'m','input':'hi'} if path.endswith('responses') else {'model':'m','messages':[{'role':'user','content':'hi'}]}
    if path.endswith('messages'):body['max_tokens']=16
    with TestClient(app) as client:
        response=client.post(path,headers={'authorization':'Bearer '+key},json=body)
    assert response.status_code>=400,response.text
    assert len(calls)==1,response.text  # accepted or uncertain sends are never replayed
    if failure!='bytes':assert response.json()['error']['code']=='upstream_timeout',response.text
    else:assert response.json()['error']['code']=='upstream_output_limit',response.text
    if failure!='headers':assert closed


@pytest.mark.parametrize('path',['/v1/chat/completions','/v1/responses','/v1/messages'])
@pytest.mark.parametrize('stream',[False,True])
def test_header_budget_has_consistent_error_code_and_audit_without_replay(state,monkeypatch,path,stream):
    from server import config,db,deadline,keysvc,observability
    from server.routers import gateway,responses,anthropic
    calls=[]
    async def handler(request):
        calls.append(True)
        await asyncio.sleep(.1)
        return httpx.Response(200,json={})
    monkeypatch.setattr(config,'http_client',lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(deadline,'request_budgets',lambda *a:deadline.Budgets(first_byte_s=.02,total_s=.5))
    # This contract times the provider's header wait. Preparation/admission has
    # independent exhaustion tests; slow/emulated SQLite must not spend this
    # synthetic 20ms before the fake transport is entered.
    real_deadline=deadline.Deadline
    monkeypatch.setattr(deadline,'Deadline',lambda budgets:real_deadline(budgets,clock=lambda:0.0))
    app=FastAPI()
    for router in (gateway.router,responses.router,anthropic.router):app.include_router(router)
    app.add_middleware(observability.RequestAuditMiddleware)
    key=keysvc.create_key('synthetic-header-budget')['key']
    body={'model':'synthetic','input':'hi'} if path.endswith('responses') else {'model':'synthetic','messages':[{'role':'user','content':'hi'}]}
    body['stream']=stream
    if path.endswith('messages'):body['max_tokens']=16
    with TestClient(app) as client:
        result=client.post(path,headers={'authorization':'Bearer '+key},json=body)
    assert result.status_code==502 and result.json()['error']['code']=='upstream_timeout',result.text
    assert len(calls)==1 and 'UnboundLocalError' not in result.text
    log=db.query_one('SELECT error_code,terminal_state FROM request_outcomes WHERE request_id=?',(result.headers['x-request-id'],))
    assert log['error_code']=='upstream_timeout' and log['terminal_state']=='transport_error'


@pytest.mark.parametrize('path',['/v1/chat/completions','/v1/responses','/v1/messages'])
def test_nonstream_fast_headers_slow_body_uses_remaining_idle_and_total_budget(state,monkeypatch,path):
    from server import config,deadline,keysvc,observability
    from server.routers import gateway,responses,anthropic
    calls=[];closed=[]
    raw=json.dumps({'id':'chat_synthetic','choices':[{'index':0,'message':{'role':'assistant','content':'synthetic ok'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2,'credit':0}}).encode()
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(.08)
            yield raw
        async def aclose(self):closed.append(True)
    async def handler(request):
        calls.append(True)
        return httpx.Response(200,stream=SlowBody())
    monkeypatch.setattr(config,'http_client',lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(deadline,'request_budgets',lambda *a:deadline.Budgets(first_byte_s=.02,stream_idle_s=.5,total_s=.5))
    # Measure the 80ms response body after immediate headers, without spending
    # the synthetic 20ms header budget on emulated SQLite preparation.
    real_deadline=deadline.Deadline
    monkeypatch.setattr(deadline,'Deadline',lambda budgets:real_deadline(budgets,clock=lambda:0.0))
    app=FastAPI()
    for router in (gateway.router,responses.router,anthropic.router):app.include_router(router)
    app.add_middleware(observability.RequestAuditMiddleware)
    key=keysvc.create_key('synthetic-fast-headers')['key']
    body={'model':'synthetic','input':'hi'} if path.endswith('responses') else {'model':'synthetic','messages':[{'role':'user','content':'hi'}]}
    if path.endswith('messages'):body['max_tokens']=16
    with TestClient(app) as client:
        result=client.post(path,headers={'authorization':'Bearer '+key},json=body)
    assert result.status_code==200,result.text
    assert 'synthetic ok' in result.text and len(calls)==1 and closed


@pytest.mark.parametrize('path',['/v1/chat/completions','/v1/responses','/v1/messages'])
@pytest.mark.parametrize('status',[400,429])
@pytest.mark.parametrize('stream',[False,True])
@pytest.mark.parametrize('compressed',[False,True])
def test_oversized_rejection_body_retains_known_http_status(state,monkeypatch,path,status,stream,compressed):
    from server import config,keysvc,observability
    from server.routers import gateway,responses,anthropic
    calls=[];closed=[]
    class HugeError(httpx.AsyncByteStream):
        async def __aiter__(self):
            raw=b'{"error":{"message":"'+b'x'*100000+b'"}}'
            yield gzip.compress(raw) if compressed else raw
        async def aclose(self):closed.append(True)
    async def handler(request):
        calls.append(True)
        return httpx.Response(status,stream=HugeError(),headers={'content-encoding':'gzip'} if compressed else {})
    monkeypatch.setattr(config,'http_client',lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    app=FastAPI();app.include_router(gateway.router);app.include_router(responses.router);app.include_router(anthropic.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    key=keysvc.create_key('synthetic oversized rejection')['key']
    body={'model':'m','input':'hi'} if path.endswith('responses') else {'model':'m','messages':[{'role':'user','content':'hi'}]}
    body['stream']=stream
    if path.endswith('messages'):body['max_tokens']=16
    with TestClient(app) as client:response=client.post(path,headers={'authorization':'Bearer '+key},json=body)
    assert response.status_code==status,response.text
    assert len(response.content)<2048 and 'size limit' in response.text
    assert len(calls)==1 and closed


@pytest.mark.parametrize('protocol',['responses','messages'])
def test_actual_translated_wire_bytes_have_a_cumulative_cap(monkeypatch,protocol):
    from server.routers import responses,anthropic
    from server.outputlimits import OutputLimitExceeded
    monkeypatch.setenv('WB_MAX_OUTPUT_BYTES','65536')
    tr=responses._StreamTranslator('synthetic','resp_wire') if protocol=='responses' else anthropic._StreamTranslator('synthetic')
    emitted=[]
    with pytest.raises(OutputLimitExceeded):
        for _ in range(2000):emitted+=tr.feed(delta(content='x'))
    assert sum(len(event) for event in emitted)<=65536
    assert tr.output_budget.used<65536  # generated event overhead, not the input cap


def test_wire_limit_during_responses_finish_emits_failed_and_records_failure(state,monkeypatch):
    from server import config,db,keysvc,observability
    from server.routers import responses
    calls=[];closed=[]
    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            for frame in [delta(content='x'*35000),delta(finish='stop'),
                          {'choices':[],'usage':{'prompt_tokens':1,'completion_tokens':1,'credit':.001}}]:
                yield ('data: '+json.dumps(frame)+'\n\n').encode()
            yield b'data: [DONE]\n\n'
        async def aclose(self):closed.append(True)
    async def handler(request):
        calls.append(True)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Wire())
    monkeypatch.setattr(config,'http_client',lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv('WB_MAX_OUTPUT_BYTES','65536')
    app=FastAPI();app.include_router(responses.router);app.add_middleware(observability.RequestAuditMiddleware)
    key=keysvc.create_key('synthetic terminal limit')['key']
    with TestClient(app) as client:
        response=client.post('/v1/responses',headers={'authorization':'Bearer '+key},json={'model':'m','input':'hi','stream':True})
    events=objects([response.content])
    assert any(e['type']=='response.failed' and e['response']['error']['code']=='upstream_output_limit' for e in events)
    assert not any(e['type']=='response.completed' for e in events)
    assert len(response.content)<65536+4096 and len(calls)==1 and closed
    log=db.query_one('SELECT status,terminal_state FROM request_logs ORDER BY id DESC LIMIT 1')
    assert log['status']==502 and log['terminal_state']!='completed'


def test_compressed_expansion_is_bounded_and_transport_is_closed(monkeypatch):
    from server import deadline
    from server.outputlimits import OutputLimitExceeded
    monkeypatch.setenv('WB_MAX_OUTPUT_BYTES','65536')
    closed=[]
    class Compressed(httpx.AsyncByteStream):
        async def __aiter__(self):yield gzip.compress(b'x'*1_000_000)
        async def aclose(self):closed.append(True)
    async def handler(request):return httpx.Response(200,headers={'content-encoding':'gzip'},stream=Compressed())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OutputLimitExceeded):
                await deadline.buffered_post(client,'https://synthetic.invalid',content=b'{}',headers={},deadline=deadline.Deadline(deadline.Budgets()))
    asyncio.run(run());assert closed


def test_scheduler_holds_lease_until_owned_background_work_finishes(monkeypatch):
    from server.services import dailyplan,taskrun
    async def run():
        started=asyncio.Event();stop=asyncio.Event();released=[]
        async def work(*a):
            try:started.set();await stop.wait()
            finally:taskrun._state.update(running=False,exit_code=0,error='')
        monkeypatch.setattr(dailyplan,'claim_task',lambda _: (True,1))
        monkeypatch.setattr(dailyplan.tasklease,'release',lambda *a:released.append(True))
        monkeypatch.setattr(dailyplan,'mark_ran_fenced',lambda *a:True)
        monkeypatch.setattr(taskrun,'_prepare',lambda *a:(True,['synthetic-never-executed']))
        monkeypatch.setattr(taskrun,'_run',work)
        monkeypatch.setattr(taskrun,'_task',None)
        monkeypatch.setattr(taskrun,'_state',{'running':False})
        pending=asyncio.create_task(dailyplan._run_claimed('claim',{'claim':{'hour':0}}))
        await asyncio.wait_for(started.wait(),1)
        assert not pending.done() and not released
        stop.set();assert await asyncio.wait_for(pending,1)
        assert released==[True]
    asyncio.run(run())


def test_lost_lease_cancels_and_joins_work_before_release(monkeypatch):
    from server.services import dailyplan
    async def run():
        started=asyncio.Event();stopped=asyncio.Event();events=[]
        async def work(*a):
            try:started.set();await asyncio.Event().wait()
            finally:await asyncio.sleep(0);stopped.set();events.append('joined')
        async def lost(*a,**kw):await started.wait();raise RuntimeError('synthetic lost lease')
        monkeypatch.setattr(dailyplan,'claim_task',lambda _: (True,1))
        monkeypatch.setattr(dailyplan,'run_task',work)
        monkeypatch.setattr(dailyplan,'_lease_heartbeat',lost)
        monkeypatch.setattr(dailyplan.tasklease,'release',lambda *a:events.append('released'))
        monkeypatch.setattr(dailyplan,'mark_ran_fenced',lambda *a:events.append('stamped'))
        assert not await dailyplan._run_claimed('checkin',{'checkin':{'hour':0}})
        assert stopped.is_set() and events==['joined','released']
    asyncio.run(run())


def test_expired_live_owner_blocks_takeover_and_publish(state):
    from server import db,tasklease
    tasklease.reset_identity()
    try:
        assert tasklease.claim('audit-live',1)[0]
        db.execute('UPDATE task_leases SET lease_until=1 WHERE task=?',('audit-live',))
        assert not tasklease.claim('audit-live',1,owner='other-host:'+'a'*32)[0]
        with db.transaction() as conn,pytest.raises(tasklease.PublishBlocked):
            tasklease.begin_publish(conn,'synthetic-publisher')
    finally:tasklease.reset_identity()


def test_failed_pointer_replace_cleans_only_own_stage_and_restarts_old(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[2]/'deploy/release_state.py'
    spec=importlib.util.spec_from_file_location('audit_release_state',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    class Pointer:
        def is_symlink(self):return True
    events=[]
    monkeypatch.setattr(module,'Path',lambda _:Pointer())
    monkeypatch.setattr(module.os,'readlink',lambda _: 'old')
    def failed(*a):raise OSError('synthetic replace failure')
    monkeypatch.setattr(module,'point',failed)
    with pytest.raises(OSError):
        module.activate('synthetic','new',lambda:events.append('stop'),lambda:events.append('start-old'),lambda:True)
    assert events==['stop','stop','start-old']


@pytest.mark.parametrize('existing',[False,True])
def test_pointer_stage_is_cleaned_on_failure_without_discarding_unknown_stage(monkeypatch,existing):
    path=Path(__file__).resolve().parents[2]/'deploy/release_state.py'
    spec=importlib.util.spec_from_file_location('audit_pointer_transaction',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    pointer=mock.MagicMock();pointer.name='current'
    stage=pointer.with_name.return_value
    stage.exists.return_value=existing;stage.is_symlink.return_value=False
    stage.lstat.return_value=SimpleNamespace(st_dev=1,st_ino=7)
    monkeypatch.setattr(module,'Path',lambda _:pointer)
    monkeypatch.setattr(module.os,'replace',mock.Mock(side_effect=OSError('synthetic atomic replace failure')))
    with pytest.raises((OSError,RuntimeError)):module.point('synthetic-pointer','new')
    if existing:
        stage.symlink_to.assert_not_called();stage.unlink.assert_not_called()
    else:
        stage.symlink_to.assert_called_once();stage.unlink.assert_called_once()


def test_rollback_postcommit_fsync_error_still_starts_the_old_service(monkeypatch):
    path=Path(__file__).resolve().parents[2]/'deploy/release_state.py'
    spec=importlib.util.spec_from_file_location('audit_pointer_durability',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    pointer=mock.MagicMock();pointer.is_symlink.return_value=True
    current=['old'];events=[]
    monkeypatch.setattr(module,'Path',lambda _:pointer)
    monkeypatch.setattr(module.os,'readlink',lambda _:current[0])
    def uncertain(_pointer,target):current[0]=target;raise OSError('synthetic directory fsync failed after rename')
    monkeypatch.setattr(module,'point',uncertain)
    with pytest.raises(RuntimeError,match='durability unconfirmed'):
        module.activate('synthetic','new',lambda:events.append('stop'),lambda:events.append('start-'+current[0]),lambda:True)
    assert current==['old'] and events==['stop','stop','start-old']


def test_continuous_renewal_errors_exhaust_budget_and_join_work(monkeypatch):
    import time
    from server.services import dailyplan
    async def run():
        original_sleep=asyncio.sleep
        clock=[0.0];events=[];renewals=[]
        async def step(_delay):clock[0]+=2.1;await original_sleep(0)
        def busy(*a):renewals.append(True);raise RuntimeError('synthetic database busy')
        async def work(*a):
            try:await asyncio.Event().wait()
            finally:events.append('joined')
        monkeypatch.setattr(dailyplan,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time))
        monkeypatch.setattr(dailyplan.asyncio,'sleep',step)
        monkeypatch.setattr(dailyplan,'LEASE_TTL',6)
        monkeypatch.setattr(dailyplan,'claim_task',lambda _: (True,1))
        monkeypatch.setattr(dailyplan,'run_task',work)
        monkeypatch.setattr(dailyplan.tasklease,'renew',busy)
        monkeypatch.setattr(dailyplan.tasklease,'release',lambda *a:events.append('released'))
        monkeypatch.setattr(dailyplan,'mark_ran_fenced',lambda *a:events.append('stamped'))
        assert not await dailyplan._run_claimed('checkin',{'checkin':{'hour':0}})
        assert len(renewals)==1 and events==['joined','released']  # First renewal failure now stops immediately.
    asyncio.run(run())


def test_completed_work_cannot_stamp_after_simultaneous_lease_failure(monkeypatch):
    from server.services import dailyplan
    async def run():
        events=[]
        async def failed(*a,**kw):raise RuntimeError('synthetic ownership loss')
        async def completed(*a):return True,'synthetic completed work'
        monkeypatch.setattr(dailyplan,'claim_task',lambda _: (True,1))
        monkeypatch.setattr(dailyplan,'_lease_heartbeat',failed)
        monkeypatch.setattr(dailyplan,'run_task',completed)
        monkeypatch.setattr(dailyplan,'mark_ran_fenced',lambda *a:events.append('stamped'))
        monkeypatch.setattr(dailyplan.tasklease,'release',lambda *a:events.append('released'))
        assert not await dailyplan._run_claimed('checkin',{'checkin':{'hour':0}})
        assert events==['released']
    asyncio.run(run())


def test_actual_task_subprocess_is_joined_before_lost_lease_release(state,monkeypatch):
    import sys
    from server import config
    from server.services import dailyplan,taskrun
    async def run():
        started=asyncio.Event();processes=[];events=[]
        spawn=asyncio.create_subprocess_exec
        append=taskrun._append
        async def capture(*args,**kwargs):
            process=await spawn(*args,**kwargs)
            processes.append(process)
            return process
        def record(line):
            append(line)
            if line=='SYNTHETIC_CHILD_STARTED':started.set()
        async def loss(*a,**kw):
            await asyncio.wait_for(started.wait(),5)
            raise RuntimeError('synthetic ownership loss after child start')
        async def work(*a):return await taskrun.run_owned('synthetic','ALL')
        def release(*a):
            assert len(processes)==1 and processes[0].returncode is not None
            assert taskrun._state['running'] is False
            events.append('released-after-child-exit')
        monkeypatch.setattr(config,'UPSTREAM_DIR',state)
        monkeypatch.setattr(taskrun,'_script_path',lambda:state/'scripts'/'synthetic.py')
        monkeypatch.setattr(taskrun,'_prepare',lambda *a:(True,[sys.executable,'-u','-c','import time;print("SYNTHETIC_CHILD_STARTED",flush=True);time.sleep(60)']))
        monkeypatch.setattr(taskrun,'_state',dict(taskrun._state,running=False,lines=[]))
        monkeypatch.setattr(taskrun,'_task',None)
        monkeypatch.setattr(taskrun,'_append',record)
        monkeypatch.setattr(taskrun.asyncio,'create_subprocess_exec',capture)
        monkeypatch.setattr(dailyplan,'claim_task',lambda _:(True,1))
        monkeypatch.setattr(dailyplan,'_lease_heartbeat',loss)
        monkeypatch.setattr(dailyplan,'run_task',work)
        monkeypatch.setattr(dailyplan,'mark_ran_fenced',lambda *a:events.append('stamped'))
        monkeypatch.setattr(dailyplan.tasklease,'release',release)
        assert not await asyncio.wait_for(dailyplan._run_claimed('synthetic',{'synthetic':{'hour':0}}),10)
        assert started.is_set() and events==['released-after-child-exit']
    asyncio.run(run())


@pytest.mark.parametrize('first',['','a'])
def test_late_and_cumulative_tool_names_restore_without_duplication(first):
    from server.routers import responses as r
    tr=r._StreamTranslator('synthetic','resp_cumulative')
    frames=tr.feed(delta(tool_calls=[{'index':0,'id':'call_cumulative','function':{'name':first,'arguments':'{'}}]))
    frames+=tr.feed(delta(tool_calls=[{'index':0,'function':{'name':'alpha','arguments':'}'}}],finish='tool_calls'))
    values=objects(frames)
    assert values[-1]['type']=='response.completed'
    assert values[-1]['response']['output'][0]['name']=='alpha'


def test_custom_namespace_roundtrip_agrees_for_stream_and_nonstream():
    from server.routers import responses as r
    tools=[{'type':'namespace','name':n,'tools':[{'type':'custom','name':'patch'}]} for n in ('alpha','beta')]
    bridge=r._ToolBridge(tools)
    name=bridge.outbound_name('patch','beta')
    function={'name':name,'arguments':json.dumps({'input':'synthetic patch'})}
    data={'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'call_custom_beta','function':function}]}}]}
    normal=r.to_responses_object(data,'synthetic','resp_custom',bridge=bridge)['output'][0]
    tr=r._StreamTranslator('synthetic','resp_custom',bridge=bridge)
    events=objects(tr.feed(delta(tool_calls=[{'index':0,'id':'call_custom_beta','function':function}],finish='tool_calls')))
    items=[x['item'] for x in events if x['type'] in ('response.output_item.added','response.output_item.done')]+[events[-1]['response']['output'][0],normal]
    assert len(items)==4
    for item in items:
        assert item['type']=='custom_tool_call' and item['name']=='patch' and item['namespace']=='beta'
        assert item['call_id']=='call_custom_beta'
    follow=r.to_chat_request({'model':'synthetic','tools':tools,'input':[normal,{'type':'custom_tool_call_output','call_id':'call_custom_beta','output':'ok'}]})
    assert follow['messages'][0]['tool_calls'][0]['function']['name']==name


@pytest.mark.parametrize('protocol',['responses','anthropic'])
def test_translated_nonstream_object_has_a_final_serialized_byte_bound(state,monkeypatch,protocol):
    from server.routers import responses,anthropic
    from server.outputlimits import OutputLimitExceeded
    monkeypatch.setenv('WB_MAX_OUTPUT_BYTES','65536')
    message={'content':'x'*33000} if protocol=='responses' else {'reasoning_content':'r'*30000,'content':'ok'}
    data={'choices':[{'finish_reason':'stop','message':message}]}
    with pytest.raises(OutputLimitExceeded):
        if protocol=='responses':responses.to_responses_object(data,'synthetic','resp_limit')
        else:anthropic.to_anthropic_response(data,'synthetic',thinking=True)


@pytest.mark.parametrize('protocol',['responses','anthropic'])
def test_nonstream_item_cap_includes_text_and_reasoning(protocol):
    from server.routers import responses,anthropic
    from server.outputlimits import OutputLimitExceeded
    count=responses.MAX_OUTPUT_ITEMS if protocol=='responses' else anthropic.MAX_STREAM_TOOLS
    data={'choices':[{'finish_reason':'tool_calls','message':{'content':'text','tool_calls':[
        {'id':f'call_{i}','function':{'name':'run','arguments':'{}'}} for i in range(count)]}}]}
    with pytest.raises(OutputLimitExceeded):
        if protocol=='responses':responses.to_responses_object(data,'synthetic','resp_limit')
        else:anthropic.to_anthropic_response(data,'synthetic')


@pytest.mark.parametrize('arguments',['[]','{"partial":','{"x":1,"x":2}','NaN'])
def test_invalid_message_tools_have_a_specific_http_error_and_no_replay(state,monkeypatch,arguments):
    from server import config,keysvc,observability
    from server.routers import gateway,anthropic
    calls=[]
    async def handler(request):
        calls.append(True)
        return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[
            {'id':'call_synthetic','function':{'name':'run','arguments':arguments}}]}}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'credit':0}})
    monkeypatch.setattr(config,'http_client',lambda *a,**kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    app=FastAPI();app.include_router(gateway.router);app.include_router(anthropic.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    key=keysvc.create_key('synthetic-invalid-tool')['key']
    with TestClient(app) as client:
        response=client.post('/v1/messages',headers={'authorization':'Bearer '+key},json={
            'model':'m','max_tokens':16,'messages':[{'role':'user','content':'hi'}]})
    assert response.status_code==502,response.text
    assert response.json()['error']['code']=='invalid_upstream_tool_body'
    assert '_raw' not in response.text and len(calls)==1


def test_empty_late_thinking_block_never_duplicates_prior_reasoning(state):
    from server.routers import anthropic as a
    converted=a.to_openai_request({'model':'synthetic','max_tokens':16,'messages':[
        {'role':'assistant','content':[{'type':'thinking','thinking':'first reason'},
         {'type':'text','text':'body'},{'type':'thinking','signature':'invalid-without-plaintext'}]},
        {'role':'user','content':'continue'}]})
    assert converted['messages'][0]['reasoning_content']=='first reason'
