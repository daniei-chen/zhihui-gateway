"""workbuddy 用量统计口径（2026-10-23「用量统计不对」的回归）。

用户反馈统计数字不对。实测差异（修复前）：
    total_requests  数据库 162,286  vs  qtg 33,424   （漏 79%，从 14 天窗口推 total）
    by-model 的 tokens / credit     恒为 0
    hourly                          恒为 0（读的是 qtg 自己的空流水）
    credits 的 income               恒为 0（只算支出，不读 task_logs）

这里用临时库把这些口径钉住 —— 口径来自 workbuddy manager 的
server/routers/stats.py，改之前请先对上那边。
"""
import sqlite3
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qtg.adapters.workbuddy import WorkBuddyAdapter


def _make_db(tmp: str, rows, task_rows=(), keys=()) -> str:
    p = str(Path(tmp) / "manager.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE usage_daily(day TEXT, key_id INT, model TEXT, requests INT,
            prompt_tokens INT, completion_tokens INT, credit REAL, realm TEXT,
            cache_hit_tokens INT, cache_miss_tokens INT);
        CREATE TABLE usage_hourly(day TEXT, hour INT, key_id INT, model TEXT,
            requests INT, prompt_tokens INT, completion_tokens INT, credit REAL,
            realm TEXT);
        CREATE TABLE task_logs(ts INT, uid TEXT, kind TEXT, credits REAL);
        CREATE TABLE api_keys(id INT, name TEXT, enabled INT);
        """
    )
    conn.executemany(
        "INSERT INTO usage_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO usage_hourly(day,hour,key_id,model,requests,prompt_tokens,"
        "completion_tokens,credit,realm) VALUES (?,?,?,?,?,?,?,?,?)", rows and [
            (r[0], 9, r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows])
    conn.executemany("INSERT INTO task_logs VALUES (?,?,?,?)", task_rows)
    conn.executemany("INSERT INTO api_keys VALUES (?,?,?)", keys)
    conn.commit()
    conn.close()
    return p


def _adapter(db: str) -> WorkBuddyAdapter:
    a = WorkBuddyAdapter.__new__(WorkBuddyAdapter)
    a.cfg = SimpleNamespace(wb_db=db)
    return a


class SummaryIsFullTableTest(unittest.TestCase):
    def test_total_includes_rows_outside_any_window(self) -> None:
        """total_* 必须是全表：历史行（100 天前）也要算进来。

        修复前 qtg 从「最近 14 天」窗口推 total，16 万次请求只显示 3.3 万次。
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp, [
                ("2020-01-01", 1, "m", 100, 1000, 500, 10.0, "cn", 0, 0),
                ("2020-01-02", 1, "m", 200, 2000, 1000, 20.0, "cn", 0, 0),
            ], keys=[(1, "k", 1)])
            s = asyncio.run(_adapter(db).usage_summary())
            self.assertEqual(s["total_requests"], 300, "历史行必须计入 total")
            self.assertEqual(s["total_tokens"], 4500)
            self.assertAlmostEqual(s["total_credit"], 30.0)
            self.assertEqual(s["today_requests"], 0)
            self.assertEqual(s["active_keys"], 1)


class ByModelShapeTest(unittest.TestCase):
    def test_tokens_and_credit_not_zero(self) -> None:
        """by-model 必须带 tokens/credit（修复前恒 0）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp, [
                ("2099-01-01", 1, "small", 1, 10, 5, 0.1, "cn", 7, 3),
                ("2099-01-01", 1, "big", 1, 10_000, 5_000, 9.0, "cn", 0, 0),
            ])
            rows = asyncio.run(_adapter(db).usage_by_model(400))
            self.assertTrue(rows)
            self.assertEqual(rows[0]["name"], "big", "排序按 token 总量，不是请求数")
            self.assertEqual(rows[0]["prompt_tokens"], 10_000)
            self.assertEqual(rows[0]["completion_tokens"], 5_000)
            self.assertAlmostEqual(rows[0]["credit"], 9.0)
            self.assertEqual(rows[0]["cache_hit_tokens"], 0)


class ByKeyJoinTest(unittest.TestCase):
    def test_joins_key_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp, [
                ("2099-01-01", 7, "m", 3, 300, 30, 1.5, "cn", 0, 0),
            ], keys=[(7, "my-key", 1)])
            rows = asyncio.run(_adapter(db).usage_by_key(400))
            self.assertEqual(rows[0]["name"], "my-key")
            self.assertEqual(rows[0]["requests"], 3)


class HourlyBucketsTest(unittest.TestCase):
    def test_always_24_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp, [("2099-01-01", 1, "m", 5, 50, 5, 0.5, "cn", 0, 0)])
            rows = asyncio.run(_adapter(db).usage_hourly("2099-01-01"))
            self.assertEqual(len(rows), 24, "必须固定 24 个桶")
            self.assertEqual(sum(r["requests"] for r in rows), 5)


class CreditsIncomeTest(unittest.TestCase):
    def test_income_comes_from_task_logs(self) -> None:
        """收入来自 task_logs 的 kind='credit'，支出才是 usage_daily.credit。"""
        with tempfile.TemporaryDirectory() as tmp:
            import time
            now = int(time.time())
            db = _make_db(
                tmp,
                [("2099-01-01", 1, "m", 1, 10, 5, 3.0, "cn", 0, 0)],
                task_rows=[(now, "u1", "credit", 100.0),
                           (now, "u1", "renew", 1.0)],   # renew 不算收入
            )
            flow = asyncio.run(_adapter(db).credits_flow(7))
            self.assertEqual(flow["today"]["income"], 100.0,
                             "只有 kind='credit' 计入收入")
            self.assertEqual(len(flow["daily"]), 7, "区间内每天都要补零")


if __name__ == "__main__":
    unittest.main()
