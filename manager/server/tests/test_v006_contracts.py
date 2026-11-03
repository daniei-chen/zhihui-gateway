"""Self-use release contracts: fresh rules, fidelity, diagnostics and owned SSE."""
import asyncio
import copy
import json
import unittest
from unittest import mock

from server import logview, promptcompat, sseheartbeat
from server import fairadmission, runtimemetrics, modelcontext


class FairCapacity(unittest.IsolatedAsyncioTestCase):
    async def test_round_robin_not_fifo_monopoly(self):
        gate, grants = fairadmission.FairGate(), []
        busy = True
        def reserve(name):
            def claim():
                nonlocal busy
                if busy:
                    return False
                busy = True
                grants.append(name)
                return True
            return claim
        tasks = [asyncio.create_task(gate.acquire(key, reserve(name)))
                 for key, name in [(1,'a1'), (1,'a2'), (2,'b1'), (2,'b2')]]
        await asyncio.sleep(0)
        for _ in tasks:
            busy = False
            gate.notify()
            await asyncio.sleep(0)
        self.assertEqual(await asyncio.gather(*tasks), [True]*4)
        self.assertEqual(grants, ['a1','b1','a2','b2'])
        self.assertEqual(gate.pending, 0)

    async def test_cancel_timeout_and_queue_bounds_release_tickets(self):
        gate = fairadmission.FairGate(maximum=2, per_key=1)
        a = asyncio.create_task(gate.acquire(1, lambda: False, .5))
        await asyncio.sleep(0)
        self.assertFalse(await gate.acquire(1, lambda: False, .5))
        b = asyncio.create_task(gate.acquire(2, lambda: False, .01))
        await asyncio.sleep(0)
        self.assertFalse(await gate.acquire(3, lambda: False, .5))
        a.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await a
        self.assertFalse(await b)
        self.assertEqual((gate.pending, len(gate.order), len(gate.lanes)), (0,0,0))
        self.assertEqual((gate.overflows, gate.timeouts), (2,1))

    async def test_byte_blocked_lane_does_not_block_small_lane(self):
        gate = fairadmission.FairGate()
        big = asyncio.create_task(gate.acquire(1, lambda: False, .01))
        await asyncio.sleep(0)
        self.assertTrue(await gate.acquire(2, lambda: True, .1))
        self.assertFalse(await big)

    async def test_cancellation_after_grant_remains_owned_by_caller_finally(self):
        gate = fairadmission.FairGate()
        can_reserve, owns, released = False, False, False
        def reserve():
            nonlocal owns
            owns = can_reserve
            return owns
        async def owner():
            nonlocal released
            try:
                await gate.acquire(1, reserve)
            finally:
                if owns:
                    released = True
                gate.notify()
        task = asyncio.create_task(owner())
        await asyncio.sleep(0)
        can_reserve = True
        gate.notify()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(released)
        self.assertEqual(gate.pending, 0)


class BoundedMetrics(unittest.TestCase):
    def observe(self, metrics, **kwargs):
        metrics.observe(**dict(protocol='responses', traffic='inference', http_status=200,
                               terminal='completed', latency_ms=1500, **kwargs))

    def test_expiring_windows_do_not_reset_counters(self):
        now = [0.0]
        metrics = runtimemetrics.Metrics(clock=lambda: now[0])
        self.observe(metrics, first_token_ms=100)
        now[0] = 90000
        text = '\n'.join(metrics.render())
        self.assertIn('workbuddy_requests_total 1', text)
        self.assertIn('workbuddy_requests_last_hour 0', text)
        self.assertIn('# TYPE workbuddy_errors_5xx_24h gauge', text)
        self.assertIn('workbuddy_first_token_seconds_count{protocol="responses"} 1', text)

    def test_metrics_cardinality_and_samples_are_bounded(self):
        metrics = runtimemetrics.Metrics()
        for i in range(1000):
            self.observe(metrics, model='model-'+str(i), rules=('rule-'+str(i),))
        self.assertLessEqual(len(metrics.models), 64)
        self.assertLessEqual(len(metrics.rules), 97)
        self.assertLessEqual(len(next(iter(metrics.minutes.values()))), 71)
        self.assertIn('model="other"', '\n'.join(metrics.render()))

    def test_stream_failure_and_cancel_are_separate_from_success(self):
        metrics = runtimemetrics.Metrics()
        for terminal in ('completed','protocol_incomplete','client_cancelled'):
            metrics.observe(protocol='responses', traffic='inference', http_status=200,
                            terminal=terminal, latency_ms=100)
        self.assertEqual(metrics.counts['requests'], 3)
        self.assertEqual(metrics.counts['5xx'], 1)
        self.assertEqual(metrics.outcomes['responses','inference','4xx'], 1)


class ContextDeclarations(unittest.TestCase):
    def test_sensitive_operation_rejects_old_or_missing_human_auth(self):
        from fastapi import HTTPException
        from server import security
        with mock.patch.object(security.time, 'time', return_value=10000):
            for age in (None, 9099, 10001):
                with self.assertRaises(HTTPException) as caught:
                    security.require_recent_session_admin(None, {'_session_auth_at':age})
                self.assertEqual(caught.exception.status_code, 403)
            user = {'_session_auth_at':9999}
            self.assertIs(security.require_recent_session_admin(None, user), user)

    def test_new_receipt_annex_does_not_extend_legacy_log_kwargs(self):
        from server import ledgerreplay
        facts = {'request_id':'compat-receipt', 'credit':1.25,
                 'compatibility_version':promptcompat.VERSION, 'compatibility_rules':'["codex-identity"]'}
        entry = ledgerreplay.build_entry(facts)
        self.assertNotIn('compatibility_version', entry['facts'])
        self.assertNotIn('compatibility_rules', entry['facts'])
        self.assertEqual(entry['facts']['credit'], 1.25)
        self.assertEqual(entry['diagnostics']['compatibility_version'], promptcompat.VERSION)

    def test_ceiling_default_options_and_conflict_are_distinct(self):
        window = {'defaultLength':300000, 'supportedLengths':[1000000,300000,300000]}
        self.assertEqual(modelcontext.optional_fields(1000000, window), {
            'default_context_length':300000, 'context_length_options':[300000,1000000]})
        window['defaultLength'] = 2000000
        result = modelcontext.optional_fields(1000000, window)
        self.assertNotIn('default_context_length', result)
        self.assertTrue(result['context_window_conflict'])
        self.assertEqual(modelcontext.optional_fields(0, None), {})


class CompatibilityBundle(unittest.TestCase):
    def test_fresh_install_all_builtins_are_scoped_and_idempotent(self):
        for rule_id, pattern, replacement in promptcompat.BUILTINS:
            with self.subTest(rule=rule_id):
                messages = [{'role': role, 'content': pattern} for role in ('system', 'developer', 'user', 'assistant', 'tool')]
                messages[3]['tool_calls'] = [{'function': {'arguments': json.dumps({'port': 11128, 'text': pattern})}}]
                messages[4]['content'] = [{'type': 'text', 'text': pattern}, {'type': 'image_url', 'image_url': {'url': pattern}}]
                expected = copy.deepcopy(messages)
                expected[0]['content'] = expected[1]['content'] = replacement
                body = {'messages': messages}
                applied = promptcompat.apply(body, platform='workbuddy', get_setting=lambda _: None)
                self.assertEqual(body['messages'], expected)
                self.assertIn(rule_id, applied.rules)
                self.assertEqual(promptcompat.apply(body, platform='workbuddy', get_setting=lambda _: None).changes, 0)

    def test_custom_override_is_preserved_and_hot_disable_is_immediate(self):
        pattern = promptcompat.BUILTINS[1][1]
        settings = {'prompt_defuse_rules': [[pattern, 'Operator-approved wording']]}
        body = {'messages': [{'role': 'system', 'content': pattern}]}
        result = promptcompat.apply(body, platform='workbuddy', get_setting=settings.get)
        self.assertEqual(body['messages'][0]['content'], 'Operator-approved wording')
        self.assertTrue(result.rules[0].startswith('custom-'))
        settings['prompt_compat_enabled'] = False
        body = {'messages': [{'role': 'system', 'content': pattern}]}
        self.assertEqual(promptcompat.apply(body, platform='workbuddy', get_setting=settings.get).changes, 0)
        self.assertEqual(body['messages'][0]['content'], pattern)

    def test_other_providers_never_read_or_apply_compatibility(self):
        body = {'messages': [{'role': 'system', 'content': promptcompat.BUILTINS[1][1]}]}
        before = copy.deepcopy(body)
        for platform in ('qoder', 'trae', 'other'):
            self.assertEqual(promptcompat.apply(body, platform=platform, get_setting=lambda _: self.fail('no settings read')).changes, 0)
            self.assertEqual(body, before)

    def test_duplicate_hot_rules_keep_builtin_diagnostic_ids(self):
        rule_id, pattern, replacement = promptcompat.BUILTINS[1]
        body = {'messages': [{'role': 'system', 'content': pattern}]}
        result = promptcompat.apply(body, platform='workbuddy', get_setting=lambda name: [[pattern, replacement]] if name == 'prompt_defuse_rules' else None)
        self.assertEqual(result.rules, (rule_id,))
        self.assertEqual(result.changes, 1)


class StatusDisplay(unittest.TestCase):
    def test_legacy_rejection_is_explained_without_changing_facts(self):
        for status in (400, 401, 403, 429):
            row = {'status': 502, 'http_status': status, 'terminal_state': 'upstream_error'}
            before = dict(row)
            self.assertEqual(logview.displayed_status(row), status)
            self.assertEqual(row, before)
        self.assertEqual(logview.displayed_status({'status': 502, 'http_status': 200, 'terminal_state': 'upstream_error'}), 502)
        self.assertEqual(logview.displayed_status({'status': 499, 'http_status': 200, 'terminal_state': 'client_cancelled'}), 499)


class OwnedHeartbeat(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_read_has_heartbeats_and_data_remains_exact(self):
        close = asyncio.Event()
        first, done = asyncio.Event(), asyncio.Event()
        async def upstream():
            try:
                await first.wait()
                yield b'data: {"first":1}\n\n'
                await done.wait()
                yield b'data: [DONE]\n\n'
            finally:
                close.set()
        iterator = sseheartbeat.comments(upstream(), interval=.01)
        chunks = [await anext(iterator), await anext(iterator)]
        first.set()
        async with asyncio.timeout(1):
            while b'data: {"first":1}\n\n' not in chunks:
                chunks.append(await anext(iterator))
        chunks.append(await anext(iterator))
        self.assertEqual(chunks[-1], b': keepalive\n\n')
        done.set()
        chunks.extend([chunk async for chunk in iterator])
        self.assertGreaterEqual(chunks.count(b': keepalive\n\n'), 2)
        self.assertEqual(b''.join(x for x in chunks if not x.startswith(b':')), b'data: {"first":1}\n\ndata: [DONE]\n\n')
        self.assertTrue(close.is_set())

    async def test_cancel_and_close_join_the_owned_read(self):
        closed = asyncio.Event()
        async def upstream():
            try:
                await asyncio.Event().wait()
                yield b'never'
            finally:
                await asyncio.sleep(.01)
                closed.set()
        iterator = sseheartbeat.comments(upstream(), interval=.005)
        self.assertEqual(await anext(iterator), b': keepalive\n\n')
        await iterator.aclose()
        self.assertTrue(closed.is_set())

    async def test_upstream_error_is_not_completion_or_retry(self):
        count = 0
        async def upstream():
            nonlocal count
            count += 1
            await asyncio.sleep(.015)
            raise OSError('accepted then disconnected')
            yield b'never'
        with self.assertRaises(OSError):
            async for _ in sseheartbeat.comments(upstream(), interval=.005):
                pass
        self.assertEqual(count, 1)

    async def test_disabled_heartbeat_does_not_alter_stream(self):
        async def upstream():
            yield b'data: exactly\n\n'
        self.assertEqual([x async for x in sseheartbeat.comments(upstream(), interval=0)], [b'data: exactly\n\n'])


if __name__ == '__main__':
    unittest.main()
