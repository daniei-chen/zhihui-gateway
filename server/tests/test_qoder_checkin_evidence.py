"""Offline regressions: a successful check does not prove a reward was issued."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

from server.qtgcore import service
from server.qtgcore import checkin_scheduler
from server.qtgcore.adapters.qoder2 import Qoder2Adapter
from server.qtgcore.checkin_result import classify_result, summarize_result, validate_summary
from server.qtgcore.config import Settings
from server.qtgcore.routers import compat


class QoderCheckinEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dispatch_temp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.dispatch_temp.cleanup()

    async def test_credit_flow_reads_exact_window_and_realm_without_inventing_income(self):
        from server.qtgcore.routers import compat_stats
        today = dt.date.today()
        series = [{'day': (today - dt.timedelta(days=i)).isoformat(), 'credit': 0 if i else 2.5}
                  for i in range(30)]
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        adapter._json = AsyncMock(return_value={'ok': True, 'days': 30, 'realm': 'intl', 'series': series})
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [(b'x-qtg-platform', b'qoder')]})
        rt = SimpleNamespace(qoder=adapter)
        with patch.object(compat_stats, '_rt', return_value=rt), patch.object(compat_stats, '_payload', new=AsyncMock()) as aggregate:
            result = await compat_stats.stats_credits(request, days=30, realm='global')
        aggregate.assert_not_awaited()
        adapter._json.assert_awaited_once_with('GET', '/usage/daily', params={'days': 30, 'realm': 'intl'}, required=True)
        self.assertEqual(len(result['daily']), 30)
        self.assertEqual(result['total'], {'income': None, 'expense': 2.5, 'net': None})
        self.assertEqual(result['income_status'], 'unavailable')
        self.assertEqual(result['realm'], 'global')

    async def test_credit_flow_rejects_failed_wrong_scope_and_invalid_evidence(self):
        from server.qtgcore.errors import UpstreamError
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        valid = {'ok': True, 'days': 7, 'realm': 'cn', 'series': []}
        for payload in ({}, {**valid, 'ok': False}, {**valid, 'realm': 'intl'},
                        {**valid, 'days': 30}, {**valid, 'series': [None]},
                        {**valid, 'series': [{'day': 'bad', 'credit': 0}]},
                        {**valid, 'series': [{'day': '2026-01-01', 'credit': 0}] * 2}):
            with self.subTest(payload=payload):
                adapter._json = AsyncMock(return_value=payload)
                with self.assertRaises(UpstreamError):
                    await adapter.credits_flow_scoped(7, 'cn')
        for bad in (None, True, '12', -2, float('nan'), float('inf')):
            adapter._json = AsyncMock(return_value={**valid, 'days': 1, 'series': [
                {'day': dt.date.today().isoformat(), 'credit': bad}]})
            row = (await adapter.credits_flow_scoped(1, 'cn'))['daily'][0]
            self.assertIsNone(row['expense'])
            self.assertIsNone(row['income'])

    async def test_missing_credit_days_fail_instead_of_filling_manager_calendar_zeroes(self):
        from server.qtgcore.routers import compat_stats
        from server.qtgcore.errors import UpstreamError
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        adapter._json = AsyncMock(return_value={'ok': True, 'days': 7, 'realm': 'cn', 'series': []})
        rt = SimpleNamespace(qoder=adapter)
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [(b'x-qtg-platform', b'qoder')]})
        with patch.object(compat_stats, '_rt', return_value=rt), self.assertRaises(UpstreamError):
            await compat_stats.stats_credits(request, days=7, realm='cn')

    def test_historical_stamp_and_unsupported_are_not_checked_today(self):
        for raw in ({'lastCheckin': '2020-01-01 10:00:00'}, {'canCheckin': False},
                    {'lastCheckin': 'invalid'}, {'lastCheckin': 1234}):
            with self.subTest(raw=raw):
                self.assertFalse(service.map_qoder_account(raw).checked_today)

    def test_explicit_upstream_today_flag_wins(self):
        for flag in (True, False):
            self.assertEqual(service.map_qoder_account({
                'checkedInToday': flag, 'lastCheckin': '2020-01-01',
            }).checked_today, flag)

    def test_legacy_stamp_uses_shanghai_calendar(self):
        now = dt.datetime.now(service.TZ)
        for stamp in (now.isoformat(), now.date().isoformat(),
                      now.astimezone(dt.timezone.utc).isoformat()):
            self.assertTrue(service.map_qoder_account({'lastCheckin': stamp}).checked_today)
        yesterday = (now - dt.timedelta(days=1)).isoformat()
        self.assertFalse(service.map_qoder_account({'lastCheckin': yesterday}).checked_today)

    def test_zero_unknown_and_already_have_distinct_evidence(self):
        cases = [({'ok': True, 'earned_credit': 0}, 'no_reward'),
                 ({'ok': True, 'earned_credit': 100}, 'rewarded'),
                 ({'ok': True, 'earned_credit': 0, 'already': True}, 'already'),
                 ({'ok': True, 'status': 'already'}, 'already'),
                 ({'ok': False, 'earned_credit': 100}, 'unknown'),
                 ({'ok': True, 'already': True, 'earned_credit': 100}, 'unknown'),
                 ({'ok': True, 'earned_credit': 10**400}, 'unknown'),
                 ({'ok': 'true', 'earned_credit': 100}, 'unknown'),
                 ({'ok': True, 'earned_credit': float('nan')}, 'unknown'),
                 ({'ok': True, 'earned_credit': True}, 'unknown'),
                 ({'ok': True, 'earned_credit': 'bad'}, 'unknown'),
                 ({'ok': True, 'status': 'success', 'earned_credit': 'bad'}, 'unknown'),
                 ({'ok': False, 'skipped': True}, 'skipped'),
                 ({'ok': True, 'status': 'pending'}, 'pending')]
        for row, expected in cases:
            with self.subTest(expected=expected, row=row):
                self.assertEqual(classify_result(row), expected)

    def test_scheduler_cannot_invent_success_from_empty_or_partial_response(self):
        for payload in ({}, None, [], {'ok': True}, {'results': []},
                        {'results': [{'ok': True, 'earned_credit': 1}, {'ok': False}]},
                        {'ok': False, 'results': [{'ok': True, 'earned_credit': 1}]},
                        {'results': [{'ok': True}]}, {'results': [None]}):
            self.assertFalse(summarize_result(payload)['ok'])
        summary = summarize_result({'results': [{'ok': True, 'earned_credit': 0,
                                                'uid': 'private', 'message': 'private'}]})
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['no_reward'], 1)
        self.assertNotIn('private', str(summary))

    def test_unknown_batch_never_counts_as_success(self):
        row = compat._batch_results({'results': [{'ok': True, 'message': 'Unconfirmed'}]})[0]
        self.assertFalse(row['ok'])
        self.assertTrue(row['processed'])
        self.assertEqual(row['reward_status'], 'unknown')

    def test_scheduler_preserves_unknown_across_midnight_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'random_checkin.json'
            yesterday = (dt.datetime.now(checkin_scheduler.TZ) - dt.timedelta(days=1)).date().isoformat()
            state = {'day': yesterday, 'targets': {'qoder': 0}, 'done': {},
                     'attempts': {}, 'last': {}, 'uncertain': {'qoder': 'old-attempt'}}
            self.assertTrue(checkin_scheduler._save(state, path))
            self.assertEqual(checkin_scheduler._ensure_state(path)['uncertain'], {'qoder': 'old-attempt'})
            for raw in ('{broken', '[]', '{"day":"2026-01-01"}'):
                path.write_text(raw, encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                    checkin_scheduler._ensure_state(path)

    def test_scheduler_single_writer_lock_and_atomic_save_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with checkin_scheduler._scheduler_owner(root) as first:
                self.assertTrue(first)
                with checkin_scheduler._scheduler_owner(root) as second:
                    self.assertFalse(second)
            with checkin_scheduler._scheduler_owner(root) as third:
                self.assertTrue(third)
            path = root / 'random_checkin.json'
            before = checkin_scheduler._fresh_state()
            self.assertTrue(checkin_scheduler._save(before, path))
            with patch.object(checkin_scheduler.os, 'replace', side_effect=OSError('synthetic')):
                self.assertFalse(checkin_scheduler._save({'day': 'modified'}, path))
            self.assertEqual(checkin_scheduler._load(path), before)
            self.assertEqual(list(root.glob('.checkin-*.tmp')), [])

    async def test_partial_batch_is_fenced_not_replayed_as_a_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'random_checkin.json'
            checkin_scheduler._save({'day': checkin_scheduler._today(), 'targets': {'qoder': 0},
                                     'done': {}, 'attempts': {}, 'last': {}}, path)
            ticks = 0
            async def tick(delay):
                nonlocal ticks
                ticks += 1
                if ticks == 3:
                    raise asyncio.CancelledError()
            dispatch = AsyncMock(return_value={'results': [{'ok': True, 'earned_credit': 100}, {'ok': False}]})
            rt = SimpleNamespace(settings=SimpleNamespace(data_dir=directory))
            with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick), patch.object(checkin_scheduler, '_run_one', dispatch):
                with self.assertRaises(asyncio.CancelledError):
                    await checkin_scheduler._loop(rt)
            self.assertEqual(dispatch.await_count, 1)
            self.assertIn('qoder', checkin_scheduler._load(path)['uncertain'])

    async def test_single_and_batch_zero_reward_are_not_success_or_already(self):
        result = {
            'results': [{'uid': 'one', 'ok': True, 'earned_credit': 0,
                         'message': 'No eligible reward'}],
        }
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=self.dispatch_temp.name),
                             qoder=SimpleNamespace(checkin=AsyncMock(return_value=result),
                                                  checkin_one=AsyncMock(return_value=result)))
        request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []})
        with patch.object(compat, '_rt', return_value=rt), patch.object(compat, 'platform_of', return_value='qoder'):
            one = await compat.compat_account_checkin(request, 'qoder:one')
            self.assertFalse(one['already'])
            self.assertEqual(one['reward_status'], 'no_reward')
            self.assertIn('未新增积分', one['message'])
            batch = await compat.compat_checkin_all(request)
            self.assertEqual((batch['total'], batch['succeeded'], batch['already'], batch['skipped']), (0, 0, 0, 1))
            self.assertEqual(batch['no_reward'], 1)
            rt.qoder.checkin_one.assert_awaited_once_with('one')

    async def test_global_reward_history_maps_to_engine_intl_and_conflicts_stay_unknown(self):
        stamp = time.time()
        rt = SimpleNamespace(qoder=SimpleNamespace(checkin_activity=AsyncMock(return_value={'rows': [
            {'kind': 'checkin', 'ts': stamp, 'uid': 'account-', 'realm': 'intl', 'ok': True, 'amount': 100},
            {'kind': 'checkin', 'ts': stamp - 1, 'uid': 'account-', 'realm': 'intl', 'ok': False, 'amount': 100},
            {'kind': 'checkin', 'ts': stamp - 2, 'uid': 'account-', 'realm': 'cn', 'ok': True, 'amount': 100},
        ]})))
        agg = SimpleNamespace(qoder_accounts=[SimpleNamespace(uid='account-unique')])
        rt.qoder.accounts = AsyncMock(return_value={'accounts': [{'uid': a.uid} for a in agg.qoder_accounts]})
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
        with patch.object(compat, '_rt', return_value=rt), patch.object(compat, 'platform_of', return_value='qoder'), patch.object(compat, 'build_aggregate', new=AsyncMock(return_value=agg)):
            result = await compat.compat_checkin_logs(request, realm='global', limit=20, offset=0, days=7, uid='')
        self.assertEqual([row['reward_status'] for row in result['items']], ['rewarded', 'unknown'])

    async def test_manual_unknown_fences_single_batch_scheduler_and_midnight(self):
        from fastapi import HTTPException
        settings = SimpleNamespace(data_dir=self.dispatch_temp.name)
        adapter = SimpleNamespace(checkin=AsyncMock(return_value={}), checkin_one=AsyncMock(return_value={}))
        rt = SimpleNamespace(settings=settings, qoder=adapter)
        await checkin_scheduler.dispatch_checkin(rt, 'qoder', uid='private-account')
        for kwargs in ({}, {'uid': 'other'}, {'source': 'scheduler'}):
            with self.assertRaises(HTTPException) as caught:
                await checkin_scheduler.dispatch_checkin(rt, 'qoder', **kwargs)
            self.assertEqual(caught.exception.status_code, 409)
        adapter.checkin.assert_not_awaited()
        adapter.checkin_one.assert_awaited_once_with('private-account')
        text = (Path(settings.data_dir) / 'checkin-dispatch-qoder.json').read_text()
        self.assertNotIn('private-account', text)
        self.assertIn('uncertain', text)
        self.assertEqual(checkin_scheduler.dispatch_state(Path(settings.data_dir), 'qoder'), 'uncertain')

    async def test_concurrent_manual_and_scheduler_never_dispatch_twice(self):
        from fastapi import HTTPException
        entered, release = asyncio.Event(), asyncio.Event()
        async def effect():
            entered.set()
            await release.wait()
            return {'results': [{'ok': True, 'earned_credit': 0}]}
        adapter = SimpleNamespace(checkin=AsyncMock(side_effect=effect))
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=self.dispatch_temp.name), qoder=adapter)
        first = asyncio.create_task(checkin_scheduler.dispatch_checkin(rt, 'qoder'))
        await entered.wait()
        try:
            with self.assertRaises(HTTPException):
                await checkin_scheduler.dispatch_checkin(rt, 'qoder', source='scheduler')
        finally:
            release.set()
            await first
        self.assertEqual(adapter.checkin.await_count, 1)

    def test_dispatch_state_read_is_bounded_and_never_creates_or_unlocks(self):
        directory = Path(self.dispatch_temp.name)
        self.assertEqual(checkin_scheduler.dispatch_state(directory, 'qoder'), 'absent')
        self.assertEqual(list(directory.iterdir()), [])
        path = directory / 'checkin-dispatch-qoder.json'
        for raw in ('{broken', '[]', '{"version":1,"platform":"trae","phase":"complete"}'):
            path.write_text(raw)
            self.assertEqual(checkin_scheduler.dispatch_state(directory, 'qoder'), 'unknown')
            self.assertEqual(path.read_text(), raw)

    async def test_cancelled_dispatch_keeps_fence_and_failed_save_never_sends(self):
        from fastapi import HTTPException
        adapter = SimpleNamespace(checkin=AsyncMock(side_effect=asyncio.CancelledError()))
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=self.dispatch_temp.name), qoder=adapter)
        with patch.object(checkin_scheduler, '_save', return_value=False):
            with self.assertRaises(HTTPException) as caught:
                await checkin_scheduler.dispatch_checkin(rt, 'qoder')
            self.assertEqual(caught.exception.status_code, 503)
        adapter.checkin.assert_not_awaited()
        with self.assertRaises(asyncio.CancelledError):
            await checkin_scheduler.dispatch_checkin(rt, 'qoder')
        with self.assertRaises(HTTPException):
            await checkin_scheduler.dispatch_checkin(rt, 'qoder')
        self.assertEqual(adapter.checkin.await_count, 1)

    async def test_single_account_never_dispatches_an_unscoped_batch(self):
        from fastapi import HTTPException
        batch = AsyncMock()
        rt = SimpleNamespace(qoder=SimpleNamespace(checkin=batch), trae=SimpleNamespace(checkin=batch))
        request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []})
        for file in ('qoder:one', 'trae:one'):
            with patch.object(compat, '_rt', return_value=rt):
                with self.assertRaises(HTTPException) as caught:
                    await compat.compat_account_checkin(request, file)
                self.assertEqual(caught.exception.status_code, 409)
        batch.assert_not_awaited()

    async def test_qoder_single_account_passes_uid_before_effectful_dispatch(self):
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        adapter._json = AsyncMock(return_value={'results': []})
        await adapter.checkin_one('selected-account')
        adapter._json.assert_awaited_once_with('POST', '/accounts/checkin',
            json_body={'uid': 'selected-account'}, retry=False)

    async def test_trae_submission_is_pending_and_never_retried(self):
        from server.qtgcore.adapters.trae2 import Trae2Adapter
        adapter = Trae2Adapter.__new__(Trae2Adapter)
        adapter._json = AsyncMock(return_value={'ok': True})
        adapter._today_checkin = lambda: [{'uid': 'old', 'status': 'success'}]
        with patch('server.qtgcore.adapters.trae2.asyncio.sleep', new=AsyncMock()):
            result = await adapter.checkin()
        adapter._json.assert_awaited_once_with('POST', '/api/cmd/checkin_start',
            json_body={'scope': 'all'}, required=True, retry=False)
        self.assertEqual(classify_result(result['results'][0]), 'pending')

    async def test_qoder_checkin_post_is_never_transparently_replayed(self):
        for code in (401, 500, 'timeout'):
            calls = []
            async def handler(request):
                calls.append(request)
                if code == 'timeout':
                    raise httpx.ReadTimeout('synthetic', request=request)
                return httpx.Response(code, json={'error': 'synthetic'})
            adapter = Qoder2Adapter(Settings(upstream_retries=3))
            adapter._client = httpx.AsyncClient(base_url=adapter.base_url,
                                               transport=httpx.MockTransport(handler))
            adapter._authenticated = True
            try:
                with self.assertRaises(Exception):
                    await adapter.checkin()
                self.assertEqual(len(calls), 1)
            finally:
                await adapter.close()

    async def test_scheduler_durably_fences_unknown_before_dispatch(self):
        for response in ({}, {'results': [{'ok': True, 'status': 'pending'}]}, RuntimeError('private')):
            with tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / 'random_checkin.json'
                state = {'day': checkin_scheduler._today(), 'targets': {'qoder': 0},
                         'attempts': {}, 'last': {}, 'done': {}}
                checkin_scheduler._save(state, state_path)
                calls = 0

                async def run_one(rt, name, attempt_id):
                    nonlocal calls
                    calls += 1
                    self.assertIn('qoder', checkin_scheduler._load(state_path)['uncertain'])
                    self.assertEqual(checkin_scheduler._load(state_path)['dispatch_attempts']['qoder'], attempt_id)
                    if isinstance(response, Exception):
                        raise response
                    return response

                ticks = 0
                async def tick(delay):
                    nonlocal ticks
                    ticks += 1
                    if ticks == 3:
                        raise asyncio.CancelledError()

                rt = SimpleNamespace(settings=SimpleNamespace(data_dir=directory))
                with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick), patch.object(checkin_scheduler, '_run_one', side_effect=run_one):
                    with self.assertRaises(asyncio.CancelledError):
                        await checkin_scheduler._loop(rt)
                saved = checkin_scheduler._load(state_path)
                self.assertEqual(calls, 1)
                self.assertNotIn('qoder', saved['done'])
                self.assertIn('qoder', saved['uncertain'])
                self.assertNotIn('private', str(saved))

    def test_persisted_summary_requires_consistent_exact_bounded_types(self):
        summary = summarize_result({'results': [{'ok': True, 'earned_credit': 0}]})
        self.assertEqual(validate_summary(summary), summary)
        for payload in ({}, {'results': []}, {'results': [{'ok': False}]},
                        {'ok': False, 'results': [{'ok': True, 'earned_credit': 3}]},
                        {'results': [{'status': 'pending', 'ok': True}]}):
            value = summarize_result(payload)
            self.assertEqual(validate_summary(value), value)
        bad = [None, [], {}, {**summary, 'unknown': True}, {**summary, 'unknown': -1},
               {**summary, 'accounts': 2}, {**summary, 'partial': True},
               {**summary, 'ok': 1}, {**summary, 'unknown': 1},
               {**summary, 'no_reward': 10001}, {**summary, 'extra': 'private'}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_summary(value)

    def _recovery_fixture(self, *, day=None, summary=None):
        directory = Path(self.dispatch_temp.name)
        reference = 'a' * 32
        now = dt.datetime.now(checkin_scheduler.TZ)
        state = {'day': day or now.date().isoformat(), 'targets': {'qoder': 0},
                 'attempts': {'qoder': 1}, 'last': {'qoder': int(now.timestamp())}, 'done': {},
                 'uncertain': {'qoder': 'pending'}, 'dispatch_attempts': {'qoder': reference}}
        record = {'version': 1, 'platform': 'qoder', 'source': 'scheduler', 'scope': 'all',
                  'phase': 'complete', 'attempt_id': reference,
                  'started_at': (now - dt.timedelta(seconds=2)).isoformat(),
                  'completed_at': (now - dt.timedelta(seconds=1)).isoformat(),
                  'summary': summary or summarize_result({'results': [{'ok': True, 'earned_credit': 0}]})}
        path = directory / 'checkin-dispatch-qoder.json'
        self.assertTrue(checkin_scheduler._save(record, path))
        return directory, path, state, record

    def test_exact_completed_dispatch_recovers_scheduler_without_replaying(self):
        directory, path, state, record = self._recovery_fixture()
        before = path.read_bytes()
        self.assertTrue(checkin_scheduler._recover_completed(directory, state, 'qoder'))
        self.assertEqual(state['uncertain'], {})
        self.assertEqual(state['dispatch_attempts'], {})
        self.assertEqual(state['result_qoder'], record['summary'])
        self.assertEqual(state['done'], {'qoder': record['completed_at']})
        self.assertFalse(checkin_scheduler._recover_completed(directory, state, 'qoder'))
        self.assertEqual(path.read_bytes(), before)

    def test_recovery_never_uses_other_attempt_manual_single_or_pending(self):
        for change in ({'attempt_id': 'b' * 32}, {'source': 'manual'}, {'scope': 'single'},
                       {'phase': 'started'}, {'phase': 'uncertain'}, {'completed_at': None},
                       {'summary': {'ok': True}}, {'version': True},
                       {'started_at': 'bad'}, {'started_at': '2020-01-01T00:00:00'},
                       {'completed_at': '2100-01-01T00:00:00+08:00'},
                       {'completed_at': '2000-01-01T00:00:00+08:00'}):
            with self.subTest(change=change):
                directory, path, state, record = self._recovery_fixture()
                record.update(change)
                self.assertTrue(checkin_scheduler._save(record, path))
                before = json.loads(json.dumps(state))
                raw = path.read_bytes()
                self.assertFalse(checkin_scheduler._recover_completed(directory, state, 'qoder'))
                self.assertEqual(state, before)
                self.assertEqual(path.read_bytes(), raw)

    def test_recovery_requires_released_dispatch_lock(self):
        directory, _, state, _ = self._recovery_fixture()
        with checkin_scheduler._scheduler_owner(directory, '.checkin-dispatch-qoder.lock') as owned:
            self.assertTrue(owned)
            self.assertFalse(checkin_scheduler._recover_completed(directory, state, 'qoder'))
        self.assertTrue(checkin_scheduler._recover_completed(directory, state, 'qoder'))

    def test_recovery_yesterday_does_not_mark_today_done_or_replay_partial_batch(self):
        directory, path, state, record = self._recovery_fixture()
        yesterday = dt.datetime.now(checkin_scheduler.TZ) - dt.timedelta(days=1)
        record.update(started_at=(yesterday - dt.timedelta(seconds=1)).isoformat(),
                      completed_at=yesterday.isoformat())
        self.assertTrue(checkin_scheduler._save(record, path))
        self.assertTrue(checkin_scheduler._recover_completed(directory, state, 'qoder'))
        self.assertEqual(state['done'], {})
        directory, path, state, record = self._recovery_fixture()
        record['summary'] = summarize_result({'results': [{'ok': True, 'earned_credit': 1}, {'ok': False}]})
        self.assertTrue(checkin_scheduler._save(record, path))
        self.assertFalse(checkin_scheduler._recover_completed(directory, state, 'qoder'))

    def test_midnight_keeps_only_uncertain_attempt_references(self):
        directory, _, state, _ = self._recovery_fixture()
        state['day'] = (dt.datetime.now(checkin_scheduler.TZ) - dt.timedelta(days=1)).date().isoformat()
        state['dispatch_attempts']['trae'] = 'c' * 32
        path = directory / 'random_checkin.json'
        self.assertTrue(checkin_scheduler._save(state, path))
        advanced = checkin_scheduler._ensure_state(path)
        self.assertEqual(advanced['dispatch_attempts'], {'qoder': 'a' * 32})
        for reference in (True, None, 'short', 'A' * 32):
            state['dispatch_attempts']['qoder'] = reference
            self.assertTrue(checkin_scheduler._save(state, path))
            with self.assertRaises(RuntimeError):
                checkin_scheduler._load(path)

    async def test_completed_manual_pool_satisfies_scheduler_but_single_does_not(self):
        value = {'results': [{'ok': True, 'earned_credit': 0}]}
        adapter = SimpleNamespace(checkin=AsyncMock(return_value=value), checkin_one=AsyncMock(return_value=value))
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=self.dispatch_temp.name), qoder=adapter)
        await checkin_scheduler.dispatch_checkin(rt, 'qoder', uid='one')
        await checkin_scheduler.dispatch_checkin(rt, 'qoder', source='scheduler')
        self.assertEqual(adapter.checkin.await_count, 1)
        with self.assertRaises(checkin_scheduler.DispatchNotStarted) as caught:
            await checkin_scheduler.dispatch_checkin(rt, 'qoder', source='scheduler')
        self.assertTrue(caught.exception.confirmed['ok'])
        self.assertEqual(adapter.checkin.await_count, 1)

    async def test_scheduler_records_completed_pool_receipt_without_repeating_manual_checkin(self):
        directory = Path(self.dispatch_temp.name)
        value = {'results': [{'ok': True, 'earned_credit': 0}]}
        adapter = SimpleNamespace(checkin=AsyncMock(return_value=value))
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=str(directory)), qoder=adapter)
        await checkin_scheduler.dispatch_checkin(rt, 'qoder')
        state = checkin_scheduler._fresh_state()
        state['targets'] = {'qoder': 0}
        self.assertTrue(checkin_scheduler._save(state, directory / 'random_checkin.json'))
        ticks = 0
        async def tick(delay):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                raise asyncio.CancelledError()
        with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await checkin_scheduler._loop(rt)
        saved = checkin_scheduler._load(directory / 'random_checkin.json')
        self.assertEqual(adapter.checkin.await_count, 1)
        self.assertIn('qoder', saved['done'])
        self.assertNotIn('qoder', saved['uncertain'])
        self.assertTrue(saved['result_qoder']['ok'])

    async def test_scheduler_before_dispatch_failure_does_not_create_its_own_unknown(self):
        directory = Path(self.dispatch_temp.name)
        state = checkin_scheduler._fresh_state()
        state['targets'] = {'qoder': 0}
        self.assertTrue(checkin_scheduler._save(state, directory / 'random_checkin.json'))
        adapter = SimpleNamespace(checkin=AsyncMock())
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=str(directory)), qoder=adapter)
        ticks = 0
        async def tick(delay):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                raise asyncio.CancelledError()
        with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick), \
                patch.object(checkin_scheduler, '_run_one', side_effect=checkin_scheduler.DispatchNotStarted(409, 'busy')):
            with self.assertRaises(asyncio.CancelledError):
                await checkin_scheduler._loop(rt)
        saved = checkin_scheduler._load(directory / 'random_checkin.json')
        self.assertNotIn('qoder', saved.get('uncertain', {}))
        adapter.checkin.assert_not_awaited()

    async def test_scheduler_restart_recovers_saved_response_without_another_provider_call(self):
        directory, _, state, record = self._recovery_fixture()
        self.assertTrue(checkin_scheduler._save(state, directory / 'random_checkin.json'))
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=str(directory)))
        ticks = 0
        async def tick(delay):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                raise asyncio.CancelledError()
        with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick), \
                patch.object(checkin_scheduler, '_run_one', new=AsyncMock()) as effect:
            with self.assertRaises(asyncio.CancelledError):
                await checkin_scheduler._loop(rt)
        effect.assert_not_awaited()
        saved = checkin_scheduler._load(directory / 'random_checkin.json')
        self.assertEqual(saved['done']['qoder'], record['completed_at'])
        self.assertEqual(saved['result_qoder'], record['summary'])
        self.assertEqual(saved['uncertain'], {})

    async def test_recovery_save_failure_preserves_durable_fence_and_does_not_dispatch(self):
        directory, _, state, _ = self._recovery_fixture()
        path = directory / 'random_checkin.json'
        self.assertTrue(checkin_scheduler._save(state, path))
        original = path.read_bytes()
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=str(directory)))
        ticks = 0
        async def tick(delay):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                raise asyncio.CancelledError()
        with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick), \
                patch.object(checkin_scheduler, '_save', return_value=False), \
                patch.object(checkin_scheduler, '_run_one', new=AsyncMock()) as effect:
            with self.assertRaises(asyncio.CancelledError):
                await checkin_scheduler._loop(rt)
        effect.assert_not_awaited()
        self.assertEqual(path.read_bytes(), original)

    async def test_corrupt_complete_summary_cannot_authorize_another_dispatch(self):
        from fastapi import HTTPException
        directory, path, _, record = self._recovery_fixture()
        adapter = SimpleNamespace(checkin=AsyncMock())
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=str(directory)), qoder=adapter)
        for summary in ({}, {'ok': True}, {**record['summary'], 'unknown': 1}):
            record['summary'] = summary
            self.assertTrue(checkin_scheduler._save(record, path))
            self.assertEqual(checkin_scheduler.dispatch_state(directory, 'qoder'), 'unknown')
            with self.assertRaises(HTTPException):
                await checkin_scheduler.dispatch_checkin(rt, 'qoder')
        adapter.checkin.assert_not_awaited()

    async def test_account_error_after_dispatch_is_not_proof_of_a_retryable_non_claim(self):
        from fastapi import HTTPException
        directory = Path(self.dispatch_temp.name)
        adapter = SimpleNamespace(checkin=AsyncMock(return_value={
            'results': [{'ok': False, 'earned_credit': 0, 'message': 'error after sending'}],
        }))
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=str(directory)), qoder=adapter)
        await checkin_scheduler.dispatch_checkin(rt, 'qoder')
        self.assertEqual(checkin_scheduler.dispatch_state(directory, 'qoder'), 'uncertain')
        for options in ({}, {'source': 'scheduler'}):
            with self.assertRaises(HTTPException):
                await checkin_scheduler.dispatch_checkin(rt, 'qoder', **options)
        self.assertEqual(adapter.checkin.await_count, 1)

    async def test_scheduler_never_retries_an_all_failed_account_batch(self):
        directory = Path(self.dispatch_temp.name)
        state = checkin_scheduler._fresh_state()
        state['targets'] = {'qoder': 0}
        path = directory / 'random_checkin.json'
        self.assertTrue(checkin_scheduler._save(state, path))
        dispatch = AsyncMock(return_value={'results': [{'ok': False, 'earned_credit': 0}]})
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=str(directory)))
        ticks = 0
        async def tick(delay):
            nonlocal ticks
            ticks += 1
            if ticks == 3:
                raise asyncio.CancelledError()
        with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick), \
                patch.object(checkin_scheduler, '_run_one', dispatch):
            with self.assertRaises(asyncio.CancelledError):
                await checkin_scheduler._loop(rt)
        self.assertEqual(dispatch.await_count, 1)
        self.assertIn('qoder', checkin_scheduler._load(path)['uncertain'])

    async def test_history_uses_action_amounts_not_a_relabelled_account_stamp(self):
        stamp = time.time()
        account = SimpleNamespace(uid='account-unique', last_checkin='2020-01-01')
        rt = SimpleNamespace(qoder=SimpleNamespace(checkin_activity=AsyncMock(return_value={
            'rows': [{'kind': 'checkin', 'ts': stamp, 'uid': 'account-', 'ok': True, 'amount': 0, 'realm': 'cn'},
                     {'kind': 'checkin', 'ts': stamp - 1, 'uid': 'account-', 'ok': True, 'amount': 100, 'realm': 'cn'},
                     {'kind': 'checkin', 'ts': stamp, 'uid': 'account-', 'ok': True, 'amount': 9, 'realm': 'intl'},
                     {'kind': 'checkin', 'ts': 'bad', 'uid': 'account-', 'ok': True, 'amount': 1},
                     {'kind': 'travel', 'ts': stamp, 'ok': True, 'amount': 999}],
        })))
        agg = SimpleNamespace(qoder_accounts=[account])
        rt.qoder.accounts = AsyncMock(return_value={'accounts': [{'uid': a.uid} for a in agg.qoder_accounts]})
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
        with patch.object(compat, '_rt', return_value=rt), patch.object(compat, 'platform_of', return_value='qoder'), patch.object(compat, 'build_aggregate', new=AsyncMock(return_value=agg)):
            page = await compat.compat_checkin_logs(request, realm='cn', limit=1, offset=0, days=7, uid='account-unique')
            self.assertEqual(page['total'], 2)
            self.assertEqual(page['items'][0]['reward_status'], 'no_reward')
            self.assertIn('新增 0', page['items'][0]['message'])
            page = await compat.compat_checkin_logs(request, realm='cn', limit=1, offset=1, days=7, uid='account-unique')
            self.assertEqual(page['items'][0]['earned_credit'], 100)
            self.assertEqual(page['items'][0]['reward_status'], 'rewarded')

    async def test_history_ids_survive_new_rows_and_ambiguous_account_is_not_guessed(self):
        stamp = time.time()
        rows = [{'kind': 'checkin', 'ts': stamp, 'uid': 'same-pre', 'ok': True, 'amount': 0, 'realm': 'cn'}]
        rt = SimpleNamespace(qoder=SimpleNamespace(checkin_activity=AsyncMock(side_effect=lambda: {'rows': list(rows)})))
        agg = SimpleNamespace(qoder_accounts=[SimpleNamespace(uid='same-prefix-one'), SimpleNamespace(uid='same-prefix-two')])
        rt.qoder.accounts = AsyncMock(return_value={'accounts': [{'uid': a.uid} for a in agg.qoder_accounts]})
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
        with patch.object(compat, '_rt', return_value=rt), patch.object(compat, 'platform_of', return_value='qoder'), patch.object(compat, 'build_aggregate', new=AsyncMock(return_value=agg)):
            first = await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='')
            rows.insert(0, {'kind': 'checkin', 'ts': stamp + 1, 'uid': 'other', 'ok': True, 'amount': 1, 'realm': 'cn'})
            second = await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='')
            self.assertEqual(first['items'][0]['id'], second['items'][1]['id'])
            selected = await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='same-prefix-one')
            self.assertEqual(selected['items'], [])

    async def test_history_exposes_only_safe_scheduler_state_and_failed_evidence_is_not_empty(self):
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            rt = SimpleNamespace(settings=SimpleNamespace(data_dir=directory), qoder=SimpleNamespace(checkin_activity=AsyncMock(return_value={'rows': []})))
            agg = SimpleNamespace(qoder_accounts=[])
            rt.qoder.accounts = AsyncMock(return_value={'accounts': []})
            request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
            state = checkin_scheduler._fresh_state()
            state['uncertain'] = {'qoder': 'safe-marker'}
            state['result_qoder'] = {'private': 'must-not-leak'}
            checkin_scheduler._save(state, Path(directory) / 'random_checkin.json')
            with patch.object(compat, '_rt', return_value=rt), patch.object(compat, 'platform_of', return_value='qoder'), patch.object(compat, 'build_aggregate', new=AsyncMock(return_value=agg)):
                result = await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='')
                self.assertEqual(result['scheduler'], {'state': 'uncertain', 'automation_owner': 'manager'})
                self.assertNotIn('must-not-leak', str(result))
                rt.qoder.checkin_activity.return_value = {'ok': False, 'rows': []}
                with self.assertRaises(HTTPException) as caught:
                    await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='')
                self.assertEqual(caught.exception.status_code, 502)

    async def test_qoder_automatic_owner_requires_strict_engine_evidence(self):
        from server.qtgcore.errors import UpstreamError
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        for enabled, expected in ((True, 'engine'), (False, 'unverified')):
            adapter._json = AsyncMock(return_value={'enabled': enabled, 'logs': ['private']})
            self.assertEqual(await adapter.automatic_checkin_owner(), expected)
            adapter._json.assert_awaited_once_with('GET', '/scheduler', required=True)
        for payload in ({}, None, [], {'enabled': 0}, {'enabled': 'false'}, {'enabled': None},
                        {'enabled': True, 'ok': False}):
            adapter._json = AsyncMock(return_value=payload)
            with self.subTest(payload=payload), self.assertRaises(UpstreamError):
                await adapter.automatic_checkin_owner()
        adapter._json = AsyncMock(return_value={'enabled': True})
        for code in (401, 500, 'timeout'):
            async def handler(request):
                self.assertEqual((request.method, request.url.path), ('GET', '/scheduler'))
                if code == 'timeout':
                    raise httpx.ReadTimeout('synthetic', request=request)
                return httpx.Response(code, json={'enabled': False})
            live = Qoder2Adapter(Settings(upstream_retries=0))
            live._authenticated = True
            live._client = httpx.AsyncClient(base_url=live.base_url, transport=httpx.MockTransport(handler))
            try:
                rt = SimpleNamespace(qoder=live)
                self.assertEqual(await checkin_scheduler.automatic_owner(rt, 'qoder'), 'unverified')
            finally:
                await live.close()

    async def test_engine_or_unverified_owner_never_dispatches_or_marks_reward_complete(self):
        for observation in ('engine', 'unverified', RuntimeError('private'), 'unexpected', None):
            with self.subTest(observation=type(observation).__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'random_checkin.json'
                state = checkin_scheduler._fresh_state()
                state['targets'] = {'qoder': 0}
                self.assertTrue(checkin_scheduler._save(state, path))
                before = path.read_bytes()
                observer = AsyncMock(side_effect=observation) if isinstance(observation, Exception) else AsyncMock(return_value=observation)
                adapter = SimpleNamespace(automatic_checkin_owner=observer, checkin=AsyncMock())
                rt = SimpleNamespace(settings=SimpleNamespace(data_dir=directory), qoder=adapter)
                ticks = 0
                async def tick(delay):
                    nonlocal ticks
                    ticks += 1
                    if ticks == 3:
                        raise asyncio.CancelledError()
                with patch.object(checkin_scheduler.asyncio, 'sleep', side_effect=tick), \
                        patch.object(checkin_scheduler, '_run_one', new=AsyncMock()) as effect:
                    with self.assertRaises(asyncio.CancelledError):
                        await checkin_scheduler._loop(rt)
                effect.assert_not_awaited()
                adapter.checkin.assert_not_awaited()
                self.assertEqual(observer.await_count, 2)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((Path(directory) / 'checkin-dispatch-qoder.json').exists())

    async def test_direct_automatic_dispatch_cannot_bypass_engine_owner_check(self):
        for owner in ('engine', 'unverified'):
            adapter = SimpleNamespace(automatic_checkin_owner=AsyncMock(return_value=owner), checkin=AsyncMock())
            rt = SimpleNamespace(settings=SimpleNamespace(data_dir=self.dispatch_temp.name), qoder=adapter)
            with self.assertRaises(checkin_scheduler.DispatchNotStarted) as caught:
                await checkin_scheduler.dispatch_checkin(rt, 'qoder', source='scheduler')
            self.assertIsNone(caught.exception.confirmed)
            adapter.checkin.assert_not_awaited()
            self.assertFalse((Path(self.dispatch_temp.name) / 'checkin-dispatch-qoder.json').exists())

    async def test_qoder_history_reports_owner_without_exposing_engine_logs(self):
        rt = SimpleNamespace(settings=SimpleNamespace(data_dir=self.dispatch_temp.name), qoder=SimpleNamespace(
            checkin_activity=AsyncMock(return_value={'rows': []}), accounts=AsyncMock(return_value={'accounts': []}),
            automatic_checkin_owner=AsyncMock(return_value='engine')))
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
        with patch.object(compat, '_rt', return_value=rt), patch.object(compat, 'platform_of', return_value='qoder'):
            value = await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='')
        self.assertEqual(value['scheduler'], {'state': 'unknown', 'automation_owner': 'engine'})
        self.assertNotIn('done', value)

    async def test_qoder_history_has_no_cross_platform_or_statistics_dependency(self):
        rt = SimpleNamespace(qoder=SimpleNamespace(
            checkin_activity=AsyncMock(return_value={'rows': [
                {'kind': 'checkin', 'ts': time.time(), 'uid': 'unique-a', 'realm': 'cn', 'ok': True, 'amount': 0},
            ]}), accounts=AsyncMock(return_value={'accounts': [{'uid': 'unique-account'}]})))
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
        with patch.object(compat, '_rt', return_value=rt), patch.object(compat, 'platform_of', return_value='qoder'), \
                patch.object(compat, 'build_aggregate', new=AsyncMock(side_effect=RuntimeError('other platform down'))) as aggregate:
            result = await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='unique-account')
            self.assertEqual(result['items'][0]['uid'], 'unique-account')
            aggregate.assert_not_awaited()
            for payload in ({}, {'accounts': [] , 'ok': False}, {'accounts': [None]},
                            {'accounts': [{'uid': 123}]}, {'accounts': [{'uid': ''}]}):
                rt.qoder.accounts.return_value = payload
                from fastapi import HTTPException
                with self.assertRaises(HTTPException) as caught:
                    await compat.compat_checkin_logs(request, realm='cn', limit=20, offset=0, days=7, uid='')
                self.assertEqual(caught.exception.status_code, 502)


if __name__ == '__main__':
    unittest.main()
