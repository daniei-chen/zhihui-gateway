"""Product usage metrics: real cache counts, stable key identity, no ledger writes."""
from __future__ import annotations

import tempfile
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from server import cachemetrics, config, db
from server.routers import stats
from server.qtgcore.adapters.workbuddy import WorkBuddyAdapter
from server.qtgcore.routers import compat_stats


class ProductStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = config.DB_PATH, db._conn
        config.DB_PATH = Path(self.temp.name) / 'stats.db'
        db._conn = None
        db.connect()
        self.key(32, '32')
        self.key(40, 'same-name')
        self.key(50, 'same-name', enabled=0)

    def tearDown(self):
        if db._conn:
            db._conn.close()
        config.DB_PATH, db._conn = self.original
        self.temp.cleanup()

    def key(self, key_id, name, enabled=1):
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at,enabled) '
                   'VALUES(?,?,?,?,?,?)', (key_id, name, 'synthetic', 'fixture', time.time(), enabled))

    def usage(self, key_id, hit=80, miss=20, model='cn:model', realm='cn', ts=None):
        db.bump_usage(key_id, model, 100, 50, 0.5, realm=realm, cache=(hit, miss), ts=ts)

    def log(self, key_id, *, output=50, latency=3000, first=1000, status=200,
            terminal='completed', realm='cn', ts=None):
        db.execute('INSERT INTO request_logs(ts,key_id,model,status,completion_tokens,'
                   'latency_ms,first_token_ms,terminal_state,realm) VALUES(?,?,?,?,?,?,?,?,?)',
                   (time.time() if ts is None else ts, key_id, 'cn:model', status, output,
                    latency, first, terminal, realm))

    def rows(self, realm='cn'):
        return stats.by_key(1, realm, {'role': 'admin'})

    def test_deleted_keys_hidden_without_changing_any_usage(self):
        self.usage(999)
        self.usage(32)
        before = [dict(r) for r in db.query('SELECT * FROM usage_daily ORDER BY key_id')]
        self.assertEqual([r['key_id'] for r in self.rows()], [32])
        self.assertEqual(self.rows()[0]['name'], '32', 'A numeric live name is legitimate')
        self.assertEqual(before, [dict(r) for r in db.query('SELECT * FROM usage_daily ORDER BY key_id')])
        self.assertEqual(stats.by_model(1, 'cn', {})[0]['requests'], 2)

    def test_duplicate_names_keep_distinct_ids_and_disabled_keys(self):
        for key in (40, 50):
            self.usage(key)
            self.log(key, output=key)
        rows = {r['key_id']: r for r in self.rows()}
        self.assertEqual(set(rows), {40, 50})
        self.assertEqual(rows[40]['name'], rows[50]['name'])
        self.assertEqual(rows[40]['tokens_per_sec'], 20)
        self.assertEqual(rows[50]['tokens_per_sec'], 25)

    def test_cache_rate_uses_reported_input_not_output_or_request_count(self):
        self.usage(32, 90, 10)
        self.usage(32, 10, 90, model='other')
        self.assertEqual(self.rows()[0]['cache_hit_rate'], 0.5)
        models = {r['name']: r for r in stats.by_model(1, 'cn', {})}
        self.assertEqual(models['model']['cache_hit_rate'], 0.9)
        self.assertEqual(models['other']['cache_hit_rate'], 0.1)

    def test_missing_cache_is_null_but_known_zero_hit_is_zero(self):
        self.usage(32, 0, 0)
        self.usage(40, 0, 100)
        rows = {r['key_id']: r for r in self.rows()}
        self.assertIsNone(rows[32]['cache_hit_rate'])
        self.assertEqual(rows[40]['cache_hit_rate'], 0)

    def test_mixed_model_rate_is_input_weighted_not_mean_of_percentages(self):
        for model, count, prompt, output, hit, miss in (
            ('cn:deepseek-v4.1-flash', 3130, 448120299, 3233527, 439610362, 8509937),
            ('cn:hy3', 4358, 303590977, 4621202, 59696256, 243894721),
        ):
            self.usage(32, hit, miss, model=model)
            db.execute('UPDATE usage_daily SET requests=?,prompt_tokens=?,completion_tokens=? '
                       'WHERE key_id=? AND model=?', (count, prompt, output, 32, model))
        row = self.rows()[0]
        self.assertEqual(row['requests'], 7488)
        self.assertEqual(row['cache_hit_tokens'], 499306618)
        self.assertEqual(row['cache_miss_tokens'], 252404658)
        self.assertAlmostEqual(row['cache_hit_rate'], 0.664227, places=6)
        rates = {row['name']: row['cache_hit_rate'] for row in stats.by_model(1, 'cn', {})}
        self.assertAlmostEqual(rates['deepseek-v4.1-flash'], 0.981010, places=6)
        self.assertAlmostEqual(rates['hy3'], 0.196634, places=6)

    def cache_log(self, hit, miss, *, model='cn:model', realm='cn', protocol='anthropic', ts=None):
        db.execute('INSERT INTO request_logs(ts,key_id,model,realm,protocol,cache_hit_tokens,'
                   'cache_miss_tokens) VALUES(?,?,?,?,?,?,?)',
                   (time.time() if ts is None else ts, 32, model, realm, protocol, hit, miss))

    def test_request_incidence_is_distinct_from_token_efficiency_and_scope_is_exact(self):
        self.usage(32, 1, 999)
        self.usage(32, 0, 1, model='other')
        self.cache_log(1, 999)
        self.cache_log(0, 1, model='other')
        for hit, miss in ((None, None), (0, 0), (-1, 10), (1, None)):
            self.cache_log(hit, miss)
        self.cache_log(100, 0, realm='global')
        self.cache_log(100, 0, protocol='http')
        self.cache_log(100, 0, ts=stats._epoch_since(1)-1)
        before = [dict(row) for row in db.query('SELECT * FROM usage_daily')]
        row = self.rows()[0]
        self.assertEqual((row['cache_hit_requests'], row['cache_observed_requests']), (1, 2))
        self.assertEqual(row['cache_request_hit_rate'], .5)
        self.assertAlmostEqual(row['cache_hit_rate'], 1/1001, places=6)
        models = {row['name']: row for row in stats.by_model(1, 'cn', {})}
        self.assertEqual(models['model']['cache_request_hit_rate'], 1)
        self.assertEqual(models['other']['cache_request_hit_rate'], 0)
        adapter = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        adapter.cfg = SimpleNamespace(wb_read_mode='sqlite', wb_db=str(config.DB_PATH))
        self.assertEqual(adapter._usage_by_key_from_sql(1, 'cn')[0]['cache_request_hit_rate'], .5)
        self.assertEqual(before, [dict(row) for row in db.query('SELECT * FROM usage_daily')])

    def test_daily_token_totals_cannot_invent_request_cache_samples(self):
        self.usage(32)
        row = self.rows()[0]
        self.assertIsNone(row['cache_request_hit_rate'])
        self.assertEqual(row['cache_observed_requests'], 0)
        with self.assertRaises(KeyError):
            cachemetrics.request_rates(db.query, 0, None, 'key_id; DELETE FROM api_keys')

    def test_weighted_generation_speed_and_sample_count(self):
        self.usage(32)
        self.log(32)
        self.log(32, output=100, latency=2000, first=None)
        self.assertEqual(self.rows()[0]['tokens_per_sec'], 37.5)
        self.assertEqual(self.rows()[0]['throughput_samples'], 2)

    def test_failed_cancelled_invalid_or_outside_window_not_samples(self):
        self.usage(32)
        self.log(32)
        for args in [{'status': 500}, {'terminal': 'cancelled'},
                     {'terminal': 'protocol_incomplete'}, {'latency': 1000},
                     {'latency': 0}, {'first': -1}, {'output': 0},
                     {'realm': 'global'}, {'ts': stats._epoch_since(1) - 1}]:
            self.log(32, **args)
        row = self.rows()[0]
        self.assertEqual(row['tokens_per_sec'], 25)
        self.assertEqual(row['throughput_samples'], 1)

    def test_missing_logs_never_invent_output_speed(self):
        self.usage(32)
        self.assertIsNone(self.rows()[0]['tokens_per_sec'])
        self.assertEqual(self.rows()[0]['throughput_samples'], 0)

    def test_scope_applies_to_daily_rows_as_well_as_speed(self):
        self.usage(32)
        self.usage(32, realm='global')
        self.usage(40, ts=time.time() - 86400 * 3)
        self.log(32, realm='global', output=100)
        self.assertEqual(self.rows()[0]['requests'], 1)
        self.assertIsNone(self.rows()[0]['tokens_per_sec'])
        self.assertEqual(self.rows('global')[0]['tokens_per_sec'], 50)

    def test_sqlite_adapter_preserves_current_keys_and_metrics(self):
        self.usage(999)
        self.usage(32)
        self.log(32)
        adapter = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        adapter.cfg = SimpleNamespace(wb_read_mode='sqlite', wb_db=str(config.DB_PATH))
        rows = adapter._usage_by_key_from_sql(1, 'cn')
        self.assertEqual([r['key_id'] for r in rows], [32])
        self.assertEqual(rows[0]['cache_hit_rate'], 0.8)
        self.assertEqual(rows[0]['tokens_per_sec'], 25)
        self.assertEqual(adapter._usage_by_model_from_sql(1, 'cn')[0]['cache_hit_rate'], 0.8)

    def test_sqlite_adapter_legacy_timing_is_unknown_not_zero(self):
        self.usage(32)
        adapter = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        adapter.cfg = SimpleNamespace(wb_read_mode='sqlite', wb_db=str(config.DB_PATH))
        db.execute('ALTER TABLE request_logs RENAME COLUMN first_token_ms TO legacy_timing')
        for rows in (adapter._usage_by_key_from_sql(1, 'cn'),
                     adapter._usage_by_model_from_sql(1, 'cn')):
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]['tokens_per_sec'])
            self.assertEqual(rows[0]['throughput_samples'], 0)
            self.assertEqual(rows[0]['cache_hit_rate'], 0.8)

    def test_legacy_schema_without_protocol_cannot_invent_request_cache_rate(self):
        self.usage(32)
        adapter = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
        adapter.cfg = SimpleNamespace(wb_read_mode='sqlite', wb_db=str(config.DB_PATH))
        db.execute('ALTER TABLE request_logs RENAME COLUMN protocol TO legacy_protocol')
        for rows in (adapter._usage_by_key_from_sql(1, 'cn'),
                     adapter._usage_by_model_from_sql(1, 'cn')):
            self.assertIsNone(rows[0]['cache_request_hit_rate'])
            self.assertEqual(rows[0]['cache_observed_requests'], 0)
            self.assertEqual(rows[0]['cache_hit_rate'], .8)

    def test_deleting_current_key_hides_only_its_row_not_model_totals(self):
        self.usage(32)
        self.log(32)
        self.usage(40)
        before = stats.by_model(1, 'cn', {})
        db.execute('DELETE FROM api_keys WHERE id=?', (32,))
        self.assertEqual([r['key_id'] for r in self.rows()], [40])
        self.assertEqual(stats.by_model(1, 'cn', {}), before)

    def test_dynamic_throughput_dimension_cannot_be_sql(self):
        with self.assertRaises(KeyError):
            stats._throughput_by_dimension(1, None, 'key_id; DELETE FROM api_keys')

    def test_sqlite_statistics_cutoff_matches_manager_local_date_not_fixed_china_date(self):
        # Both sides must use the database writer's day convention, including
        # the UTC evening where a fixed +08 clock has already entered tomorrow.
        moments = (1793289600.0, 1793318399.0, 1793318400.0)
        for now in moments:
            with patch('time.time', return_value=now):
                for days in (1, 7, 30, 400, 3650):
                    with self.subTest(now=now, days=days):
                        self.assertEqual(WorkBuddyAdapter._stats_since(days), stats._since(days))

    @unittest.skipUnless(hasattr(time, 'tzset'), 'POSIX timezone conversion contract')
    def test_statistics_keep_current_key_metrics_on_both_sides_of_local_midnight(self):
        previous_tz = os.environ.get('TZ')
        try:
            for zone in ('UTC0', 'CST-8', 'EST5'):
                os.environ['TZ'] = zone
                time.tzset()
                for now in (1793289599.0, 1793289601.0, 1793318399.0, 1793318401.0):
                    with patch('time.time', return_value=now), self.subTest(zone=zone, now=now):
                        db.execute('DELETE FROM usage_daily')
                        db.execute('DELETE FROM request_logs')
                        self.usage(32, ts=now)
                        self.log(32, ts=now)
                        adapter = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
                        adapter.cfg = SimpleNamespace(wb_read_mode='sqlite', wb_db=str(config.DB_PATH))
                        rows = adapter._usage_by_key_from_sql(1, 'cn')
                        self.assertEqual([row['key_id'] for row in rows], [32])
                        self.assertEqual(rows[0]['cache_hit_rate'], 0.8)
                        self.assertEqual(rows[0]['tokens_per_sec'], 25)
                        self.assertEqual(adapter._usage_by_model_from_sql(1, 'cn')[0]['cache_hit_rate'], 0.8)
        finally:
            if previous_tz is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = previous_tz
            time.tzset()


class CompatStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_manager_result_is_authoritative_no_old_key_fallback(self):
        wb = SimpleNamespace(usage_by_key=AsyncMock(return_value=[]), usage_by_model=AsyncMock(return_value=[]))
        rt = SimpleNamespace(wb=wb)
        request = Request({'type': 'http', 'headers': [(b'x-qtg-platform', b'workbuddy')],
                           'query_string': b'', 'app': SimpleNamespace(state=SimpleNamespace(rt=rt))})
        with patch.object(compat_stats, '_payload', side_effect=AssertionError('fallback')):
            self.assertEqual(await compat_stats.stats_by_key(request, 1, 'cn'), [])
            self.assertEqual(await compat_stats.stats_by_model(request, 1, 'cn'), [])


if __name__ == '__main__':
    unittest.main()
