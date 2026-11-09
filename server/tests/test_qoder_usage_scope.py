"""Offline scope contracts; never issue provider requests or load credentials."""
import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from starlette.requests import Request

from server.qtgcore.adapters.qoder2 import Qoder2Adapter
from server.qtgcore.errors import UpstreamError
from server.qtgcore.config import Settings, load_settings
from server.qtgcore.qoder_usage import credits_evidence, daily_evidence, hourly_evidence, hourly_window, summary_evidence
from server.qtgcore.routers import compat_stats


def daily(days=7, realm='intl'):
    today = dt.datetime.now(dt.timezone.utc).date()
    rows = [{'day': (today - dt.timedelta(days=days - 1 - i)).isoformat(),
             'requests': 2, 'errors': 1, 'prompt_tokens': 30, 'completion_tokens': 50,
             'total_tokens': 190, 'credit': 2.5} for i in range(days)]
    return {'ok': True, 'realm': realm, 'days': days, 'group_by': 'model', 'series': rows,
            'groups': [{'id': 'selected-model', 'requests': days * 2, 'errors': days,
                        'total_tokens': days * 190, 'credit': days * 2.5}]}


def snapshot(realm='intl'):
    return {'realm': realm, 'requests': 200, 'total_tokens': 123456, 'credit': 80,
            'by_model': {'selected-model': {'requests': 200}}}


def request():
    return Request({'type': 'http', 'method': 'GET', 'path': '/',
                    'headers': [(b'x-qtg-platform', b'qoder')]})


def calendar(timezone='Asia/Shanghai', realm='intl'):
    now = dt.datetime.now(ZoneInfo(timezone))
    value = daily(1, realm)
    value['series'][0].update(day=now.date().isoformat(),
        at=dt.datetime.combine(now.date(), dt.time(), now.tzinfo).timestamp())
    return value


def timeseries(window, realm='intl', offsets=(60, 120, 3 * 3600)):
    return {'ok': True, 'realm': realm, 'bucket_seconds': 60, 'since': window['since'],
            'until': window['until'], 'series': [
                {'at': window['since'] + offset, 'requests': 2, 'errors': 1,
                 'prompt_tokens': 30, 'completion_tokens': 50, 'total_tokens': 190, 'credit': 2.5}
                for offset in offsets],
            'credits': [{'account': 'must-not-leak', 'model': 'must-not-leak'}]}


class QoderCreditCalendarTests(unittest.IsolatedAsyncioTestCase):
    def payload(self, days=7, shift=0, realm='intl'):
        value = daily(days, realm)
        for index, row in enumerate(value['series']):
            row['day'] = (dt.date.fromisoformat(row['day']) + dt.timedelta(days=shift)).isoformat()
            row['credit'] = index + 1
        return value

    async def test_route_keeps_engine_today_and_week_across_both_midnight_offsets(self):
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        for shift in (-1, 1):
            for realm, engine in (('cn', 'cn'), ('global', 'intl'), ('', 'all')):
                payload = self.payload(30, shift, engine)
                adapter._json = AsyncMock(return_value=payload)
                with self.subTest(shift=shift, realm=realm), \
                        patch.object(compat_stats, '_rt', return_value=SimpleNamespace(qoder=adapter)), \
                        patch.object(compat_stats, '_day_list', side_effect=AssertionError('Manager calendar forbidden')), \
                        patch.object(compat_stats, '_payload', new=AsyncMock(side_effect=AssertionError('aggregate forbidden'))):
                    result = await compat_stats.stats_credits(request(), 30, realm)
                self.assertEqual(result['to'], payload['series'][-1]['day'])
                self.assertEqual(result['from'], payload['series'][0]['day'])
                self.assertEqual(result['calendar_day'], result['to'])
                self.assertEqual(result['today'], {'income': None, 'expense': 30, 'net': None})
                self.assertEqual(result['week']['expense'], sum(range(24, 31)))
                self.assertEqual(result['total']['expense'], sum(range(1, 31)))
                self.assertTrue(all(row['income'] is None and row['net'] is None for row in result['daily']))
                adapter._json.assert_awaited_once_with('GET', '/usage/daily',
                    params={'days': 30, 'realm': engine}, required=True)

    def test_out_of_order_rows_sort_without_mutating_or_leaking_engine_details(self):
        import copy
        value = self.payload()
        value['series'].reverse()
        value['series'][0]['account'] = 'must-not-leak'
        value['private'] = 'must-not-leak'
        before = copy.deepcopy(value)
        result = credits_evidence(value, 7, 'global')
        self.assertEqual(result['daily'][-1]['expense'], 7)
        self.assertEqual(result['total']['expense'], 28)
        self.assertNotIn('must-not-leak', str(result))
        result['daily'][0]['expense'] = 99
        self.assertEqual(value, before)

    def test_invalid_amounts_remain_unknown_without_poisoning_independent_periods(self):
        for bad in (None, True, '1.5', -1, float('nan'), float('inf'), 10**400, 2**53):
            value = self.payload(30)
            value['series'][0]['credit'] = bad
            with self.subTest(bad=type(bad).__name__):
                result = credits_evidence(value, 30, 'global')
                self.assertIsNone(result['daily'][0]['expense'])
                self.assertIsNone(result['total']['expense'])
                self.assertEqual(result['week']['expense'], 189)
                self.assertEqual(result['today']['expense'], 30)
        value['series'][-1]['credit'] = None
        result = credits_evidence(value, 30, 'global')
        self.assertIsNone(result['today']['expense'])
        self.assertIsNone(result['week']['expense'])

    def test_complete_zero_is_known_zero_and_aggregate_overflow_is_unknown(self):
        value = self.payload()
        for row in value['series']:
            row['credit'] = 0
        result = credits_evidence(value, 7, 'global')
        self.assertEqual(result['total'], {'income': None, 'expense': 0, 'net': None})
        for row in value['series']:
            row['credit'] = 2**53 - 1
        result = credits_evidence(value, 7, 'global')
        self.assertIsNone(result['total']['expense'])
        self.assertEqual(result['today']['expense'], 2**53 - 1)

    def test_incomplete_stale_gapped_ambiguous_calendars_fail_closed(self):
        bad = []
        for change in ({'ok': False}, {'days': True}, {'days': 30}, {'realm': 'cn'},
                       {'series': []}, {'series': [None] * 7}):
            bad.append({**self.payload(), **change})
        value = self.payload()
        value['series'][0]['day'] = value['series'][1]['day']
        bad.append(value)
        value = self.payload()
        value['series'][0]['day'] = (dt.date.fromisoformat(value['series'][0]['day']) - dt.timedelta(days=1)).isoformat()
        bad.append(value)
        bad.extend([self.payload(7, -2), self.payload(7, 2)])
        for day in ('2026-1-1', 'bad', True, '20260101'):
            value = self.payload()
            value['series'][0]['day'] = day
            bad.append(value)
        for index, value in enumerate(bad):
            with self.subTest(case=index), self.assertRaises(UpstreamError):
                credits_evidence(value, 7, 'global')


class QoderDailyEvidenceTests(unittest.TestCase):
    def test_total_only_model_evidence_never_fabricates_split_cache_or_speed(self):
        value = daily_evidence(daily(), 7, 'global')
        model = value['by_model'][0]
        self.assertEqual(model['total_tokens'], 1330)
        for field in ('prompt_tokens', 'completion_tokens', 'cache_hit_rate', 'tokens_per_sec'):
            self.assertIsNone(model[field])
        self.assertEqual(value['daily'][0]['prompt_tokens'], 30)
        self.assertEqual(value['daily'][0]['completion_tokens'], 50)
        self.assertEqual(value['daily'][0]['total_tokens'], 190)
        self.assertEqual(value['daily'][0]['failed'], 1)

    def test_period_and_realm_evidence_must_match_exactly(self):
        for changes in ({'ok': False}, {'days': True}, {'days': 30}, {'realm': 'cn'},
                        {'realm': 'global'}, {'group_by': 'key'}, {'series': []}, {'groups': None}):
            with self.subTest(changes=changes), self.assertRaises(UpstreamError):
                daily_evidence({**daily(), **changes}, 7, 'global')
        self.assertEqual(daily_evidence(daily(1, 'all'), 1, '')['realm'], '')
        self.assertEqual(daily_evidence(daily(30, 'cn'), 30, 'cn')['days'], 30)

    def test_malformed_rows_inconsistent_groups_and_unsafe_counts_fail_closed(self):
        mutations = [lambda p: p['series'][0].update(day='2020-01-01'),
                     lambda p: p['series'][1].update(day=p['series'][0]['day']),
                     lambda p: p['groups'].append(p['groups'][0]),
                     lambda p: p['groups'][0].update(total_tokens=1),
                     lambda p: p['groups'][0].update(credit=1),
                     lambda p: p['groups'][0].update(requests=1),
                     lambda p: p['groups'][0].update(errors=0),
                     lambda p: p['groups'][0].update(id=''),
                     lambda p: p['groups'].clear()]
        for mutate in mutations:
            value = daily()
            mutate(value)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(UpstreamError):
                daily_evidence(value, 7, 'global')
        for bad in (True, None, '12', -1, 2**54, float('nan'), float('inf'), 10**500):
            for location, field in (('series', 'total_tokens'), ('groups', 'requests'), ('series', 'credit')):
                value = daily()
                value[location][0][field] = bad
                with self.subTest(bad=type(bad).__name__, field=field), self.assertRaises(UpstreamError):
                    daily_evidence(value, 7, 'global')

    def test_empty_window_is_known_zero_only_when_complete_engine_buckets_exist(self):
        value = daily(1)
        value['groups'] = []
        value['series'][0].update(requests=0, errors=0, prompt_tokens=0, completion_tokens=0,
                                  total_tokens=0, credit=0)
        result = daily_evidence(value, 1, 'global')
        self.assertEqual(result['by_model'], [])
        self.assertEqual(result['daily'][0]['total_tokens'], 0)

    def test_summary_lifetime_is_not_the_seven_day_total(self):
        result = summary_evidence(daily_evidence(daily(), 7, 'global'), snapshot(), 'global')
        self.assertEqual((result['today_requests'], result['week_requests'], result['total_requests']), (2, 14, 200))
        self.assertEqual((result['today_tokens'], result['week_tokens'], result['total_tokens']), (190, 1330, 123456))
        self.assertEqual((result['today_credit'], result['week_credit'], result['total_credit']), (2.5, 17.5, 80))
        self.assertEqual(result['top_model'], 'selected-model')
        for changed in ({'realm': 'cn'}, {'credit': True}, {'ok': False}, {'by_model': {}},
                        {'total_tokens': '123'}, {'by_model': {'other': {'requests': 3}}}):
            with self.subTest(changed=changed), self.assertRaises(UpstreamError):
                summary_evidence(daily_evidence(daily(), 7, 'global'), {**snapshot(), **changed}, 'global')

    def test_engine_day_labels_survive_manager_calendar_difference(self):
        value = daily(1)
        value['series'][0]['day'] = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)).isoformat()
        result = daily_evidence(value, 1, 'global')
        self.assertEqual(result['daily'][0]['day'], value['series'][0]['day'])
        value['series'][0]['day'] = '2000-01-01'
        with self.assertRaises(UpstreamError):
            daily_evidence(value, 1, 'global')

    def test_daily_parser_does_not_mutate_shared_cached_engine_payload(self):
        value = daily()
        import json
        before = json.dumps(value, sort_keys=True)
        parsed = daily_evidence(value, 7, 'global')
        parsed['daily'][0]['credit'] = 999
        parsed['by_model'][0]['name'] = 'changed'
        self.assertEqual(json.dumps(value, sort_keys=True), before)

    def test_calendar_extremes_and_total_overflow_are_controlled_upstream_errors(self):
        value = daily(2)
        value['series'][0]['day'] = '9999-12-31'
        value['series'][1]['day'] = '0001-01-01'
        with self.assertRaises(UpstreamError):
            daily_evidence(value, 2, 'global')
        value = daily(2)
        for row in value['series']:
            row['credit'] = 2**53 - 1
        value['groups'][0]['credit'] = 2**53 - 1
        with self.assertRaises(UpstreamError):
            daily_evidence(value, 2, 'global')


class QoderScopeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_and_daily_routes_use_requested_scope_without_aggregate(self):
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        async def read(method, path, *, params, required):
            self.assertEqual((method, path, required), ('GET', '/usage/daily', True))
            self.assertEqual(params['group'], 'model')
            return daily(params['days'], params['realm'])
        adapter._json = AsyncMock(side_effect=read)
        rt = SimpleNamespace(qoder=adapter)
        with patch.object(compat_stats, '_rt', return_value=rt), \
                patch.object(compat_stats, '_payload', new=AsyncMock(side_effect=AssertionError('all-platform fallback'))), \
                patch.object(compat_stats, '_realm_available', new=AsyncMock(side_effect=AssertionError('account gate'))):
            for days in (1, 7, 30, 90):
                for realm, engine in (('cn', 'cn'), ('global', 'intl'), ('', 'all')):
                    rows = await compat_stats.stats_daily(request(), days, realm)
                    model = await compat_stats.stats_by_model(request(), days, realm)
                    self.assertEqual(len(rows), days)
                    self.assertEqual(model[0]['total_tokens'], days * 190)
                    self.assertIsNone(model[0]['completion_tokens'])
                    adapter._json.assert_awaited_with('GET', '/usage/daily',
                        params={'days': days, 'realm': engine, 'group': 'model'}, required=True)

    async def test_scoped_summary_uses_two_readonly_endpoints_and_enabled_local_keys(self):
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        async def read(method, path, *, params, required):
            self.assertEqual((method, required), ('GET', True))
            self.assertEqual(params['realm'], 'intl')
            return daily(7) if path == '/usage/daily' else snapshot()
        adapter._json = AsyncMock(side_effect=read)
        rt = SimpleNamespace(qoder=adapter, store=SimpleNamespace(list_keys=lambda: [
            {'enabled': True, 'platforms': ['qoder']}, {'enabled': False, 'platforms': ['qoder']},
            {'enabled': True, 'platforms': ['trae']}]))
        with patch.object(compat_stats, '_rt', return_value=rt), \
                patch.object(compat_stats, '_payload', new=AsyncMock(side_effect=AssertionError('fallback'))):
            value = await compat_stats.stats_summary(request(), 'global')
        self.assertEqual(value['active_keys'], 1)
        self.assertEqual(value['total_requests'], 200)
        self.assertEqual(adapter._json.await_count, 2)

    async def test_invalid_scope_or_upstream_failure_never_falls_back_to_another_realm(self):
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        adapter._json = AsyncMock(side_effect=UpstreamError('synthetic'))
        rt = SimpleNamespace(qoder=adapter)
        with patch.object(compat_stats, '_rt', return_value=rt), \
                patch.object(compat_stats, '_payload', new=AsyncMock(side_effect=AssertionError('fallback'))):
            for route, args in ((compat_stats.stats_daily, (7,)), (compat_stats.stats_by_model, (7,)),
                                (compat_stats.stats_summary, ())):
                with self.subTest(route=route.__name__), self.assertRaises(HTTPException):
                    await route(request(), *args, realm='intl')
                with self.subTest(route=route.__name__), self.assertRaises(UpstreamError):
                    await route(request(), *args, realm='global')


class QoderHourlyEvidenceTests(unittest.TestCase):
    def test_sparse_minutes_become_24_engine_hours_with_explicit_totals(self):
        window = hourly_window('', 'Asia/Shanghai', calendar(), 'global')
        payload = timeseries(window)
        import copy
        original = copy.deepcopy(payload)
        rows = hourly_evidence(payload, window, 'global')
        self.assertEqual(len(rows), 24)
        self.assertEqual([row['hour'] for row in rows], list(range(24)))
        self.assertEqual((rows[0]['requests'], rows[0]['failed'], rows[0]['total_tokens']), (4, 2, 380))
        self.assertEqual((rows[0]['prompt_tokens'], rows[0]['completion_tokens']), (60, 100))
        self.assertEqual(rows[1]['total_tokens'], 0)
        self.assertEqual(sum(row['credit'] for row in rows), 7.5)
        self.assertTrue(all(row['timezone'] == 'Asia/Shanghai' for row in rows))
        self.assertNotIn('must-not-leak', str(rows))
        self.assertEqual(payload, original)
        empty = hourly_evidence(timeseries(window, offsets=()), window, 'global')
        self.assertTrue(all(row['requests'] == row['total_tokens'] == 0 for row in empty))

    def test_wrong_scope_window_duplicates_and_invalid_buckets_are_not_zero(self):
        window = hourly_window('', 'Asia/Shanghai', calendar(), 'global')
        for change in ({'ok': False}, {'realm': 'cn'}, {'bucket_seconds': 3600},
                       {'bucket_seconds': True}, {'since': window['since'] + 1},
                       {'until': window['end']}, {'series': None}):
            with self.subTest(change=change), self.assertRaises(UpstreamError):
                hourly_evidence({**timeseries(window), **change}, window, 'global')
        for offset in (-60, 1, 86400):
            with self.subTest(offset=offset), self.assertRaises(UpstreamError):
                hourly_evidence(timeseries(window, offsets=(offset,)), window, 'global')
        duplicate = timeseries(window, offsets=(60, 60))
        with self.assertRaises(UpstreamError):
            hourly_evidence(duplicate, window, 'global')
        for field in ('requests', 'errors', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'credit', 'at'):
            for bad in (None, True, '1', -1, float('nan'), float('inf'), 10**500):
                value = timeseries(window)
                value['series'][0][field] = bad
                with self.subTest(field=field, bad=type(bad).__name__), self.assertRaises(UpstreamError):
                    hourly_evidence(value, window, 'global')

    def test_engine_calendar_disagreement_and_invalid_timezone_fail_closed(self):
        for zone in ('../UTC', 'Unknown/Timezone', ''):
            with self.subTest(zone=zone), self.assertRaises(UpstreamError):
                hourly_window('', zone, calendar(), 'global')
        for field, value in (('at', 0), ('day', '2000-01-01')):
            payload = calendar()
            payload['series'][0][field] = value
            with self.subTest(field=field), self.assertRaises(UpstreamError):
                hourly_window('', 'Asia/Shanghai', payload, 'global')
        with self.assertRaises(UpstreamError):
            hourly_window('', 'UTC', calendar('Asia/Shanghai'), 'global')
        for day in ('2026-2-3', '9999-12-31', '0001-01-01', 'not-a-date'):
            with self.subTest(day=day), self.assertRaises(ValueError):
                hourly_window(day, 'Asia/Shanghai', calendar(), 'global')

    def test_next_midnight_is_excluded_and_historical_current_hour_is_absent(self):
        window = hourly_window('2026-01-01', 'Asia/Shanghai', calendar(), 'global')
        self.assertLess(window['until'], window['end'])
        self.assertIsNone(window['current_hour'])
        rows = hourly_evidence(timeseries(window, offsets=(0, 86340)), window, 'global')
        self.assertEqual(rows[0]['requests'], 2)
        self.assertEqual(rows[23]['requests'], 2)
        self.assertTrue(all(row['current_hour'] is None for row in rows))

    def test_dst_repeated_hour_is_merged_and_missing_hour_not_relabelled(self):
        for day, hours in (('2026-03-08', 23), ('2026-11-01', 25)):
            window = hourly_window(day, 'America/New_York', calendar('America/New_York'), 'global')
            self.assertEqual(window['end'] - window['since'], hours * 3600)
            rows = hourly_evidence(timeseries(window, offsets=tuple(range(0, hours * 3600, 60))), window, 'global')
            self.assertEqual(sum(row['requests'] for row in rows), hours * 60 * 2)
            self.assertEqual(rows[2 if hours == 23 else 1]['requests'], 0 if hours == 23 else 240)
        for day, hours in (('2026-04-05', 24.5), ('2026-10-04', 23.5)):
            window = hourly_window(day, 'Australia/Lord_Howe', calendar('Australia/Lord_Howe'), 'global')
            self.assertEqual(window['end'] - window['since'], hours * 3600)
            rows = hourly_evidence(timeseries(window, offsets=tuple(range(0, int(hours * 3600), 60))), window, 'global')
            self.assertEqual(sum(row['requests'] for row in rows), int(hours * 60 * 2))

    def test_aggregate_overflow_is_rejected(self):
        window = hourly_window('', 'Asia/Shanghai', calendar(), 'global')
        for field in ('requests', 'total_tokens', 'credit'):
            value = timeseries(window)
            for row in value['series']:
                row[field] = 2**53 - 1
            with self.subTest(field=field), self.assertRaises(UpstreamError):
                hourly_evidence(value, window, 'global')

    def test_timezone_is_explicit_configuration_not_browser_or_manager_calendar(self):
        import os
        import tempfile
        from server.qtgcore.configcontract import ConfigurationError, load_schema
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: directory for name, spec in load_schema()['settings'].items()
                     if spec['type'] == 'absolute_path'}
            with patch.dict(os.environ, {**paths, 'QTG_QODER2_TIMEZONE': 'America/New_York'}):
                self.assertEqual(load_settings().qoder2_timezone, 'America/New_York')
            with patch.dict(os.environ, {**paths, 'QTG_QODER2_TIMEZONE': 'Unknown/Timezone'}), self.assertRaises(ConfigurationError):
                load_settings()
        self.assertEqual(Settings().qoder2_timezone, 'Asia/Shanghai')


class QoderHourlyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_uses_only_two_scoped_engine_gets_and_no_local_fallback(self):
        adapter = Qoder2Adapter.__new__(Qoder2Adapter)
        adapter.cfg = Settings()
        async def read(method, path, *, params, required):
            self.assertEqual((method, required), ('GET', True))
            if path == '/usage/daily':
                self.assertEqual(params, {'days': 1, 'realm': 'intl', 'group': 'model'})
                return calendar()
            self.assertEqual(path, '/usage/timeseries')
            self.assertEqual((params['realm'], params['range'], params['bucket']), ('intl', 'custom', 60))
            window = hourly_window('', 'Asia/Shanghai', calendar(), 'global')
            return timeseries(window)
        adapter._json = AsyncMock(side_effect=read)
        rt = SimpleNamespace(qoder=adapter, store=SimpleNamespace(hourly=lambda *_: self.fail('local fallback')))
        with patch.object(compat_stats, '_rt', return_value=rt), \
                patch.object(compat_stats, '_realm_available', new=AsyncMock(side_effect=AssertionError('account gate'))):
            rows = await compat_stats.stats_hourly(request(), '', 'global')
        self.assertEqual(sum(row['total_tokens'] for row in rows), 570)
        self.assertEqual(adapter._json.await_count, 2)

    async def test_invalid_input_and_engine_failure_do_not_fall_back(self):
        scoped = AsyncMock(side_effect=UpstreamError('synthetic'))
        rt = SimpleNamespace(qoder=SimpleNamespace(usage_hourly_scoped=scoped))
        with patch.object(compat_stats, '_rt', return_value=rt):
            for day, realm in (('', 'intl'), ('bad', 'cn'), ('2026-2-3', 'cn'), ('9999-12-31', '')):
                with self.subTest(day=day, realm=realm), self.assertRaises(HTTPException) as caught:
                    await compat_stats.stats_hourly(request(), day, realm)
                self.assertEqual(caught.exception.status_code, 422)
            scoped.assert_not_awaited()
            with self.assertRaises(UpstreamError):
                await compat_stats.stats_hourly(request(), '', 'cn')


if __name__ == '__main__':
    unittest.main()
