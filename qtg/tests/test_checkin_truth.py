"""签到结果必须说真话（2026-10-23 用户实测缺陷的回归测试）。

用户反馈「签到完了刷新没数字变动」。实测上游返回的是 already
（qoder: earned_credit=0 / claimed=[]；trae: checkin_results.status=already），
qtg 却一律统计成 succeeded，界面显示「签到成功 1/1」，用户以为刚发了积分。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from qtg.adapters.trae2 import Trae2Adapter
from qtg.routers.compat import _batch_results


def _make_db(tmp: str) -> str:
    p = str(Path(tmp) / "aiwork.sqlite")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE credits_daily (date TEXT PRIMARY KEY, total REAL,
                                    earned REAL, consumed REAL);
        CREATE TABLE checkin_results (day TEXT, uid TEXT, name TEXT, status TEXT,
                                      updated_at TEXT, PRIMARY KEY(day, uid));
        """
    )
    conn.commit()
    conn.close()
    return p


class CreditsDailyTest(unittest.TestCase):
    def test_reads_real_table(self) -> None:
        """`credits_daily` 是积分收入的唯一数据源；之前硬编码返回空，
        导致 /api/stats/credits 的 income 永远是 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            conn = sqlite3.connect(db)
            conn.executemany(
                "INSERT INTO credits_daily VALUES (?,?,?,?)",
                [("2026-10-22", 1543.43, 100.0, 6.57),
                 ("2026-10-23", 1543.43, 0.0, 0.0)],
            )
            conn.commit()
            conn.close()
            a = Trae2Adapter.__new__(Trae2Adapter)
            a.cfg = SimpleNamespace(aiwork_db=db)
            rows = a._credits_daily(7)
            self.assertEqual([r["date"] for r in rows], ["2026-10-22", "2026-10-23"])
            self.assertEqual(rows[0]["earned"], 100.0)
            self.assertEqual(rows[0]["consumed"], 6.57)
            self.assertEqual(rows[1]["earned"], 0.0)


class CheckinTruthTest(unittest.IsolatedAsyncioTestCase):
    async def _with_status(self, status: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = _make_db(tmp.name)
        a = Trae2Adapter.__new__(Trae2Adapter)
        a.cfg = SimpleNamespace(aiwork_db=db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO checkin_results VALUES (?,?,?,?,?)",
            (a._today(), "u1", "前程如风", status, "2026-10-23 01:00:00"),
        )
        conn.commit()
        conn.close()
        a._cmd = AsyncMock(return_value={"ok": True})
        return await a.checkin()

    async def test_already_is_reported_as_already(self) -> None:
        out = await self._with_status("already")
        row = out["results"][0]
        self.assertTrue(row["ok"])
        self.assertEqual(row["status"], "already")
        self.assertIn("已签到", row["message"])
        self.assertNotIn("签到成功", row["message"])

    async def test_success_is_reported_as_success(self) -> None:
        out = await self._with_status("success")
        row = out["results"][0]
        self.assertTrue(row["ok"])
        self.assertEqual(row["status"], "success")
        self.assertIn("成功", row["message"])

    async def test_failed_is_reported_as_failure(self) -> None:
        out = await self._with_status("failed")
        self.assertFalse(out["ok"])
        self.assertFalse(out["results"][0]["ok"])

    async def test_nickname_is_the_real_account(self) -> None:
        """之前固定写成 "Trae"，用户看不出是哪个号签的。"""
        out = await self._with_status("already")
        self.assertEqual(out["results"][0]["nickname"], "前程如风")


class BatchResultsAlreadyTest(unittest.TestCase):
    def test_qoder_zero_earned_marks_already(self) -> None:
        out = _batch_results({"results": [
            {"nickname": "轻松熊", "ok": True, "earned_credit": 0,
             "message": "今日活动奖励已领取（每天领 100 Credits）",
             "next_available_note": "10-23 10:00（UTC+8）"},
            {"nickname": "新号", "ok": True, "earned_credit": 100,
             "message": "领取成功"},
            {"nickname": "Trae", "ok": True, "status": "already"},
            {"nickname": "坏号", "ok": False, "message": "失败"},
        ]})
        self.assertTrue(out[0]["already"], "earned_credit=0 必须判为已签")
        self.assertEqual(out[0]["next_available_note"], "10-23 10:00（UTC+8）")
        self.assertFalse(out[1]["already"], "真领到 100 不能判成已签")
        self.assertTrue(out[2]["already"], "status=already 必须判为已签")
        self.assertFalse(out[3]["already"], "失败不是已签")


if __name__ == "__main__":
    unittest.main()
