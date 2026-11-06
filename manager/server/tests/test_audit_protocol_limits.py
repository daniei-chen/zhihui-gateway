"""Regression tests for protocol identity, retained memory and reconciliation."""
import json
import sqlite3
import time
import tracemalloc
import unittest
from unittest.mock import patch

from server import metering, observability
from server.routers import anthropic, gateway, responses


def payloads(events):
    return [json.loads(line[6:]) for event in events for line in event.decode().splitlines()
            if line.startswith('data: ') and line[6:] != '[DONE]']


class ProtocolStateTests(unittest.TestCase):
    def test_anthropic_interleaved_tool_arguments_keep_their_call_index(self):
        translator = anthropic._StreamTranslator('fixture')
        frames = [
            {'tool_calls': [
                {'index': 0, 'id': 'a', 'function': {'name': 'first', 'arguments': '{"a":'}},
                {'index': 1, 'id': 'b', 'function': {'name': 'second', 'arguments': '{"b":'}}]},
            {'tool_calls': [
                {'index': 0, 'function': {'arguments': '1}'}},
                {'index': 1, 'function': {'arguments': '2}'}}]},
        ]
        events = []
        for delta in frames:
            events.extend(payloads(translator.feed({'choices': [{'delta': delta}]})))
        events.extend(payloads(translator.finish('tool_calls')))
        names, arguments, closed = {}, {}, set()
        for event in events:
            if event['type'] == 'content_block_start':
                index = event['index']
                names[index] = event['content_block']['name']
                arguments[index] = ''
            elif event['type'] == 'content_block_delta':
                self.assertNotIn(event['index'], closed)
                arguments[event['index']] += event['delta']['partial_json']
            elif event['type'] == 'content_block_stop':
                closed.add(event['index'])
        self.assertEqual({names[i]: json.loads(value) for i, value in arguments.items()},
                         {'first': {'a': 1}, 'second': {'b': 2}})
        self.assertEqual(closed, set(names))

    def test_anthropic_reasoning_credentials_cover_only_their_block(self):
        translator = anthropic._StreamTranslator('fixture', thinking=True)
        events = []
        for delta in ({'reasoning_content': 'first'}, {'content': 'text'},
                      {'reasoning_content': 'second'}, {'content': 'last'}):
            events.extend(payloads(translator.feed({'choices': [{'delta': delta}]})))
        signatures = [e['delta']['signature'] for e in events if
                      e.get('delta', {}).get('type') == 'signature_delta']
        self.assertEqual(signatures, [responses._encode_credential('first'),
                                      responses._encode_credential('second')])

    def test_responses_does_not_silently_drop_excess_tools(self):
        translator = responses._StreamTranslator('fixture', 'response_fixture')
        with patch.object(responses, 'MAX_OUTPUT_ITEMS', 2):
            for index in range(2):
                translator.feed({'choices': [{'delta': {'tool_calls': [
                    {'index': index, 'id': str(index),
                     'function': {'name': 'tool', 'arguments': '{}'}}]}}]})
            with self.assertRaisesRegex(ValueError, 'output item limit'):
                translator.feed({'choices': [{'delta': {'tool_calls': [
                    {'index': 2, 'function': {'name': 'tool', 'arguments': '{}'}}]}}]})
        self.assertFalse(translator.finished)

    def test_alternating_reasoning_and_text_cannot_bypass_item_limit(self):
        for translator in (responses._StreamTranslator('fixture', 'r'),
                           anthropic._StreamTranslator('fixture', thinking=True)):
            with self.subTest(protocol=type(translator).__module__), \
                    patch.object(responses, 'MAX_OUTPUT_ITEMS', 2):
                translator.feed({'choices': [{'delta': {'reasoning_content': 'first'}}]})
                translator.feed({'choices': [{'delta': {'content': 'second'}}]})
                with self.assertRaises(ValueError):
                    translator.feed({'choices': [{'delta': {'reasoning_content': 'third'}}]})

    def test_stream_usage_does_not_retain_unknown_metadata(self):
        audit = observability.StreamAudit()
        translator = responses._StreamTranslator('fixture', 'r')
        for index in range(1000):
            usage = {f'unrecognized_{index}': 'x' * 50,
                     'prompt_tokens': 2, 'completion_tokens': 3,
                     'prompt_tokens_details': {'cached_tokens': 1, 'unbounded': ['x'] * 100}}
            obj = {'usage': usage}
            audit.feed(('data: ' + json.dumps(obj) + '\n\n').encode())
            translator.feed(obj)
        for retained in (audit.usage, translator.usage):
            self.assertEqual(retained, {'prompt_tokens': 2, 'completion_tokens': 3,
                                       'prompt_tokens_details': {'cached_tokens': 1}})

    def test_nonfinite_tool_and_usage_numbers_never_raise_in_protocol_finalizers(self):
        for value in (float('inf'), float('nan'), -1, True, 1.5, 2**64):
            for convert in (responses._as_int, anthropic._as_int):
                self.assertEqual(convert(value), 0)

    def test_malformed_structured_credit_keeps_stream_usage_unknown(self):
        for bad_credit in ({'unexpected': 'metadata'}, [0], 'x' * 129):
            with self.subTest(kind=type(bad_credit).__name__):
                audit = observability.StreamAudit()
                audit.feed(('data: ' + json.dumps({'usage': {'prompt_tokens': 0,
                    'completion_tokens': 0, 'credit': bad_credit}}) + '\n\n').encode())
                usage = metering.parse_usage(audit.usage)
                self.assertTrue(usage.malformed)
                self.assertEqual(metering.spend_class(terminal_state='completed', usage=usage,
                    credit=usage.credit), metering.SPEND_UNKNOWN)

    def test_retry_receipt_contract_applies_to_selected_workbuddy_only(self):
        for platform in ('workbuddy', 'qoder', 'trae'):
            state = observability.RequestAudit('r', '/v1/chat/completions', 'POST',
                                              'openai', time.monotonic())
            state.actual_platform = platform
            token = observability.current.set(state)
            try:
                with patch.object(gateway.db, 'execute'), \
                        patch.object(gateway.upstreamsvc, 'forward_api_key', return_value=''):
                    headers = gateway._upstream_headers({'name': platform})
                self.assertEqual('X-WB-Attempt-Budget' in headers, platform == 'workbuddy')
                self.assertEqual(state.provider_budget_enabled, platform == 'workbuddy')
            finally:
                observability.current.reset(token)


class ReconciliationResourceTests(unittest.TestCase):
    def test_related_history_is_bounded_to_selected_request_ids(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript('''
          CREATE TABLE request_logs(id INTEGER PRIMARY KEY, request_id TEXT, ts INTEGER,
            endpoint TEXT, protocol TEXT, platform TEXT, actual_platform TEXT, upstream_ref TEXT,
            status INTEGER, http_status INTEGER, terminal_state TEXT, prompt_tokens INTEGER,
            completion_tokens INTEGER, credit REAL, usage_source TEXT, usage_complete INTEGER);
          CREATE INDEX logs_request ON request_logs(request_id);
          CREATE TABLE usage_events(request_id TEXT PRIMARY KEY, prompt_tokens INTEGER,
            completion_tokens INTEGER, credit_units INTEGER, counted INTEGER, terminal_state TEXT);
          CREATE TABLE request_outcomes(request_id TEXT PRIMARY KEY, ts INTEGER,
            terminal_state TEXT, http_status INTEGER);
        ''')
        connection.execute("INSERT INTO request_logs VALUES(1,'selected',100,'/v1/chat/completions',"
            "'openai','workbuddy','workbuddy','default',200,200,'completed',0,0,NULL,'upstream',0)")
        connection.executemany('INSERT INTO usage_events VALUES(?,1,2,0,1,\'completed\')',
                               [(f'old_{i}',) for i in range(10000)] + [('selected',)])
        connection.executemany('INSERT INTO request_outcomes VALUES(?,?,\'completed\',200)',
                               [('old_orphan', 1), ('new_orphan', 100)])
        tracemalloc.start()
        try:
            report = metering.reconcile(connection, since_ts=50, limit=1)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 256 * 1024)
        self.assertEqual(report['tables_seen']['usage_events'], 1)
        self.assertEqual(report['findings']['zero_token_completed']['count'], 1)
        self.assertEqual(report['spend_classes'][metering.SPEND_UNKNOWN], 1)
        self.assertEqual(report['findings']['outcome_without_log']['samples'][0]['request_id'],
                         'new_orphan')

    def test_unknown_budget_and_malformed_credit_cannot_be_settled(self):
        for usage, state in ((metering.parse_usage({'prompt_tokens': 1, 'completion_tokens': 2,
                                                   'credit': float('inf')}), None),
                             (metering.parse_usage({'prompt_tokens': 1, 'completion_tokens': 2}),
                              'unknown')):
            self.assertEqual(metering.spend_class(terminal_state='completed', usage=usage,
                credit=usage.credit, budget_state=state), metering.SPEND_UNKNOWN)
