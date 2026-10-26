"""核心基础设施单测：配置 / 缓存 / 审计 / 会话。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from qtg import config as cfg
from qtg.audit import AuditLog
from qtg.cache import TTLCache
from qtg.security import CredentialStore


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._path_env = {k: v for k, v in os.environ.items() if k.startswith('QTG_')}

    def tearDown(self) -> None:
        for k in list(os.environ):
            if k.startswith("QTG_"):
                os.environ.pop(k, None)
        os.environ.update(self._path_env)

    def test_defaults(self) -> None:
        s = cfg.load_settings()
        self.assertEqual(s.port, 8795)
        self.assertEqual(s.qoder_base, "http://127.0.0.1:8791")
        self.assertFalse(s.docs_enabled)
        self.assertFalse(s.read_requires_auth)

    def test_env_override_and_bool_parsing(self) -> None:
        os.environ["QTG_PORT"] = "9911"
        os.environ["QTG_DOCS_ENABLED"] = "yes"
        os.environ["QTG_READ_REQUIRES_AUTH"] = "0"
        s = cfg.load_settings()
        self.assertEqual(s.port, 9911)
        self.assertTrue(s.docs_enabled)
        self.assertFalse(s.read_requires_auth)

    def test_invalid_values_rejected(self) -> None:
        os.environ["QTG_PORT"] = "70000"
        with self.assertRaises(ValueError):
            cfg.load_settings()
        os.environ["QTG_PORT"] = "8795"
        os.environ["QTG_UPSTREAM_RETRIES"] = "99"
        with self.assertRaises(ValueError):
            cfg.load_settings()

    def test_relative_path_rejected(self) -> None:
        os.environ["QTG_DATA_DIR"] = "relative/dir"
        with self.assertRaises(ValueError):
            cfg.load_settings()

    def test_derived_paths(self) -> None:
        s = cfg.load_settings()
        self.assertTrue(str(s.audit_path).endswith("audit.jsonl"))
        self.assertTrue(str(s.state_path).endswith("state.json"))


class CacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_ttl_and_hit(self) -> None:
        c = TTLCache()
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return calls["n"]

        v1 = await c.get_or_set("k", 60, factory)
        v2 = await c.get_or_set("k", 60, factory)
        self.assertEqual(v1, v2)
        self.assertEqual(calls["n"], 1)

    async def test_expiry(self) -> None:
        c = TTLCache()
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return calls["n"]

        await c.get_or_set("k", 0.01, factory)
        await self._sleep(0.05)
        v = await c.get_or_set("k", 0.01, factory)
        self.assertEqual(v, 2)

    async def test_force_refresh(self) -> None:
        c = TTLCache()
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return calls["n"]

        await c.get_or_set("k", 60, factory)
        v = await c.get_or_set("k", 60, factory, force=True)
        self.assertEqual(v, 2)

    async def test_single_flight(self) -> None:
        import asyncio

        c = TTLCache()
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return calls["n"]

        results = await asyncio.gather(*[c.get_or_set("k", 60, factory) for _ in range(8)])
        self.assertEqual(set(results), {1})
        self.assertEqual(calls["n"], 1)
        self.assertGreaterEqual(c.stats()["coalesced"], 1)

    async def test_invalidate(self) -> None:
        c = TTLCache()
        await c.get_or_set("a", 60, _ret(1))
        await c.get_or_set("b", 60, _ret(2))
        self.assertEqual(c.invalidate("a"), 1)
        self.assertEqual(c.invalidate(), 1)

    @staticmethod
    async def _sleep(s: float) -> None:
        import asyncio

        await asyncio.sleep(s)


def _ret(v):
    async def _f():
        return v

    return _f


class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "audit.jsonl"
        self.log = AuditLog(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_and_verify(self) -> None:
        self.log.append("backup", target="trae")
        self.log.append("keys", target="qoder", detail={"keys": [{"key": "SECRET"}]})
        self.assertEqual(self.log.count(), 2)
        rep = self.log.verify()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["records"], 2)

    def test_redaction(self) -> None:
        rec = self.log.append("login", detail={"password": "p", "token": "t", "nested": {"api_key": "k"}})
        self.assertEqual(rec["detail"]["password"], "***")
        self.assertEqual(rec["detail"]["token"], "***")
        self.assertEqual(rec["detail"]["nested"]["api_key"], "***")

    def test_tamper_detected(self) -> None:
        self.log.append("a")
        self.log.append("b")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[0])
        rec["action"] = "tampered"
        lines[0] = json.dumps(rec, ensure_ascii=False)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rep = AuditLog(self.path).verify()
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["broken_at"], 1)

    def test_tail(self) -> None:
        for i in range(5):
            self.log.append(f"a{i}")
        rows = self.log.tail(2)
        self.assertEqual([r["action"] for r in rows], ["a3", "a4"])

    def test_mode_0600(self) -> None:
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class SecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "admin_token"
        self.secret_file = Path(self.tmp.name) / "session_secret"
        self.token_file.write_text("s3cr3t-token", encoding="utf-8")
        self.store = CredentialStore(self.token_file, self.secret_file)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_password(self) -> None:
        self.assertTrue(self.store.verify_password("s3cr3t-token"))
        self.assertFalse(self.store.verify_password("wrong"))

    def test_session_roundtrip(self) -> None:
        tok = self.store.issue_session(600)
        payload = self.store.verify_session(tok)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], "admin")

    def test_session_expired(self) -> None:
        tok = self.store.issue_session(10, now=time.time() - 3600)
        self.assertIsNone(self.store.verify_session(tok))

    def test_session_tampered(self) -> None:
        tok = self.store.issue_session(600)
        body, sig = tok.rsplit(".", 1)
        self.assertIsNone(self.store.verify_session(body + "." + ("a" * len(sig))))
        self.assertIsNone(self.store.verify_session("garbage"))

    def test_secret_autogenerated(self) -> None:
        self.assertFalse(self.secret_file.exists())
        self.store.issue_session(60)
        self.assertTrue(self.secret_file.exists())
        self.assertEqual(self.secret_file.stat().st_mode & 0o777, 0o600)

    def test_missing_token_raises(self) -> None:
        from qtg.errors import Unauthorized

        empty = Path(self.tmp.name) / "nope"
        store = CredentialStore(empty, self.secret_file)
        with self.assertRaises(Unauthorized):
            store.token()


if __name__ == "__main__":
    unittest.main()
