"""用量统计的时区口径与重建（P0 回归）。

历史缺陷：写入用量用本地日期（time.strftime），回填却用 UTC
（SQLite 的 date(ts,'unixepoch')）。在 UTC+8 的机器上，凌晨 00:00-08:00
的调用会被算进两个不同的 day，回填时把同一次调用重复计数。

当前合同：Python、SQL 和回填统一使用固定 UTC+8 业务日，不随宿主机 TZ 改变。
用子进程覆盖 UTC、UTC+8、UTC+9，并在北京时间午夜边界核对 Python 与 SQL。
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402


def business_midnight_plus(minutes: int) -> int:
    """北京时间今天 00:minutes，不依赖执行测试的机器时区。"""
    return db.day_start_ts() + minutes * 60


class UsageTimezone(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'u.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _seed(self, ts: int, pt: int = 100, ct: int = 50) -> None:
        db.execute(
            'INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, max_ips, '
            'ip_allowlist, models, quota, used_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('k', 'h', 'wbk_abc', 1, None, 0, '[]', '[]', 0, 0, ts),
        )
        db.execute(
            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
            'prompt_tokens, completion_tokens, latency_ms, ua, error, stream) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (ts, 1, '1.2.3.4', 'glm-5.2', 'glm-5.2', 200, pt, ct, 10, 'ua', '', 0),
        )

    def _daily(self, ts: int, requests: int = 1, pt: int = 100, ct: int = 50) -> None:
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens) '
            'VALUES(?,?,?,?,?,?)',
            (db.day_of(ts), 1, 'glm-5.2', requests, pt, ct),
        )

    # ── 口径一致性（时区无关）────────────────────────────
    def test_day_of_and_sql_agree(self) -> None:
        ts = business_midnight_plus(30)
        via_py = db.day_of(ts)
        via_sql = db.query_one(
            f'SELECT {db.day_sql("ts")} AS d FROM (SELECT {ts} AS ts)'
        )['d']
        self.assertEqual(via_py, via_sql, 'Python 与 SQL 的取日口径必须一致')

    def test_day_of_uses_business_timezone_across_host_timezones(self) -> None:
        """UTC+8 的午夜边界必须与宿主机时区无关。"""
        cases = (
            ('2026-11-26T15:59:59+00:00', '2026-11-26'),
            ('2026-11-26T16:00:00+00:00', '2026-11-27'),
            ('2026-11-26T23:30:00+00:00', '2026-11-27'),
            ('2026-11-27T16:00:00+00:00', '2026-11-28'),
        )
        timestamps = [int(datetime.datetime.fromisoformat(value).timestamp()) for value, _ in cases]
        expected = [[day, day] for _, day in cases]
        script = '''
import json, sqlite3, sys, time
from server import db
if hasattr(time, "tzset"):
    time.tzset()
with sqlite3.connect(":memory:") as conn:
    print(json.dumps([
        [db.day_of(ts), conn.execute("SELECT " + db.day_sql("?"), (ts,)).fetchone()[0]]
        for ts in json.loads(sys.argv[1])
    ]))
'''
        for host_tz in ('UTC0', 'CST-8', 'JST-9'):
            with self.subTest(host_tz=host_tz):
                result = subprocess.run(
                    [sys.executable, '-B', '-c', script, json.dumps(timestamps)],
                    cwd=Path(__file__).resolve().parents[2],
                    env={**os.environ, 'TZ': host_tz}, capture_output=True,
                    text=True, check=True, timeout=15,
                )
                self.assertEqual(json.loads(result.stdout), expected)

    def test_day_sql_is_not_naive_utc(self) -> None:
        """SQL 表达式必须带时区修饰符（这正是当初出错的地方）。

        v1.2.39 起用固定 `+8 hours` 而不是 `localtime`：口径不能再跟着进程 TZ 走，
        否则换台机器就会和硬编码 UTC+8 的用户账本分家。
        """
        self.assertIn('+8 hours', db.day_sql('ts'))
        self.assertNotIn('localtime', db.day_sql('ts'))
        ts = int(time.time())
        naive_utc = db.query_one(
            f"SELECT date({ts}, 'unixepoch') AS d"
        )['d']
        ours = db.query_one(f'SELECT {db.day_sql(str(ts))} AS d')['d']
        # 与 day_of 同口径（固定 UTC+8），不再跟随本机时区
        self.assertEqual(ours, db.day_of(ts))
        self.assertIsInstance(naive_utc, str)

    # ── 不再重复计数 ────────────────────────────────────
    def test_backfill_after_bump_has_no_gap(self) -> None:
        """按 bump_usage 语义写入业务日后，回填应无缺口。"""
        ts = business_midnight_plus(30)
        self._seed(ts)
        self._daily(ts)
        res = db.backfill_usage_from_logs()
        self.assertEqual(res['repaired'], 0, '口径一致时不应产生回填')
        self.assertEqual(len(db.query('SELECT day FROM usage_daily')), 1)

    def test_backfill_does_not_create_second_day(self) -> None:
        ts = business_midnight_plus(30)
        self._seed(ts)
        db.backfill_usage_from_logs()
        days = [r['day'] for r in db.query('SELECT day FROM usage_daily')]
        self.assertEqual(days, [db.day_of(ts)])

    # ── 重建清理污染（不依赖机器时区）────────────────────
    def test_rebuild_removes_polluted_rows(self) -> None:
        """污染用一个「多出来的 day 行」模拟，任何时区都能构造。"""
        ts = business_midnight_plus(30)
        self._seed(ts)
        real_day = db.day_of(ts)
        # 造一个额外的（错误的）日行，模拟历史上被算到另一天
        polluting_day = '1999-01-01'
        for d in (real_day, polluting_day):
            db.execute(
                'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens) '
                'VALUES(?,?,?,?,?,?)',
                (d, 1, 'glm-5.2', 1, 100, 50),
            )
        before = db.query_one('SELECT SUM(requests) r FROM usage_daily')['r']
        self.assertEqual(before, 2, '应有两行（含一条污染）')

        res = db.rebuild_usage_from_logs()
        after = db.query_one('SELECT COUNT(*) c, SUM(requests) r FROM usage_daily')
        self.assertEqual(after['c'], 1, '重建后应只剩一行')
        self.assertEqual(after['r'], 1, '重建后请求数应回落到原始值 1')
        self.assertEqual(res['requests_delta'], -1)
        self.assertEqual(
            db.query_one('SELECT day FROM usage_daily')['day'], real_day
        )

    def test_rebuild_is_idempotent(self) -> None:
        ts = business_midnight_plus(30)
        self._seed(ts)
        db.rebuild_usage_from_logs()
        first = db.query_one('SELECT COUNT(*) c, SUM(requests) r FROM usage_daily')
        res2 = db.rebuild_usage_from_logs()
        second = db.query_one('SELECT COUNT(*) c, SUM(requests) r FROM usage_daily')
        self.assertEqual((first['c'], first['r']), (second['c'], second['r']))
        self.assertEqual(res2['requests_delta'], 0)

    def test_rebuild_keeps_multiple_days_separate(self) -> None:
        """不同两天的数据重建后仍应各占一行。"""
        now = int(time.time())
        for ts, pt in ((now, 100), (now - 86400, 200)):
            self._seed(ts, pt=pt)
        db.rebuild_usage_from_logs()
        rows = db.query('SELECT day, prompt_tokens FROM usage_daily ORDER BY day')
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['prompt_tokens'] for r in rows}, {100, 200})


if __name__ == '__main__':
    unittest.main()
