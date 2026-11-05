"""R2: indexed tools retain exact arguments and never fake completion on failure."""
import json
import unittest
from unittest import mock

from server.routers import anthropic
from server.tests import test_review_usage_repairs as usage_fixtures


def events(chunks):
    return [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
            if line.startswith(b'data: ')]


def frame(*calls, text=None):
    delta = {'tool_calls': list(calls)}
    if text is not None:
        delta['content'] = text
    return {'choices': [{'index': 0, 'delta': delta}]}


def call(index, args=None, name=None, identity=None):
    fn = {}
    if args is not None: fn['arguments'] = args
    if name is not None: fn['name'] = name
    result = {'index': index, 'function': fn}
    if identity is not None: result['id'] = identity
    return result


class ToolRepairs(unittest.TestCase):
    def assert_well_formed(self, output):
        opened, closed, inputs = {}, set(), {}
        active = None
        for event in output:
            kind = event['type']
            if kind == 'content_block_start':
                self.assertIsNone(active, 'SDK callbacks must see one current content block')
                index = event['index']
                active = index
                self.assertEqual(index, len(opened))
                self.assertNotIn(index, opened)
                opened[index] = event['content_block']
            elif kind == 'content_block_delta':
                index = event['index']
                self.assertEqual(index, active)
                self.assertIn(index, opened)
                self.assertNotIn(index, closed)
                if event['delta']['type'] == 'input_json_delta':
                    inputs[index] = inputs.get(index, '') + event['delta']['partial_json']
            elif kind == 'content_block_stop':
                self.assertEqual(event['index'], active)
                active = None
                self.assertIn(event['index'], opened)
                self.assertNotIn(event['index'], closed)
                closed.add(event['index'])
        self.assertEqual(set(opened), closed)
        self.assertEqual(sum(event['type'] == 'message_stop' for event in output), 1)
        return {block['id']: (block['name'], inputs.get(index, '{}')) for index, block in opened.items()
                if block['type'] == 'tool_use'}

    def test_interleaved_tools_keep_own_index_and_exact_json(self):
        translator = anthropic._StreamTranslator('synthetic')
        output = []
        for delta in (frame(call(0, '{"a":', 'alpha', 'call-a')),
                      frame(call(1, '{"b":', 'beta', 'call-b')),
                      frame(call(0, '1}'), text='between'), frame(call(1, '2}'))):
            output += events(translator.feed(delta))
        output += events(translator.finish('tool_calls'))
        self.assertEqual(self.assert_well_formed(output),
                         {'call-a': ('alpha', '{"a":1}'), 'call-b': ('beta', '{"b":2}')})

    def test_multiple_calls_in_same_frame_and_out_of_order_indices(self):
        translator = anthropic._StreamTranslator('synthetic')
        output = events(translator.feed(frame(call(7, '{"x":', 'f', 'seven'), call(2, '{"y":', 'f', 'two'))))
        output += events(translator.feed(frame(call(2, '2}'), call(7, '7}'))))
        output += events(translator.finish('tool_calls'))
        self.assertEqual(self.assert_well_formed(output), {'seven': ('f', '{"x":7}'), 'two': ('f', '{"y":2}')})

    def test_fragmented_identity_and_repeated_metadata_do_not_open_new_tool(self):
        translator = anthropic._StreamTranslator('synthetic')
        output = events(translator.feed(frame(call(0, '', 'get_', 'call_'))))
        output += events(translator.feed(frame(call(0, '{"city":', 'weather', 'one'))))
        output += events(translator.feed(frame(call(0, '"北京"}', 'get_weather', 'call_one'))))
        output += events(translator.finish('tool_calls'))
        self.assertEqual(self.assert_well_formed(output), {'call_one': ('get_weather', '{"city":"北京"}')})

    def test_arguments_before_metadata_are_deferred_without_losing_content(self):
        translator = anthropic._StreamTranslator('synthetic')
        output = events(translator.feed(frame(call(0, '{"n":'))))
        output += events(translator.feed(frame(call(0, '1}', 'f', 'one'))))
        self.assertFalse(any(event['type'] == 'content_block_start' for event in output))
        output += events(translator.finish('tool_calls'))
        self.assertEqual(self.assert_well_formed(output), {'one': ('f', '{"n":1}')})

    def test_repeated_literal_metadata_fragments_are_concatenated_before_arguments(self):
        translator = anthropic._StreamTranslator('synthetic')
        output=events(translator.feed(frame(call(0,'','f','a'))))
        output+=events(translator.feed(frame(call(0,'{}','f','a'))))
        output+=events(translator.finish('tool_calls'))
        self.assertEqual(self.assert_well_formed(output),{'aa':('ff','{}')})

    def test_id_only_delta_is_followed_by_function_metadata(self):
        translator=anthropic._StreamTranslator('synthetic')
        output=events(translator.feed(frame({'index':0,'id':'one','type':'function'})))
        output+=events(translator.feed(frame(call(0,'{}','f'))))
        output+=events(translator.finish('tool_calls'))
        self.assertEqual(self.assert_well_formed(output),{'one':('f','{}')})

    def test_invalid_or_truncated_json_emits_error_without_success_stop(self):
        for args in ('', '{"x":', '{"x":NaN}', '[]', '{invalid}'):
            with self.subTest(args=args):
                translator = anthropic._StreamTranslator('synthetic')
                output = events(translator.feed(frame(call(0, args, 'f', 'one'))))
                output += events(translator.finish('tool_calls'))
                self.assertTrue(any(event['type'] == 'error' for event in output))
                self.assertFalse(any(event['type'] == 'message_stop' for event in output))
                self.assertTrue(translator.failed)
                self.assertEqual(translator.finish('tool_calls'), [])

    def test_invalid_indices_and_changed_identities_fail_closed(self):
        for index in (-1, None, True, '0', 999999):
            with self.subTest(index=index):
                with self.assertRaises(anthropic.ToolStreamError):
                    anthropic._StreamTranslator('synthetic').feed(frame(call(index, '{}', 'f', 'one')))
        translator = anthropic._StreamTranslator('synthetic')
        translator.feed(frame(call(0, '{', 'f', 'one')))
        with self.assertRaises(anthropic.ToolStreamError):
            translator.feed(frame(call(0, '}', 'other', 'one')))

    def test_argument_and_metadata_limits_are_enforced_before_retention(self):
        translator = anthropic._StreamTranslator('synthetic')
        with mock.patch.object(anthropic, 'MAX_TOOL_ARGUMENT_BYTES', 4):
            translator.feed(frame(call(0, '{"x"', 'f', 'one')))
            with self.assertRaises(anthropic.ToolStreamError):
                translator.feed(frame(call(0, ':1}')))
        self.assertEqual(translator.tool_argument_bytes, 4)
        with mock.patch.object(anthropic, 'MAX_TOOL_METADATA_BYTES', 4), self.assertRaises(anthropic.ToolStreamError):
            anthropic._StreamTranslator('synthetic').feed(frame(call(0, '{}', 'oversized', 'one')))

    def test_abort_does_not_complete_and_valid_finalization_is_idempotent(self):
        translator = anthropic._StreamTranslator('synthetic')
        translator.feed(frame(call(0, '{', 'f', 'one')))
        self.assertEqual(translator.abort(), [])
        self.assertEqual(translator.finish('tool_calls'), [])
        self.assertEqual(translator.feed(frame(call(0, '}'))), [])
        translator = anthropic._StreamTranslator('synthetic')
        output = events(translator.feed(frame(call(0, '{}', 'f', 'one'))))
        output += events(translator.finish('tool_calls'))
        self.assert_well_formed(output)
        self.assertEqual(translator.finish('tool_calls'), [])

    def test_duplicate_tool_ids_are_not_claimed_as_successful(self):
        translator = anthropic._StreamTranslator('synthetic')
        translator.feed(frame(call(0, '{}', 'f', 'same'), call(1, '{}', 'g', 'same')))
        output = events(translator.finish('tool_calls'))
        self.assertEqual([event['type'] for event in output], ['error'])


class ToolHTTPRepairs(unittest.IsolatedAsyncioTestCase):
    setUp = usage_fixtures.UsageRepairs.setUp

    async def test_real_messages_route_preserves_interleaving_and_rejects_bad_streams(self):
        import httpx
        from fastapi import FastAPI
        from server import config,db,keysvc,observability,platformpolicy
        from server.routers import gateway
        app=FastAPI();app.include_router(anthropic.router)
        app.add_middleware(observability.RequestAuditMiddleware)
        app.add_middleware(platformpolicy.PlatformRoutingMiddleware)
        calls=[]
        mode='valid'
        def provider(request):
            calls.append(json.loads(request.content))
            if mode=='valid':
                frames=[frame(call(0,'{"a":','alpha','call-a')),frame(call(1,'{"b":','beta','call-b')),
                        frame(call(0,'1}')),frame(call(1,'2}'))]
            elif mode=='truncated':
                frames=[frame(call(0,'{"a":','alpha','call-a'))]
            else:
                frames=[frame(call(0,'{','alpha','call-a')),frame(call(0,'}','beta','call-a'))]
            frames.append({'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}],
                           'usage':{'prompt_tokens':1,'completion_tokens':2}})
            raw=''.join('data: '+json.dumps(value)+'\n\n' for value in frames)+'data: [DONE]\n\n'
            return httpx.Response(200,content=raw,headers={'content-type':'text/event-stream'})
        def client(*args,**kwargs):return httpx.AsyncClient(transport=httpx.MockTransport(provider))
        with mock.patch.object(config,'http_client',side_effect=client),mock.patch.object(gateway,'RATE_MAX_PER_MIN',0):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as caller:
                for mode in ('valid','truncated','identity-change'):
                    with self.subTest(mode=mode):
                        made=keysvc.create_key('synthetic-tool-http',quota=100000)
                        result=await caller.post('/v1/messages',headers={'authorization':'Bearer '+made['key']},json={
                            'model':'synthetic','stream':True,'max_tokens':16,'messages':[{'role':'user','content':'synthetic'}]})
                        self.assertEqual(result.status_code,200,result.text)
                        output=events([result.content])
                        row=db.query_one('SELECT terminal_state,status FROM request_logs WHERE key_id=?',(made['id'],))
                        if mode=='valid':
                            parsed=ToolRepairs.assert_well_formed(self,output)
                            self.assertEqual(parsed,{'call-a':('alpha','{"a":1}'),'call-b':('beta','{"b":2}')})
                            self.assertEqual(tuple(row),('completed',200))
                        else:
                            self.assertFalse(any(value['type']=='message_stop' for value in output))
                            self.assertEqual([value['error']['code'] for value in output if value['type']=='error'],['invalid_upstream_tool_stream'])
                            self.assertEqual(tuple(row),('protocol_incomplete',502))
        self.assertEqual(len(calls),3,'no tool failure may replay an accepted provider request')
