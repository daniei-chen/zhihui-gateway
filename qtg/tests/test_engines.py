"""双引擎适配器单测（不依赖真实上游）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from qtg.adapters.qoder2 import Qoder2Adapter
from qtg.adapters.trae2 import Trae2Adapter
from qtg.config import _file_switch


class FileSwitchTest(unittest.TestCase):
    def test_missing_falls_back(self) -> None:
        self.assertEqual(_file_switch("/nonexistent/x", ("bff", "qoder2"), "bff"), "bff")

    def test_valid(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "e"
            p.write_text("qoder2\n", encoding="utf-8")
            self.assertEqual(_file_switch(str(p), ("bff", "qoder2"), "bff"), "qoder2")

    def test_invalid_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "e"
            p.write_text("../../etc/passwd", encoding="utf-8")
            self.assertEqual(_file_switch(str(p), ("bff", "qoder2"), "bff"), "bff")


class Qoder2UsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_series_to_buckets_keeps_split(self) -> None:
        a = Qoder2Adapter.__new__(Qoder2Adapter)
        a._json = AsyncMock(side_effect=[
            {"series": [{"day": "2026-10-22", "requests": 2, "total_tokens": 5195,
                         "credit": 0.0364, "prompt_tokens": 5123,
                         "completion_tokens": 72, "errors": 0}]},
            {"by_model": {"Auto": {"requests": 2, "total_tokens": 5195, "credit": 0.0364}}},
        ])
        out = await a.usage(3)
        b = out["buckets"][0]
        self.assertEqual(b["label"], "10/22")
        self.assertEqual(b["prompt_tokens"], 5123)
        self.assertEqual(b["completion_tokens"], 72)
        self.assertEqual(out["by_model"][0]["model"], "Auto")


class Trae2UsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_command(self) -> None:
        a = Trae2Adapter.__new__(Trae2Adapter)
        a._cmd = AsyncMock(return_value={"data": [{
            "date": "2026-10-22", "total_requests": 22, "ok": 22, "errors": 0,
            "prompt_tokens": 228188, "completion_tokens": 3275, "models": []}]})
        out = await a.usage(2)
        self.assertEqual(out["usage"][0]["requests"], 22)
        self.assertEqual(out["credits_daily"], [])

    async def test_set_account_enable_appends(self) -> None:
        a = Trae2Adapter.__new__(Trae2Adapter)
        calls = []

        async def fake(name, body=None, required=True):
            calls.append((name, body))
            return {"data": {"enabled_uids": ["a"]}} if name == "pool_list" else {"ok": True}

        a._cmd = fake
        await a.set_account("b", True)
        self.assertEqual(calls[-1][0], "pool_set")
        self.assertEqual(sorted(calls[-1][1]["uids"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
