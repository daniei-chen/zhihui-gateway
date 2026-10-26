"""领域映射单测：保证上游字段变化时口径不漂移。"""
from __future__ import annotations

import unittest

from qtg import service as svc


class QoderMappingTest(unittest.TestCase):
    def test_ready_account(self) -> None:
        raw = {
            "uid": "u1",
            "nickname": "轻松熊",
            "realm": "cn",
            "enabled": True,
            "inCooldown": False,
            "credits": {"remain": 900, "size": 900, "used": 0, "expires_at": 253402214400},
            "plan": "free",
            "inFlight": 1,
            "inFlightCap": 4,
        }
        a = svc.map_qoder_account(raw)
        self.assertEqual(a.platform, "qoder")
        self.assertEqual(a.state, "ready")
        self.assertEqual(a.credits_remain, 900)
        self.assertEqual(a.in_flight, 1)
        self.assertIsNone(a.days_left)  # 253402214400 视为「长期」

    def test_low_credits(self) -> None:
        a = svc.map_qoder_account({"uid": "u", "credits": {"remain": 10, "size": 900}})
        self.assertEqual(a.state, "low")

    def test_cooling(self) -> None:
        a = svc.map_qoder_account({"uid": "u", "inCooldown": True, "credits": {"remain": 500}})
        self.assertEqual(a.state, "cooling")
        self.assertTrue(a.in_cooldown)

    def test_disabled_beats_cooling(self) -> None:
        a = svc.map_qoder_account({"uid": "u", "enabled": False, "inCooldown": True})
        self.assertEqual(a.state, "disabled")

    def test_expired(self) -> None:
        a = svc.map_qoder_account({"uid": "u", "expiresAt": 1, "credits": {"remain": 500}})
        self.assertEqual(a.state, "expired")

    def test_missing_fields_do_not_crash(self) -> None:
        a = svc.map_qoder_account({})
        self.assertEqual(a.platform, "qoder")
        self.assertEqual(a.credits_remain, 0)
        self.assertEqual(a.state, "low")


class TraeMappingTest(unittest.TestCase):
    def test_ready(self) -> None:
        raw = {
            "user_id": "2385",
            "name": "前程如风",
            "remaining_credits": 1550,
            "total_credits": 1550,
            "general_credits": 1550,
            "checked_today": True,
            "credits_expire_at": 4102444800,
        }
        a = svc.map_trae_account(raw)
        self.assertEqual(a.platform, "trae")
        self.assertEqual(a.state, "ready")
        self.assertEqual(a.credits_remain, 1550)
        self.assertTrue(a.checked_today)

    def test_fallback_to_general_credits(self) -> None:
        a = svc.map_trae_account({"user_id": "x", "remaining_credits": 0, "general_credits": 800})
        self.assertEqual(a.credits_remain, 800)

    def test_cooling(self) -> None:
        a = svc.map_trae_account({"user_id": "x", "cooldown_until": "2026-10-22 20:00:00", "remaining_credits": 900})
        self.assertEqual(a.state, "cooling")

    def test_cooldown_none_string(self) -> None:
        a = svc.map_trae_account({"user_id": "x", "cooldown_until": "None", "remaining_credits": 900})
        self.assertEqual(a.state, "ready")


class SummarizeTest(unittest.TestCase):
    def test_counts(self) -> None:
        rows = [
            svc.map_qoder_account({"uid": "1", "credits": {"remain": 900}}),
            svc.map_qoder_account({"uid": "2", "inCooldown": True, "credits": {"remain": 900}}),
            svc.map_qoder_account({"uid": "3", "enabled": False}),
            svc.map_trae_account({"user_id": "4", "remaining_credits": 5}),
        ]
        s = svc.summarize(rows)
        self.assertEqual(s.total, 4)
        self.assertEqual(s.ready, 1)
        self.assertEqual(s.cooling, 1)
        self.assertEqual(s.disabled, 1)
        self.assertEqual(s.low, 1)


class DateHelpersTest(unittest.TestCase):
    def test_month_day(self) -> None:
        self.assertTrue(svc._month_day_to_date("10/9", __import__("datetime").date(2026, 10, 22)))
        self.assertIsNone(svc._month_day_to_date("bad"))

    def test_fmt_num(self) -> None:
        self.assertEqual(svc._fmt_num(900), "900")
        self.assertEqual(svc._fmt_num(1234), "1,234")
        self.assertEqual(svc._fmt_num(12000), "12.0k")
        self.assertEqual(svc._fmt_num(1_500_000), "1.5M")
        self.assertEqual(svc._fmt_num(None), "—")


class PayloadTest(unittest.TestCase):
    def _agg(self):
        agg = svc.Aggregate()
        agg.qoder_accounts = [svc.map_qoder_account({"uid": "q1", "credits": {"remain": 900, "size": 900}})]
        agg.trae_accounts = [svc.map_trae_account({"user_id": "t1", "remaining_credits": 1550, "total_credits": 1550})]
        agg.qoder_usage = {
            "buckets": [{"label": "10/21", "requests": 3, "tokens": 100, "credit": 0.1}],
            "by_model": [{"model": "DeepSeek-Flash", "requests": 3, "tokens": 100, "credit": 0.1}],
        }
        agg.trae_usage = {
            "usage": [{"date": "2026-10-21", "requests": 2, "ok": 2, "errors": 0,
                       "prompt_tokens": 10, "completion_tokens": 20,
                       "models": [{"name": "glm-5.3", "requests": 2, "ok": 2, "errors": 0}]}],
            "credits_daily": [{"date": "2026-10-21", "consumed": 1, "earned": 100, "total": 0}],
        }
        agg.qoder_flow = {"daily": [{"day": "2026-10-21", "income": 100, "expense": 1}]}
        agg.qoder_analytics = {"summary": {"today": {"requests": 5, "errors": 0}}}
        agg.qoder_scheduler = {"enabled": True, "next_run_time": "2026-10-23 21:00"}
        agg.health = []
        return agg

    def test_accounts_payload(self) -> None:
        p = svc.accounts_payload(self._agg())
        self.assertEqual(len(p.qoder), 1)
        self.assertEqual(p.summary["all"].total, 2)

    def test_usage_payload_merges_platforms(self) -> None:
        p = svc.usage_payload(self._agg(), 14)
        self.assertEqual(len(p.points["qoder"]), 1)
        self.assertEqual(len(p.points["trae"]), 1)
        models = {m.model for m in p.by_model}
        self.assertIn("DeepSeek-Flash", models)
        self.assertIn("glm-5.3", models)
        self.assertEqual(len(p.credits), 2)

    def test_overview_stats(self) -> None:
        p = svc.overview_payload(self._agg(), ["healthcheck.timer"], {"targets": {}})
        keys = [s.key for s in p.stats]
        self.assertEqual(keys, ["accounts", "ready", "expiring", "credits", "tokens"])
        self.assertEqual(p.stats[0].value, "2")

    def test_keys_payload(self) -> None:
        p = svc.keys_payload(
            {"api_keys": [{"id": "k0", "name": "k", "masked": "qd-1******2", "enabled": True}]},
            {"data": {"keys": [{"id": "k1", "name": "t", "key": "abcdef", "daily_limit": 100}]}},
        )
        self.assertEqual(len(p.qoder), 1)
        self.assertEqual(len(p.trae), 1)
        self.assertEqual(p.trae[0].masked, "***")
        self.assertEqual(p.trae[0].daily_limit, 100)

    def test_models_payload(self) -> None:
        p = svc.models_payload(
            {"data": [{"id": "Auto", "capabilities": {"vision": True}, "supports_tool_calls": True}]},
            {"data": [{"id": "glm-5.3", "display": "GLM-5.3", "context_length": 128000}]},
        )
        self.assertTrue(p.qoder[0].vision)
        self.assertTrue(p.qoder[0].tool_calls)
        self.assertEqual(p.trae[0].name, "GLM-5.3")


if __name__ == "__main__":
    unittest.main()
