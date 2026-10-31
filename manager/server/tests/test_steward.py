"""Synthetic only: no QQ SMTP, production data, credentials or model requests."""
import asyncio
from email import policy as email_policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import smtplib
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import budget, config, db, operational, security, steward_ai, steward_config, steward_mail, steward_rules, steward_runtime, steward_snapshot, steward_store, tokensvc, userroles
from server.services.asyncwork import joined_thread
from server.routers import steward
from server.tests.authorization_test_support import isolated_data, install_users, owner


def snapshot():
    return {'observed_at': int(time.time()),
            'resources': {'disk': {'used_percent': 50, 'free_bytes': 20 * 1024**3},
                          'memory': {'used_percent': 40}},
            'traffic': {'requests': 100, 'failed': 0, 'failure_percent': 0,
                        'p95_total_latency_ms': 500, 'latency_samples': 100},
            'budget': {'unknown_count': 0},
            'keys': {'total': 1}, 'release': {'version': 'fixture'},
            'ledger': {'state': 'healthy', 'writable': True, 'pending': 0, 'dead': 0},
            'backup': {'status': 'ok'},
            'certificate': {'status': 'ok', 'days_left': 47},
            'platforms': [{'name': name, 'state': 'up', 'pool': {'total': 1}}
                          for name in ('workbuddy', 'qoder', 'trae')]}


class PolicyTests(unittest.TestCase):
    def test_default_off_and_deepseek_is_explicit(self):
        selected = steward_config.validate_policy({})
        self.assertFalse(selected.enabled)
        self.assertFalse(selected.ai_analysis)
        self.assertIn('deepseek', selected.model)

    def test_recipients_deduplicated_no_header_injection(self):
        self.assertEqual(steward_config.Policy.emails(['User@Example.COM', 'User@example.com']), ['User@example.com'])
        for bad in ['x@example.com\r\nBcc:x@evil.com', 'Name <x@example.com>', 'x@localhost', '..x@example.com', 'x@example..com', '', 'a@x.com, b@x.com']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                steward_config.validate_policy({'recipients': [bad]})

    def test_invalid_settings_are_not_normalized_to_unlimited(self):
        for values in ({'enabled': True}, {'interval_seconds': '60'}, {'interval_seconds': 0},
                       {'disk_warning_percent': 90, 'disk_critical_percent': 80},
                       {'model': 'gpt-6'}, {'model': 'http://evil/deepseek'}, {'smtp_host': 'evil'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                steward_config.validate_policy(values)

    def test_secret_file_rejects_unsafe_type_and_mode(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secret'
            path.write_text('fixture')
            if os.name == 'posix':
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    steward_config.secret_file(path)
                path.chmod(0o600)
            self.assertEqual(steward_config.secret_file(path), 'fixture')
            with self.assertRaises((ValueError, OSError)):
                steward_config.secret_file(Path(directory))

    def test_smtp_endpoint_and_identity_are_strict(self):
        good = {'host': 'smtp.qq.com', 'port': 465, 'username': 'sender@Example.COM', 'password': 'synthetic'}
        self.assertEqual(steward_config.validate_smtp(good)['username'], 'sender@example.com')
        self.assertEqual(steward_config.validate_smtp({**good, 'host': 'smtp.163.com'})['host'], 'smtp.163.com')
        for changes in ({'port': 465.0}, {'port': '465'}, {'host': 'evil.invalid'},
                        {'host': 'smtp.163.com', 'port': 587}, {'host': []}, {'password': 'x' * 4097},
                        {'username': None}, {'username': 'sender@example.com\nBcc:x@example.com'}, {'password': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                steward_config.validate_smtp({**good, **changes})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        db.connect()
        steward_store.ensure_schema()

    def signal(self, active=True, severity='warning', recipients=('fixture@example.com',), **kwargs):
        return steward_store.observe('disk', active, severity, '磁盘', 'fixture', recipients=recipients, **kwargs)

    def test_transition_dedup_recovery_recurrence(self):
        for _ in range(10):
            self.signal()
        self.assertEqual(len(steward_store.incidents()), 1)
        self.assertEqual(len(steward_store.deliveries()), 1)
        self.signal(False)
        self.signal(False)
        self.assertEqual(len(steward_store.deliveries()), 2)
        self.signal()
        self.assertEqual(len(steward_store.incidents()), 2)
        self.assertEqual(len(steward_store.deliveries()), 3)

    def test_existing_fault_is_reported_to_new_recipient(self):
        self.signal(recipients=())
        self.signal()
        self.signal(recipients=('new@example.com',))
        self.assertEqual(len(steward_store.deliveries()), 2)
        self.assertNotIn('recipient', steward_store.deliveries()[0])

    def test_escalation_bypasses_prior_delivery_dedup(self):
        self.signal()
        self.signal(severity='critical')
        self.assertEqual({r['event'] for r in steward_store.deliveries()}, {'firing', 'escalated'})

    def test_retry_is_bounded_unknown_is_not_replayed(self):
        self.signal(now=100)
        item = steward_store.claim('owner', now=101)
        self.assertIsNone(steward_store.claim('other', now=102))
        steward_store.finish(item, {'status': 'retry'}, now=103)
        self.assertIsNone(steward_store.claim('other', now=105))
        item = steward_store.claim('owner', now=1000)
        steward_store.finish(item, {'status': 'unknown'}, now=1001)
        self.assertIsNone(steward_store.claim('owner', now=2000))
        self.assertEqual(steward_store.deliveries()[0]['status'], 'unknown')

    def test_crash_recovery_and_owner_fencing(self):
        self.signal(now=100)
        item = steward_store.claim('old', now=101)
        steward_store.recover_interrupted()
        self.assertEqual(steward_store.deliveries()[0]['status'], 'unknown')
        with self.assertRaises(RuntimeError):
            steward_store.finish(item, {'status': 'accepted'})

    def test_retry_budget_exhaustion_and_cas_settings(self):
        self.signal(now=1)
        for now in (2, 1000, 3000):
            item = steward_store.claim('o', now=now)
            steward_store.finish(item, {'status': 'retry'}, now=now)
        self.assertEqual(steward_store.deliveries()[0]['status'], 'failed')
        first = steward_config.save({})
        self.assertEqual(first.revision, 1)
        with self.assertRaises(ValueError):
            steward_config.save({})

    def test_rules_no_unknown_zero_no_hidden_archived_budget(self):
        current = snapshot()
        selected = steward_config.Policy()
        self.assertFalse(any(s.active for s in steward_rules.evaluate(current, selected)))
        current['budget']['unknown_count'] = 216
        current['backup']['status'] = 'unknown'
        current['platforms'][1]['state'] = 'unknown'
        active = {s.rule for s in steward_rules.evaluate(current, selected) if s.active}
        self.assertEqual(active, {'budget_evidence', 'backup_evidence', 'platform_qoder'})

    def test_debounce_and_low_free_space_critical(self):
        runtime = steward_runtime.StewardRuntime(None)
        current = snapshot()
        current['resources']['disk']['free_bytes'] = 1024
        runtime.observe(current, steward_config.Policy())
        self.assertEqual(steward_store.incidents(), [])
        runtime.observe(current, steward_config.Policy())
        self.assertEqual(steward_store.incidents()[0]['severity'], 'critical')

    def test_no_samples_and_missing_memory_cannot_resolve_a_fault(self):
        runtime = steward_runtime.StewardRuntime(None)
        current = snapshot()
        current['resources']['memory']['used_percent'] = 99
        current['traffic']['failure_percent'] = 50
        current['traffic']['p95_total_latency_ms'] = 60000
        for _ in range(2):
            runtime.observe(current, steward_config.Policy())
        current['resources']['memory']['used_percent'] = None
        current['traffic'].update(requests=0, latency_samples=0, failure_percent=None, p95_total_latency_ms=None)
        for _ in range(3):
            runtime.observe(current, steward_config.Policy())
        states = {row['rule']: row['state'] for row in steward_store.incidents()}
        self.assertTrue(all(states[name] == 'open' for name in ('memory_capacity', 'inference_failure', 'inference_latency')))
        self.assertEqual(states['memory_evidence'], 'open')

    def test_certificate_expiry_unknown_never_fabricates_recovery(self):
        runtime = steward_runtime.StewardRuntime(None)
        current = snapshot()
        current['certificate'] = {'status': 'ok', 'days_left': 2}
        for _ in range(2):
            runtime.observe(current, steward_config.Policy())
        self.assertEqual(steward_store.incidents()[0]['severity'], 'critical')
        current['certificate'] = {'status': 'stale', 'days_left': 47}
        for _ in range(3):
            runtime.observe(current, steward_config.Policy())
        states = {row['rule']: row['state'] for row in steward_store.incidents()}
        self.assertEqual(states['certificate_expiry'], 'open')
        self.assertEqual(states['certificate_evidence'], 'open')
        current['certificate'] = {'status': 'ok', 'days_left': 90}
        for _ in range(2):
            runtime.observe(current, steward_config.Policy())
        self.assertTrue(all(row['state'] == 'resolved' for row in steward_store.incidents()))

    def test_certificate_projection_bounds_age_types_and_excludes_private_fields(self):
        from unittest.mock import mock_open
        receipt = {'not_after': 3000000, 'generated_at': 1000000,
                   'domain': 'private-domain', 'private_key': 'not-read'}
        with patch.object(Path, 'open', mock_open(read_data=json.dumps(receipt).encode())):
            value = steward_snapshot.certificate_fact(1000100)
        self.assertEqual(value['status'], 'ok')
        self.assertFalse(value['served_chain_verified'])
        self.assertNotIn('private', json.dumps(value))
        for changed in ({'generated_at': 2000000}, {'not_after': True}, {'generated_at': None}):
            with self.subTest(changed=changed), patch.object(Path, 'open', mock_open(
                    read_data=json.dumps({**receipt, **changed}).encode())):
                value = steward_snapshot.certificate_fact(1000100)
                self.assertEqual((value['status'], value['days_left']), ('unknown', None))
        with patch.object(Path, 'open', mock_open(read_data=json.dumps(receipt).encode())):
            self.assertEqual(steward_snapshot.certificate_fact(2000000)['status'], 'stale')

    def test_rule_mail_does_not_wait_for_analysis_and_recovery_cancels_stale_followup(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True, 'recipients': ['fixture@example.com']})
        self.signal(analyze=True)
        item = steward_store.claim_analysis('model-owner')
        rule_mail = steward_store.claim('mail-owner')
        self.assertEqual(rule_mail['event'], 'firing')
        self.assertNotIn('analysis', json.loads(rule_mail['payload']))
        steward_store.finish(rule_mail, {'status': 'accepted'})
        self.signal(False, analyze=True)
        steward_store.finish_analysis(item, 'outdated conclusion', revision=selected.revision,
                                      recipients=('fixture@example.com',))
        self.assertEqual(steward_store.analyses()[-1]['status'], 'cancelled')
        self.assertFalse(any(row['event'] == 'analysis-firing' for row in steward_store.deliveries()))

    def test_analysis_separate_delivery_owner_cas_and_rule_priority(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True,
                                       'recipients': ['fixture@example.com', 'other@example.com']})
        self.signal(analyze=True)
        original = steward_store.claim('mail')
        steward_store.finish(original, {'status': 'accepted'})
        analysis = steward_store.claim_analysis('model')
        steward_store.finish_analysis(analysis, '可能原因，不是根因', revision=selected.revision,
                                      recipients=('fixture@example.com',))
        with self.assertRaises(RuntimeError):
            steward_store.finish_analysis(analysis, 'duplicate', revision=selected.revision,
                                          recipients=('other@example.com',))
        steward_store.observe('new-fault', True, 'critical', 'new', 'fixture', recipients=('fixture@example.com',))
        self.assertEqual(steward_store.claim('mail')['event'], 'firing')
        self.assertEqual(steward_store.claim('mail')['event'], 'analysis-firing')

    def test_escalation_invalidates_running_and_queued_warning_analysis(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True, 'recipients': ['fixture@example.com']})
        self.signal(analyze=True)
        running = steward_store.claim_analysis('model')
        self.signal(severity='critical', analyze=True)
        # A later lower measurement must not resurrect this old warning job.
        self.signal(severity='warning', analyze=True)
        self.assertFalse(steward_store.analysis_is_current(running))
        steward_store.finish_analysis(running, 'outdated warning', revision=selected.revision,
                                      recipients=selected.recipients)
        self.assertEqual(db.query_one('SELECT status FROM steward_analysis WHERE id=?', (running['id'],))['status'],
                         'cancelled')
        self.assertFalse(any(row['event'] == 'analysis-firing' for row in steward_store.deliveries()))
        # A completed warning already waiting for SMTP is also superseded.
        steward_store.observe('other', True, 'warning', 'fixture', 'fixture',
                              recipients=selected.recipients, analyze=True)
        # The prior escalation job is no longer the current severity; finish it
        # before claiming the unrelated warning fixture.
        item = steward_store.claim_analysis('model')
        steward_store.finish_analysis(item, None, revision=selected.revision)
        item = steward_store.claim_analysis('model')
        steward_store.finish_analysis(item, 'warning', revision=selected.revision, recipients=selected.recipients)
        steward_store.observe('other', True, 'critical', 'fixture', 'fixture',
                              recipients=selected.recipients, analyze=True)
        self.assertTrue(all(row['status'] == 'cancelled' for row in steward_store.deliveries()
                            if row['event'] == 'analysis-firing'))

    def test_policy_change_cannot_attach_old_answer_to_new_or_removed_recipient(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True, 'recipients': ['fixture@example.com']})
        self.signal(analyze=True)
        item = steward_store.claim_analysis('model')
        steward_config.save({**selected.model_dump(), 'recipients': ['replacement@example.com']})
        steward_store.finish_analysis(item, 'old private analysis', revision=selected.revision,
                                      recipients=('replacement@example.com', 'fixture@example.com'))
        self.assertEqual(steward_store.analyses()[0]['status'], 'cancelled')
        self.assertFalse(any(row['event'].startswith('analysis-') for row in steward_store.deliveries()))

    def test_no_dispatch_deferral_preserves_job_and_owner_fence(self):
        self.signal(analyze=True)
        item = steward_store.claim_analysis('old')
        steward_store.defer_analysis(item)
        self.assertEqual(steward_store.analyses()[0]['status'], 'pending')
        replacement = steward_store.claim_analysis('new')
        with self.assertRaises(RuntimeError):
            steward_store.defer_analysis(item)
        self.assertEqual(db.query_one('SELECT owner FROM steward_analysis WHERE id=?', (replacement['id'],))['owner'], 'new')

    def test_already_enqueued_recipient_does_not_overflow_full_queue(self):
        self.signal()
        with db.transaction() as connection:
            connection.executemany('INSERT INTO steward_deliveries(event,recipient,payload,next_at,created_at) VALUES(?,?,?,?,?)',
                                  [('test-' + str(n), 'fixture@example.com', '{}', 1, 1) for n in range(499)])
        self.signal()
        with self.assertRaises(RuntimeError):
            self.signal(recipients=('new@example.com',))

    def test_future_snapshot_is_stale(self):
        db.set_setting(steward_runtime.SNAPSHOT_KEY, {**snapshot(), 'observed_at': int(time.time()) + 3600})
        self.assertTrue(steward_runtime.snapshot()['stale'])

    def test_snapshot_freshness_accounts_for_configured_collection_interval(self):
        current = {**snapshot(), 'observed_at': 1000, 'collection_interval_seconds': 300}
        db.set_setting(steward_runtime.SNAPSHOT_KEY, current)
        with patch('server.steward_runtime.time.time', return_value=1300):
            self.assertFalse(steward_runtime.snapshot()['stale'])
        with patch('server.steward_runtime.time.time', return_value=1376):
            self.assertTrue(steward_runtime.snapshot()['stale'])


class FakeSMTP:
    def __init__(self, *args, **kwargs):
        self.raw = None
        self.closed = False

    def ehlo(self):
        return 250, b'ok'

    def login(self, *args):
        return 235, b'ok'

    def mail(self, *args):
        return 250, b'ok'

    def rcpt(self, *args):
        return 250, b'ok'

    def data(self, raw):
        self.raw = raw
        return 250, b'ok'

    def close(self):
        self.closed = True


class MailTests(unittest.TestCase):
    settings = {'host': 'smtp.qq.com', 'port': 465, 'username': 'fixture@example.com', 'password': 'synthetic'}
    event = {'title': '磁盘<script>alert(1)</script>', 'detail': 'password=synthetic-secret wbk_testlongsecretvalue',
             'observed_at': 1700000000, 'severity': 'critical', 'event': 'firing'}

    def test_email_brand_escape_and_no_secret_or_remote_assets(self):
        rendered = steward_mail.render(self.event, '可能原因；不是已确认根因')
        self.assertIn('智汇管家', rendered['subject'])
        self.assertNotIn('<script>', rendered['html'])
        self.assertNotIn('synthetic-secret', rendered['html'])
        self.assertNotIn('大肥鱼', rendered['text'] + rendered['html'])
        self.assertNotIn('<img', rendered['html'])
        self.assertIn('AI 辅助', rendered['html'])

    def test_smtp_acceptance_has_plain_and_html_alternatives(self):
        smtp = FakeSMTP()
        result = steward_mail.send(self.settings, 'target@example.com', self.event, delivery_id=1, smtp_factory=lambda *a, **k: smtp)
        self.assertEqual(result['status'], 'accepted')
        parsed = BytesParser(policy=email_policy.default).parsebytes(smtp.raw)
        self.assertIn('智汇管家', parsed['From'])
        self.assertEqual([part.get_content_type() for part in parsed.iter_parts()], ['text/plain', 'text/html'])
        self.assertTrue(smtp.closed)

    def test_after_data_timeout_is_unknown_never_safe_retry(self):
        smtp = FakeSMTP()
        smtp.data = Mock(side_effect=TimeoutError('private-server-secret'))
        result = steward_mail.send(self.settings, 'target@example.com', self.event, delivery_id=1, smtp_factory=lambda *a, **k: smtp)
        self.assertEqual(result, {'status': 'unknown', 'code': 'smtp_acceptance_unknown'})
        self.assertNotIn('private-server-secret', json.dumps(result))

    def test_before_data_failure_can_retry_no_rcpt_after_mail_rejection(self):
        smtp = FakeSMTP()
        smtp.mail = Mock(return_value=(550, b'no'))
        smtp.rcpt = Mock()
        result = steward_mail.send(self.settings, 'target@example.com', self.event, delivery_id=2, smtp_factory=lambda *a, **k: smtp)
        self.assertEqual(result['status'], 'rejected')
        smtp.rcpt.assert_not_called()

    def test_starttls_precedes_login_and_data_rejections_are_explicit(self):
        smtp = FakeSMTP()
        steps = []
        smtp.starttls = Mock(side_effect=lambda **kw: steps.append('tls'))
        smtp.login = Mock(side_effect=lambda *a: steps.append('login'))
        for code, expected in ((451, 'retry'), (550, 'rejected')):
            smtp.data = Mock(return_value=(code, b'private rejection body'))
            result = steward_mail.send({**self.settings, 'port': 587}, 'target@example.com', self.event,
                                       delivery_id=code, smtp_factory=lambda *a, **kw: smtp)
            self.assertEqual(result['status'], expected)
            self.assertNotIn('private rejection', json.dumps(result))
        self.assertEqual(steps, ['tls', 'login', 'tls', 'login'])

    def test_unexpected_exception_after_data_is_unknown_before_data_is_retry(self):
        for name, expected in (('login', 'retry'), ('data', 'unknown')):
            smtp = FakeSMTP()
            setattr(smtp, name, Mock(side_effect=RuntimeError('private transport failure')))
            result = steward_mail.send(self.settings, 'target@example.com', self.event,
                                       delivery_id=name, smtp_factory=lambda *a, **kw: smtp)
            self.assertEqual(result['status'], expected)
            self.assertNotIn('private transport failure', json.dumps(result))


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patcher = patch('server.steward_config.policy', return_value=steward_config.Policy())
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_tool_loop_uses_finite_safe_projection_only(self):
        call = {'role': 'assistant', 'tool_calls': [{'id': 'call1', 'type': 'function',
                'function': {'name': 'platform_pools', 'arguments': '{}'}}]}
        with patch.object(steward_ai, '_request', AsyncMock(side_effect=[call, {'content': '已核实：三平台探针可用。'}])) as request:
            result = await steward_ai.answer('网关怎么样', snapshot())
        self.assertEqual(result['tools'], ['platform_pools'])
        self.assertEqual(request.await_count, 2)
        self.assertFalse(request.call_args.kwargs['tools'])

    async def test_unknown_tool_nonempty_arguments_and_more_rounds_rejected(self):
        for name, args in [('shell', '{}'), ('gateway_summary', '{"path":"/etc/passwd"}')]:
            first = {'tool_calls': [{'id': 'id', 'type': 'function', 'function': {'name': name, 'arguments': args}}]}
            with patch.object(steward_ai, '_request', AsyncMock(return_value=first)), self.assertRaises(ValueError):
                await steward_ai.answer('hello', snapshot())
        self.assertFalse(steward_ai._answer_busy)

    async def test_reasoning_is_never_shown_as_answer(self):
        with patch.object(steward_ai, '_request', AsyncMock(return_value={'content': '', 'reasoning_content': 'private-thinking'})), self.assertRaises(RuntimeError):
            await steward_ai.answer('hello', snapshot())

    async def test_revocation_after_tool_read_prevents_second_billable_round(self):
        active = True
        def guard():
            if not active:
                from fastapi import HTTPException
                raise HTTPException(401, 'synthetic revocation')
        def tool(*args):
            nonlocal active
            active = False
            return {'synthetic': True}
        first = {'tool_calls': [{'id': 'id', 'type': 'function',
                 'function': {'name': 'gateway_summary', 'arguments': '{}'}}]}
        with patch.object(steward_ai, '_request', AsyncMock(return_value=first)) as request, \
                patch.object(steward_ai, '_tool', side_effect=tool):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as failure:
                await steward_ai.answer('hi', snapshot(), before_dispatch=guard)
        self.assertEqual(failure.exception.status_code, 401)
        self.assertEqual(request.await_count, 1)
        self.assertFalse(steward_ai._answer_busy)

    async def test_dispatch_guard_prevents_initial_request_and_revoked_answer(self):
        from fastapi import HTTPException
        with patch.object(steward_ai, '_request', AsyncMock(return_value={'content': 'fixture'})) as request:
            with self.assertRaises(HTTPException):
                await steward_ai.answer('hi', snapshot(), before_dispatch=Mock(side_effect=HTTPException(401)))
            request.assert_not_called()
            # A single already-sent request cannot be undone; its result is
            # withheld after revocation and no additional model round is sent.
            with self.assertRaises(HTTPException):
                await steward_ai.answer('hi', snapshot(), before_dispatch=Mock(side_effect=[None, HTTPException(401)]))
            self.assertEqual(request.await_count, 1)

    async def test_platform_probes_parallel_with_independent_unknowns(self):
        async def health():
            await asyncio.sleep(.04)
            return {'up': True, 'latency_ms': 2}
        good = SimpleNamespace(health=health, accounts=AsyncMock(return_value={'accounts': [
            {'uid': 'private-uid', 'access_token': 'must-not-leak', 'enabled': True,
             'credits': {'remain': 20}}]}))
        bad = SimpleNamespace(health=AsyncMock(side_effect=RuntimeError('private-secret')), accounts=AsyncMock(return_value={}))
        with patch.object(steward_snapshot, 'local_facts', return_value={'observed_at': 1}):
            started = time.monotonic()
            result = await steward_snapshot.collect(SimpleNamespace(wb=good, qoder=good, trae=bad))
        self.assertLess(time.monotonic() - started, .12)
        self.assertEqual([r['state'] for r in result['platforms']], ['up', 'up', 'unknown'])
        self.assertNotIn('must-not-leak', json.dumps(result))
        self.assertNotIn('private-', json.dumps(result))

    async def test_typed_pool_projection_preserves_unknown_and_real_platform_fields(self):
        wb = steward_snapshot.pool_projection({'accounts': [
            {'disabled': False, 'manual_disabled': False, 'credits': 100},
            {'disabled': False, 'manual_disabled': True, 'credits': None}]}, 'workbuddy')
        self.assertEqual((wb['enabled'], wb['credits_remain'], wb['credits_coverage']), (1, 100, 1))
        trae = steward_snapshot.pool_projection({'accounts': [{'remaining_credits': 23}]}, 'trae')
        self.assertIsNone(trae['enabled'])
        self.assertEqual(trae['credits_remain'], 23)
        self.assertIsNone(steward_snapshot.pool_projection({'accounts': [{}]})['enabled'])
        with self.assertRaises(ValueError):
            steward_snapshot.pool_projection({'accounts': [None]})

    async def test_diagnostic_pool_never_reads_account_credentials_or_history(self):
        from server.qtgcore.adapters.workbuddy import WorkBuddyAdapter
        from server.qtgcore.adapters.trae2 import Trae2Adapter
        wb = object.__new__(WorkBuddyAdapter)
        wb._json = AsyncMock(return_value={'accounts': []})
        wb._auths = Mock(side_effect=AssertionError('credentials read'))
        wb._checkin_rows = Mock(side_effect=AssertionError('unnecessary history read'))
        self.assertEqual((await wb.diagnostic_pool())['scope'], 'engine_loaded_pool')
        self.assertEqual(wb._json.call_args.args[:2], ('GET', '/status'))
        trae = object.__new__(Trae2Adapter)
        trae._cmd = AsyncMock(return_value={'data': []})
        trae._checkin_rows = wb._checkin_rows
        self.assertEqual((await trae.diagnostic_pool())['accounts'], [])
        trae._cmd.return_value = {'ok': False}
        with self.assertRaises(ValueError):
            await trae.diagnostic_pool()


class RuntimeSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        db.connect()
        steward_store.ensure_schema()

    def setup_analysis(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True, 'recipients': ['fixture@example.com']})
        db.set_setting(steward_runtime.SNAPSHOT_KEY, snapshot())
        steward_store.observe('fixture', True, 'warning', 'fixture', 'fixture',
                              recipients=selected.recipients, analyze=True)
        original = steward_store.claim('rule-mail')
        steward_store.finish(original, {'status': 'accepted'})
        return selected, steward_runtime.StewardRuntime(None)

    async def test_model_busy_defers_without_losing_analysis_or_replaying_an_unknown(self):
        _, instance = self.setup_analysis()
        with patch.object(steward_ai, 'answer', AsyncMock(side_effect=steward_ai.ModelBusy('busy'))) as answer:
            await instance.analyze_once()
        self.assertEqual(answer.call_count, 1)
        self.assertEqual(steward_store.analyses()[0]['status'], 'pending')
        with patch.object(steward_ai, 'answer', AsyncMock(side_effect=RuntimeError('dispatch outcome unknown'))) as answer:
            await instance.analyze_once()
            await instance.analyze_once()
        self.assertEqual(answer.call_count, 1)
        self.assertEqual(steward_store.analyses()[0]['status'], 'failed')

    async def test_changed_policy_after_model_return_never_queues_old_analysis(self):
        selected, instance = self.setup_analysis()
        async def completed(*args, **kwargs):
            steward_config.save({**selected.model_dump(), 'latency_warning_ms': 31000})
            return {'answer': 'outdated configuration analysis'}
        with patch.object(steward_ai, 'answer', side_effect=completed):
            await instance.analyze_once()
        self.assertEqual(steward_store.analyses()[0]['status'], 'cancelled')
        self.assertFalse(any(row['event'].startswith('analysis-') for row in steward_store.deliveries()))

    async def test_completed_analysis_cancelled_before_smtp_after_ai_disabled(self):
        selected, instance = self.setup_analysis()
        item = steward_store.claim_analysis('model')
        steward_store.finish_analysis(item, 'analysis', revision=selected.revision, recipients=selected.recipients)
        steward_config.save({**selected.model_dump(), 'ai_analysis': False})
        with patch.object(steward_mail, 'send', side_effect=AssertionError('superseded mail sent')) as send:
            await instance.deliver_once()
        send.assert_not_called()
        self.assertEqual(steward_store.deliveries()[0]['status'], 'cancelled')

    async def test_blocking_write_finishes_before_cancellation_releases_owner(self):
        entered, release = threading.Event(), threading.Event()
        finished = []
        def work():
            entered.set()
            release.wait(2)
            finished.append(True)
        task = asyncio.create_task(joined_thread(work))
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        task.cancel()  # A second cancellation must not detach the owned thread.
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [True])

    async def test_shutdown_waits_for_smtp_and_records_acceptance_before_lease_release(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True, 'recipients': ['fixture@example.com']})
        steward_store.observe('fixture', True, 'critical', 'fixture', 'fixture', recipients=selected.recipients, analyze=True)
        entered, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {'status': 'accepted'}
        instance = steward_runtime.StewardRuntime(None)
        with patch.object(steward_config, 'smtp_settings', return_value={}), \
                patch.object(steward_mail, 'send', side_effect=send), \
                patch.object(steward_ai, 'answer', AsyncMock(side_effect=AssertionError('model delayed rule mail'))):
            instance.tasks = [asyncio.create_task(instance.deliver())]
            while not entered.is_set():
                await asyncio.sleep(.001)
            closing = asyncio.create_task(instance.close())
            await asyncio.sleep(.01)
            self.assertFalse(closing.done())
            release.set()
            await closing
        self.assertEqual(steward_store.deliveries()[0]['status'], 'accepted')

    async def test_runtime_transport_crash_never_uses_configuration_retry_path(self):
        steward_config.save({'enabled': True, 'recipients': ['fixture@example.com']})
        steward_store.observe('fixture', True, 'critical', 'fixture', 'fixture', recipients=('fixture@example.com',))
        instance = steward_runtime.StewardRuntime(None)
        with patch.object(steward_config, 'smtp_settings', return_value={}), \
                patch.object(steward_mail, 'send', side_effect=RuntimeError('possibly accepted')) as send:
            await instance.deliver_once()
            await instance.deliver_once()
        self.assertEqual(steward_store.deliveries()[0]['status'], 'unknown')
        self.assertEqual(send.call_count, 1)

    async def test_extra_diagnostic_tools_are_aggregates_not_credentials_or_writers(self):
        from server.services import dailyplan, renew
        from server.routers import gateway
        db.execute('INSERT INTO task_logs(ts,uid,kind,level,message,dedup_key) VALUES(?,?,?,?,?,?)',
                   (int(time.time()), 'private-identity', 'private-task', 'ok', 'private-token', 'private-fixture-key'))
        with patch.object(dailyplan, 'get_plan', return_value={'checkin': {'enabled': True, 'hour': 5}}), \
                patch.object(renew, 'get_plan', return_value={'enabled': True, 'interval_days': 7}), \
                patch.object(gateway, 'max_body_bytes', return_value=64 * 1024**2):
            outputs = [steward_ai._tool(name, snapshot()) for name in
                       ('security_summary', 'task_schedules', 'configuration_limits')]
        raw = json.dumps(outputs)
        self.assertNotIn('private-', raw)
        self.assertNotIn('password', raw)
        self.assertEqual(outputs[1]['results'][0]['count'], 1)
        self.assertEqual(outputs[2]['application_body_limit_bytes'], 64 * 1024**2)

    async def test_parallel_engine_health_and_independent_failure(self):
        from server.qtgcore.runtime import Runtime
        entered, release = set(), asyncio.Event()
        all_started = asyncio.Event()
        def adapter(name, failing=False):
            async def health():
                entered.add(name)
                if len(entered) == 3:
                    all_started.set()
                await release.wait()
                if failing:
                    raise RuntimeError('private')
                return {'up': True}
            return SimpleNamespace(name=name, health=health)
        runtime = SimpleNamespace(wb=adapter('wb'), qoder=adapter('qoder'), trae=adapter('trae', True))
        task = asyncio.create_task(Runtime.health_snapshot(runtime))
        try:
            await asyncio.wait_for(all_started.wait(), 3)
            self.assertEqual(entered, {'wb', 'qoder', 'trae'})
            self.assertFalse(task.done())
        finally:
            release.set()
            result = await task
        self.assertTrue(result[0]['up'])
        self.assertEqual(result[1]['state'], 'unknown')
        self.assertNotIn('private', json.dumps(result))

    async def test_public_readiness_is_cached_expiring_and_drain_aware(self):
        with patch.object(operational, '_cached_readiness', {'at': 100, 'ok': True}), \
                patch('server.drain.is_draining', return_value=False), \
                patch.object(operational, 'readiness', side_effect=AssertionError('public DB probe')):
            self.assertTrue(operational.public_readiness(now=120))
            self.assertFalse(operational.public_readiness(now=176))
            self.assertFalse(operational.public_readiness(now=99))
            with patch('server.drain.is_draining', return_value=True):
                self.assertFalse(operational.public_readiness(now=120))

    async def test_operational_tick_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        def tick(*args):
            entered.set()
            release.wait(2)
            return 0, 1, 1, {'ok': True}
        with patch.object(operational, '_monitor_tick', side_effect=tick):
            task = asyncio.create_task(operational.monitor())
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        db.connect()
        steward_store.ensure_schema()
        install_users()
        self.user = owner()
        self.app = FastAPI()
        self.app.include_router(steward.router)
        self.app.dependency_overrides[security.current_user] = lambda: self.user
        self.client = TestClient(self.app)

    def test_read_only_role_views_pages_but_cannot_set_send_or_chat(self):
        self.user = {'role': 'account_admin', 'username': 'reader'}
        for path in ('status', 'settings', 'alerts'):
            response = self.client.get('/api/steward/' + path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(userroles.account_route_allowed('GET', '/api/steward/' + path))
        for method, path, body in [('put', 'settings', {}), ('post', 'test-mail', {}), ('post', 'chat', {'question': 'hi'})]:
            response = getattr(self.client, method)('/api/steward/' + path, json=body)
            self.assertEqual(response.status_code, 403)
            self.assertFalse(userroles.account_route_allowed(method, '/api/steward/' + path))

    def test_settings_stale_revision_and_secret_fields_rejected(self):
        result = self.client.put('/api/steward/settings', json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.put('/api/steward/settings', json={}).status_code, 409)
        self.assertEqual(self.client.put('/api/steward/settings', json={'password': 'bad'}).status_code, 422)

    def test_readers_cannot_read_recipients_or_submit_service_tokens(self):
        steward_config.save({'recipients': ['private@example.com']})
        self.user = {'role': 'account_admin', 'username': 'reader'}
        response = self.client.get('/api/steward/settings')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('private@example.com', response.text)
        self.user = {'role': 'admin', 'username': 'machine', 'scope': 'admin'}
        # Real current_user marks token_id for API tokens. This test ensures the
        # route's session guard, not the UI, rejects it.
        with patch.object(security, 'require_admin', return_value=self.user):
            self.user['role'] = 'viewer'
            self.assertEqual(self.client.post('/api/steward/test-mail').status_code, 403)

    def test_expired_snapshot_does_not_invoke_model(self):
        with patch.object(steward_ai, 'answer', AsyncMock()) as call:
            response = self.client.post('/api/steward/chat', json={'question': 'hi'})
        self.assertEqual(response.status_code, 503, response.text)
        call.assert_not_called()
        self.assertFalse(steward._chat_active)

    def test_status_declares_exact_read_only_tools_and_history_matches_answer_limit(self):
        response = self.client.get('/api/steward/status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['allowed_tools'], [tool['function']['name'] for tool in steward_ai.TOOLS])
        self.assertFalse(response.json()['write_tools_available'])
        steward._last_call.clear()
        db.set_setting(steward_runtime.SNAPSHOT_KEY, snapshot())
        with patch.object(steward_config, 'capabilities', return_value={'model_configured': True}), \
                patch.object(steward_ai, 'answer', AsyncMock(return_value={'answer': 'fixture'})) as model:
            response = self.client.post('/api/steward/chat', json={'question': 'continue',
                'history': [{'role': 'assistant', 'content': 'a' * 6000}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(model.call_args.kwargs['history'][0]['content']), 6000)
        for role, length in [('user', 2001), ('assistant', 6001)]:
            response = self.client.post('/api/steward/chat', json={'question': 'continue',
                'history': [{'role': role, 'content': 'a' * length}]})
            self.assertEqual(response.status_code, 422)

    def test_model_busy_returns_no_dispatch_retry_hint_and_releases_chat_slot(self):
        steward._last_call.clear()
        db.set_setting(steward_runtime.SNAPSHOT_KEY, snapshot())
        with patch.object(steward_config, 'capabilities', return_value={'model_configured': True}), \
                patch.object(steward_ai, 'answer', AsyncMock(side_effect=steward_ai.ModelBusy('busy'))):
            response = self.client.post('/api/steward/chat', json={'question': 'fixture'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '3')
        self.assertFalse(steward._chat_active)

    def test_real_machine_tokens_cannot_access_human_steward_endpoints(self):
        self.app.dependency_overrides.clear()
        for scope in tokensvc.SCOPES:
            token = tokensvc.create_token('fixture-' + scope, scope)['token']
            for method, path, body in [('GET', 'status', None), ('GET', 'alerts', None), ('GET', 'settings', None),
                                       ('POST', 'chat', {'question': 'hi'}), ('PUT', 'settings', {}), ('POST', 'test-mail', None)]:
                with self.subTest(scope=scope, method=method, path=path):
                    response = self.client.request(method, '/api/steward/' + path, json=body,
                                                   headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status_code, 403, response.text)

    def test_model_result_is_not_returned_after_user_revocation(self):
        steward._last_call.clear()
        db.set_setting(steward_runtime.SNAPSHOT_KEY, snapshot())
        async def revoke(*args, **kwargs):
            users = security.load_users(allow_bootstrap=False)
            users['users'][0]['sv'] += 1
            security.save_users(users)
            return {'answer': 'must-not-return'}
        with patch.object(steward_config, 'capabilities', return_value={'model_configured': True, 'smtp_configured': False}), \
                patch.object(steward_ai, 'answer', side_effect=revoke):
            response = self.client.post('/api/steward/chat', json={'question': 'hi'})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertNotIn('must-not-return', response.text)

    def test_revocation_while_loading_capabilities_prevents_model_dispatch(self):
        steward._last_call.clear()
        db.set_setting(steward_runtime.SNAPSHOT_KEY, snapshot())
        def revoke():
            users = security.load_users(allow_bootstrap=False)
            users['users'][0]['sv'] += 1
            security.save_users(users)
            return {'model_configured': True, 'smtp_configured': False}
        with patch.object(steward_config, 'capabilities', side_effect=revoke), \
                patch.object(steward_ai, 'answer', AsyncMock()) as model:
            response = self.client.post('/api/steward/chat', json={'question': 'hi'})
        self.assertEqual(response.status_code, 401, response.text)
        model.assert_not_called()
        self.assertFalse(steward._chat_active)


if __name__ == '__main__':
    unittest.main()
