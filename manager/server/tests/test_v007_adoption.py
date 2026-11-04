import asyncio
import copy
import time
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from fastapi import HTTPException

from server import diagnostics, observability, protocolcontracts, runtimemetrics
from server.routers import accounts, gateway
from server.services import credits


class CreditDemandTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_snapshot_never_labels_another_credentials_value_as_fresh(self):
        auth = {'uid': 'scoped', 'realm': 'cn', 'token': 'synthetic'}
        other = dict(auth, realm='global')
        with mock.patch.object(accounts, '_group', return_value={}), \
                mock.patch.object(accounts, '_group_dir', return_value='synthetic'), \
                mock.patch.object(accounts.wb2api, 'list_auth_accounts', return_value=[{'uid': 'scoped', 'file': 'one'}]), \
                mock.patch.object(accounts.wb2api, 'read_account_file', return_value=auth), \
                mock.patch.object(accounts, '_auth_dict', side_effect=lambda value: value), \
                mock.patch.object(credits, 'snapshot_entries', return_value={'accounts': []}), \
                mock.patch.object(credits, 'get_credits', side_effect=AssertionError('snapshot is offline')), \
                mock.patch.dict(credits._cache, {'scoped': (time.time(), True, 0, '', [])}, clear=True), \
                mock.patch.dict(credits._cache_identity, {'scoped': credits._auth_identity(other)}, clear=True):
            result = accounts.cached_group_credits(upstream_id=None, user={'role': 'admin'})
            self.assertEqual(result['credits'], {})
            self.assertFalse(result['provider_called'])
            credits._cache_identity['scoped'] = credits._auth_identity(auth)
            result = accounts.cached_group_credits(upstream_id=None, user={'role': 'admin'})
            self.assertEqual(result['credits']['scoped'], 0)
            self.assertEqual(result['meta']['scoped']['source'], 'cache')

    async def test_automatic_batch_is_bounded_and_zero_stays_known(self):
        rows = [{'uid': str(index), 'file': str(index)} for index in range(100)]
        calls = []

        async def fetch(auth, **kwargs):
            calls.append(auth['uid'])
            return True, 7, '', False, 0, []

        def state(auth):
            return ('fresh', (True, 0, '', [])) if auth['uid'] == '0' else ('stale', None)

        with mock.patch.object(accounts, '_group', return_value={}), \
                mock.patch.object(accounts, '_require_dir', return_value='synthetic'), \
                mock.patch.object(accounts.wb2api, 'list_auth_accounts', return_value=rows), \
                mock.patch.object(accounts.wb2api, 'read_account_file', side_effect=lambda name, root: {'uid': name}), \
                mock.patch.object(accounts, '_auth_dict', side_effect=lambda value: value), \
                mock.patch.object(credits, 'background_state', side_effect=state), \
                mock.patch.object(credits, 'get_credits', side_effect=fetch):
            result = await accounts.refresh_all_credits(force=False, upstream_id=None, stale_only=True, user={'role': 'admin'})
        self.assertEqual(len(calls), 64)
        self.assertEqual(result['credits']['0'], 0)
        self.assertEqual(result['credits']['99'], None)

    async def test_automatic_mode_cannot_force_refresh(self):
        with self.assertRaises(HTTPException) as caught:
            await accounts.refresh_all_credits(force=True, stale_only=True, user={'role': 'admin'})
        self.assertEqual(caught.exception.status_code, 400)

    async def test_failed_background_is_not_requeried_during_backoff(self):
        with mock.patch.object(accounts, '_group', return_value={}), \
                mock.patch.object(accounts, '_require_dir', return_value='synthetic'), \
                mock.patch.object(accounts.wb2api, 'list_auth_accounts', return_value=[{'uid': '1', 'file': '1'}]), \
                mock.patch.object(accounts.wb2api, 'read_account_file', return_value={'uid': '1'}), \
                mock.patch.object(credits, 'background_state', return_value=('backoff', None)), \
                mock.patch.object(credits, 'get_credits', side_effect=AssertionError('no provider call')):
            result = await accounts.refresh_all_credits(force=False, upstream_id=None, stale_only=True, user={'role': 'admin'})
        self.assertIsNone(result['credits']['1'])

    async def test_same_uid_different_credential_or_realm_never_shares_fresh_cache(self):
        auth = {'uid': 'scoped-user', 'realm': 'cn', 'token': 'synthetic-only'}
        with mock.patch.dict(credits._cache, {'scoped-user': (time.time(), True, 0, '', [])}, clear=True), \
                mock.patch.dict(credits._cache_identity, {'scoped-user': credits._auth_identity(auth)}, clear=True), \
                mock.patch.dict(credits._background_failures, {}, clear=True):
            self.assertEqual(credits.background_state(auth)[0], 'fresh')
            self.assertEqual(credits.background_state(dict(auth, realm='global'))[0], 'stale')
            self.assertEqual(credits.background_state(dict(auth, token='different-synthetic'))[0], 'stale')
            credits._background_failures[credits._auth_identity(auth)] = time.monotonic()
            credits._cache.clear()
            self.assertEqual(credits.background_state(auth)[0], 'backoff')


class ContractAndMetricsTest(unittest.TestCase):
    def test_offline_diagnostics_expose_layers_without_claiming_current_health(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'synthetic.db'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE request_outcomes(ts INTEGER,http_status INTEGER,terminal_state TEXT,error_code TEXT)')
                connection.execute('CREATE TABLE budget_reservations(state TEXT)')
                connection.execute('INSERT INTO request_outcomes VALUES(?,?,?,?)',
                                   (int(time.time()), 400, 'upstream_error', 'channel_check_rejected'))
            connection.close()
            result = diagnostics.inspect(path)
            self.assertEqual(result['historical_diagnoses'][0]['layers']['channel'], 'rejected')
            self.assertIsNone(result['historical_diagnoses'][0]['layers']['ready'])
            self.assertIsNone(result['wallet_frozen'])
            self.assertFalse(result['inference_health_proven'])
            self.assertTrue(result['read_only'])

    def test_cross_dispatch_budget_is_shared_and_missing_or_invalid_receipts_disable_replay(self):
        state = observability.RequestAudit(request_id='synthetic', endpoint='/v1/chat/completions',
                                          method='POST', protocol='openai', started=time.monotonic(),
                                          provider_budget_enabled=True)
        token = observability.current.set(state)
        try:
            self.assertTrue(gateway._consume_attempt_budget(mock.Mock(headers={'x-wb-provider-attempts': '3'})))
            self.assertEqual(state.provider_attempts_remaining, 5)
            self.assertFalse(gateway._consume_attempt_budget(mock.Mock(headers={'x-wb-provider-attempts': '5'})))
            self.assertEqual(state.provider_attempts_remaining, 0)
            for value in ('9', '-1', 'invalid', None):
                state.provider_attempts_remaining = 8
                headers = {} if value is None else {'x-wb-provider-attempts': value}
                self.assertFalse(gateway._consume_attempt_budget(mock.Mock(headers=headers)))
                self.assertEqual(state.provider_attempts_remaining, 0)
        finally:
            observability.current.reset(token)

    def test_capability_removal_is_detected_without_claiming_general_openapi_support(self):
        before = protocolcontracts.contracts()
        after = copy.deepcopy(before)
        self.assertFalse(protocolcontracts.compare(before, after)['breaking'])
        after['protocols']['responses']['capabilities']['stream'] = False
        self.assertTrue(protocolcontracts.compare(before, after)['breaking'])
        self.assertIsNone(before['client_versions']['codex'])
        with self.assertRaises(ValueError):
            protocolcontracts.compare({}, after)

    def test_failure_labels_are_bounded_and_never_include_error_text(self):
        metrics = runtimemetrics.Metrics()
        for index in range(1000):
            metrics.observe(protocol='chat', traffic='inference', http_status=502, terminal='upstream_error',
                            latency_ms=1, error_code='synthetic-private-text-' + str(index))
        metrics.observe(protocol='messages', traffic='inference', http_status=400, terminal='upstream_error',
                        latency_ms=2, error_code='channel_check_rejected')
        rendered = '\n'.join(metrics.render())
        self.assertNotIn('synthetic-private-text', rendered)
        self.assertEqual(len(metrics.failures), 2)
        self.assertIn('error_family="channel_rejected"', rendered)

    def test_liveness_is_not_inference_or_account_failure(self):
        status = diagnostics.classify(reachable=True, ready=True, http_status=400, error_code='channel_check_rejected')
        self.assertEqual(status['channel'], 'rejected')
        self.assertTrue(status['ready'])
        self.assertFalse(status['account_availability_changed'])
        self.assertFalse(status['inference_proven'])
        self.assertEqual(diagnostics.classify(http_status=401)['authentication'], 'rejected')
        self.assertFalse(diagnostics.classify(http_status=200)['inference_proven'])
        self.assertEqual(diagnostics.classify(http_status=400, error_code='11128')['channel'], 'unknown')
        self.assertEqual(runtimemetrics.error_family('11128', 'upstream_error', 400), 'upstream_11128_unclassified')
        self.assertTrue(diagnostics.classify(http_status=200, terminal_state='completed')['inference_proven'])
