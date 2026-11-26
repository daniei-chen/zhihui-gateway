"""Exact key/platform/recorded-realm evidence without billing-counter changes."""
import datetime as dt
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from server import config, db, platformstats
from server.qtgcore.routers import compat_stats


class PlatformKeyStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = config.DB_PATH, db._conn
        config.DB_PATH = Path(self.tmp.name) / 'stats.db'
        db._conn = None
        self.conn = db.connect()
        for key_id, name, enabled in ((32, '32', 1), (40, 'same', 1), (50, 'same', 0)):
            db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at,enabled) VALUES(?,?,?,?,?,?)',
                       (key_id, name, str(key_id), 'synthetic', time.time(), enabled))

    def tearDown(self):
        db._conn.close()
        config.DB_PATH, db._conn = self.old
        self.tmp.cleanup()

    def log(self, key=32, **changes):
        values = dict(ts=int(time.time()), key_id=key, actual_platform='qoder', platform='qoder',
                      protocol='openai', endpoint='/v1/chat/completions', realm='cn',
                      model='same-model', status=200, terminal_state='completed',
                      prompt_tokens=100, completion_tokens=50, credit=0.5, usage_complete=1,
                      latency_ms=3000, first_token_ms=1000, cache_hit_tokens=80, cache_miss_tokens=20)
        values.update(changes)
        return db.execute('INSERT INTO request_logs('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',
                          tuple(values.values()))

    def rows(self, realm='cn', platform='qoder', days=1):
        return platformstats.key_usage(self.conn, days, realm, platform)

    def test_exact_platform_recorded_realm_and_stable_identity_not_key_policy_or_name(self):
        self.log()
        self.log(40, completion_tokens=20)
        self.log(50, completion_tokens=70)
        self.log(999)
        self.log(actual_platform='trae')
        self.log(actual_platform='workbuddy')
        self.log(actual_platform=None, platform='qoder')
        self.log(realm='global', completion_tokens=200)
        self.log(realm=None)
        self.log(protocol='http', endpoint='/api/accounts')
        self.log(ts=time.time() - 3 * 86400)
        rows = {row['key_id']: row for row in self.rows()}
        self.assertEqual(set(rows), {32, 40, 50})
        self.assertEqual(rows[32]['name'], '32', 'An existing numeric key name is legitimate')
        self.assertEqual(rows[40]['name'], rows[50]['name'])
        self.assertEqual(rows[32]['requests'], 1)
        self.assertEqual(rows[50]['completion_tokens'], 70)
        self.assertEqual(self.rows('global')[0]['completion_tokens'], 200)
        self.assertEqual(self.rows(platform='trae')[0]['requests'], 1)
        self.assertEqual(next(r for r in self.rows('') if r['key_id'] == 32)['requests'], 3)

    def test_complete_tokens_credit_cache_and_weighted_speed_have_explicit_samples(self):
        self.log()
        self.log(completion_tokens=100, latency_ms=2000, first_token_ms=None,
                 cache_hit_tokens=0, cache_miss_tokens=100)
        row = self.rows()[0]
        self.assertEqual((row['requests'], row['total_tokens'], row['credit']), (2, 350, 1))
        self.assertEqual((row['cache_hit_rate'], row['cache_samples']), (0.4, 2))
        self.assertEqual((row['cache_hit_requests'], row['cache_observed_requests'],
                          row['cache_request_hit_rate']), (1, 2, .5))
        self.assertEqual((row['tokens_per_sec'], row['throughput_samples']), (37.5, 2))
        self.assertEqual(row['source'], 'manager_retained_request_logs')

    def test_unknown_is_not_zero_and_does_not_fabricate_split_or_charge(self):
        self.log(usage_complete=0, credit=None, cache_hit_tokens=None, cache_miss_tokens=None)
        row = self.rows()[0]
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'credit',
                      'cache_hit_rate', 'cache_hit_tokens', 'tokens_per_sec'):
            self.assertIsNone(row[field], field)
        self.assertEqual((row['token_unknown_requests'], row['credit_unknown_requests']), (1, 1))
        self.assertEqual(row['throughput_samples'], 0)
        self.assertIsNone(row['cache_request_hit_rate'])
        self.assertEqual(row['cache_observed_requests'], 0)
        self.log(40, prompt_tokens=0, completion_tokens=0, credit=0, cache_hit_tokens=0, cache_miss_tokens=10)
        zero = next(r for r in self.rows() if r['key_id'] == 40)
        self.assertEqual((zero['total_tokens'], zero['credit'], zero['cache_hit_rate']), (0, 0, 0))
        self.log(40, credit=None)
        self.assertIsNone(next(r for r in self.rows() if r['key_id'] == 40)['credit'])

    def test_failure_cancellation_invalid_timing_and_unknown_usage_are_not_speed_samples(self):
        self.log()
        for change in ({'status': 500}, {'terminal_state': 'client_cancelled'},
                       {'terminal_state': None}, {'usage_complete': 0}, {'latency_ms': 0},
                       {'latency_ms': 1000}, {'first_token_ms': -1}, {'completion_tokens': 0}):
            self.log(**change)
        row = self.rows()[0]
        self.assertEqual(row['throughput_samples'], 1)
        self.assertEqual(row['tokens_per_sec'], 25)
        self.assertIsNone(row['total_tokens'])

    def test_window_is_local_calendar_bounded_at_next_midnight(self):
        # 「当地零点」必须是**产品用的那个时区**（固定 UTC+8，见 `db.day_of`），
        # 不是进程时区。
        #
        # 原先这里用 `dt.date.today()` + `time.mktime` —— 两者都跟随进程 TZ。
        # 在 UTC+8 主机上恰好与产品一致，测试绿；在 UTC runner 上 today 与产品的
        # UTC+8 日期错开，`days=2` 的窗口少算一天，断言 `2 != 3` 失败。
        # 与 `test_product_stats` 那组失败是同一个根因。
        today = db.day_of()
        midnight = dt.datetime.combine(
            dt.date.fromisoformat(today), dt.time.min, tzinfo=db._DAY_TZ)
        start = midnight.timestamp()
        end = (midnight + dt.timedelta(days=1)).timestamp()
        self.log(ts=int(start) - 1)
        self.log(ts=int(start))
        self.log(ts=int(end) - 1)
        self.log(ts=int(end))
        row = self.rows()[0]
        self.assertEqual(row['requests'], 2)
        self.assertEqual((row['from'], row['to']), (today, today))
        self.assertEqual(self.rows(days=2)[0]['requests'], 3)

    def test_deleted_key_hidden_and_queries_never_modify_logs_ledger_or_budgets(self):
        self.log()
        self.log(40)
        before = list(self.conn.iterdump())
        self.conn.execute('PRAGMA query_only=1')
        try:
            self.assertEqual(len(self.rows()), 2)
        finally:
            self.conn.execute('PRAGMA query_only=0')
        self.assertEqual(list(self.conn.iterdump()), before)
        db.execute('DELETE FROM api_keys WHERE id=32')
        self.assertEqual([r['key_id'] for r in self.rows()], [40])
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM request_logs')['n'], 2)
        db.execute('DELETE FROM request_logs')
        self.assertEqual(self.rows(), [])

    def test_malformed_or_missing_storage_fails_instead_of_falling_back_to_legacy(self):
        self.log(prompt_tokens=-1, credit=float('inf'), cache_hit_tokens=None)
        row = self.rows()[0]
        self.assertIsNone(row['total_tokens'])
        self.assertIsNone(row['credit'])
        self.assertIsNone(row['cache_hit_rate'])
        for realm, platform in (('intl', 'qoder'), ('cn', 'all'), ('cn', 'qoder;DROP TABLE api_keys')):
            with self.assertRaises(ValueError):
                self.rows(realm, platform)
        db.execute('ALTER TABLE request_logs RENAME COLUMN actual_platform TO obsolete_platform')
        with self.assertRaises(sqlite3.OperationalError):
            self.rows()

    def test_http_route_and_serializer_use_current_manager_keys_not_qtg_store(self):
        self.log()
        app = FastAPI()
        app.include_router(compat_stats.router)
        # No runtime object is installed: QTG store/provider access must not occur.
        with TestClient(app) as client:
            response = client.get('/api/stats/by-key?days=1&realm=cn', headers={'X-QTG-Platform': 'qoder'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()[0]['key_id'], 32)
            self.assertEqual(response.json()[0]['total_tokens'], 150)
            self.assertNotIn('key_hash', response.text)
            self.assertEqual(client.get('/api/stats/by-key?realm=intl',
                                       headers={'X-QTG-Platform': 'qoder'}).status_code, 422)


class PlatformKeyDailySeriesTests(PlatformKeyStatsTests):
    """「这个 key 哪天用了多少」要和窗口合计对得上。

    原来这张表只给合计，看到「32 号用了 4 千万 token」说明不了是哪天用的 ——
    而这正是「昨天为什么突然翻倍」要回答的问题。日序列故意从同一个 WHERE 复算，
    不读 `key_daily_usage`：两张表的 tokens 不是一套口径，并排放会自相矛盾。
    """

    def _days(self, row):
        return {d['day']: d for d in row['daily']}

    def _today(self, now):
        tz = dt.timezone(dt.timedelta(hours=8))
        return dt.datetime.fromtimestamp(now, tz).date().isoformat()

    def test_daily_sums_back_to_the_total(self):
        now = time.time()
        self.log(completion_tokens=50)                      # 今天第一条
        self.log(completion_tokens=50)                      # 今天第二条
        self.log(ts=now - 86400, completion_tokens=30)      # 昨天
        self.log(ts=now - 2 * 86400, completion_tokens=10)  # 前天
        row = {r['key_id']: r for r in self.rows(days=7)}[32]
        self.assertEqual(sum(d['requests'] for d in row['daily']), row['requests'])
        self.assertEqual(sum(d['tokens'] for d in row['daily']), row['total_tokens'])
        self.assertEqual(len(row['daily']), 3)

    def test_same_day_rows_collapse_into_one_bucket(self):
        now = time.time()
        self.log()
        self.log()
        row = {r['key_id']: r for r in self.rows()}[32]
        day = self._days(row)[self._today(now)]
        self.assertEqual(day['requests'], 2)
        self.assertEqual(day['tokens'], 300)   # 两条各 100+50

    def test_rows_outside_the_window_stay_out_of_the_series(self):
        self.log()
        self.log(ts=time.time() - 2 * 86400)
        row = {r['key_id']: r for r in self.rows(days=1)}[32]
        self.assertEqual(len(row['daily']), 1)
        self.assertEqual(row['requests'], 1)

    def test_unknown_token_day_is_null_not_a_smaller_number(self):
        # 当天有一条没上报 token：那天的 tokens 给 null，而不是只算已知部分 ——
        # 否则用户看到的是「少了」而不是「不知道」。
        self.log(usage_complete=0)
        row = {r['key_id']: r for r in self.rows()}[32]
        self.assertIsNone(row['daily'][0]['tokens'])
        self.assertIsNone(row['total_tokens'])

    def test_no_rows_means_empty_series_not_missing_key(self):
        self.assertEqual(self.rows(), [])


class PlatformKeyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_storage_and_unsafe_evidence_return_unavailable_not_empty(self):
        request = Request({'type': 'http', 'headers': [(b'x-qtg-platform', b'qoder')]})
        for error in (sqlite3.OperationalError('private schema details'), ValueError('private invalid data')):
            with patch.object(platformstats, 'current_key_usage', side_effect=error), \
                    patch.object(compat_stats, '_rt', side_effect=AssertionError('fallback forbidden')), \
                    self.assertRaises(HTTPException) as caught:
                await compat_stats.stats_by_key(request, 1, 'cn')
            self.assertEqual(caught.exception.status_code, 503)
            self.assertNotIn('private', caught.exception.detail)

    async def test_qoder_trae_direct_source_and_empty_result_are_authoritative(self):
        for platform in ('qoder', 'trae'):
            request = Request({'type': 'http', 'headers': [(b'x-qtg-platform', platform.encode())]})
            with patch.object(platformstats, 'current_key_usage', return_value=[]) as source, \
                    patch.object(compat_stats, '_rt', side_effect=AssertionError('legacy store forbidden')):
                self.assertEqual(await compat_stats.stats_by_key(request, 100000, 'global'), [])
                source.assert_called_once_with(90, 'global', platform)

    async def test_invalid_realm_rejected_before_any_source_read(self):
        request = Request({'type': 'http', 'headers': [(b'x-qtg-platform', b'qoder')]})
        with patch.object(platformstats, 'current_key_usage') as source, self.assertRaises(HTTPException) as caught:
            await compat_stats.stats_by_key(request, 1, 'intl')
        self.assertEqual(caught.exception.status_code, 422)
        source.assert_not_called()
