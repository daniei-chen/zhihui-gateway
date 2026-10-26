"""K29：能力表、部分失败语义、三层成功判定。

## 被锁定的行为

1. 能力表把每条能力标成 `real / readonly / unsupported / degraded`，且
   `unsupported`/`degraded` 必须带 `error` + `evidence`；
2. 「不支持」必须在**服务端**拒绝（409/410），不能只在前端隐藏；
3. 部分失败必须带 来源 / 时间 / 错误 三要素（`partial_report`）；
4. 状态码、信封、业务成功是三层，不能混：`200 + ok:false` 与 `200 + partial:true`
   都不是成功；`409/503` 分支不能自动推断"永久桩"。

最后一条尤其重要：`POST /api/keys/check-models` 旧实现返回 `{checked:true}` 属
**假成功**（方法体自己写着"qtg 不做模型白名单校验"）。本文件锁定修复后的形状。
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from identity_support import IdentityTestBase

from qtg import capabilities as caps
from qtg.capabilities import CAPABILITIES, capability_index, classify, partial_report, source_status


class ClassifyTest(unittest.TestCase):
    """三层判定的纯逻辑边界（不依赖 HTTP）。"""

    def test_plain_success(self) -> None:
        v = classify(200, {"ok": True, "data": []})
        self.assertTrue(v.success)
        self.assertEqual((v.http_ok, v.envelope_ok, v.business_ok), (True, True, True))

    def test_http_200_with_ok_false_is_not_success(self) -> None:
        """`/api/login` 密码错误的形状：HTTP 成功、信封失败。"""
        v = classify(200, {"ok": False, "username": "", "role": ""})
        self.assertTrue(v.http_ok)
        self.assertFalse(v.envelope_ok)
        self.assertFalse(v.success)
        self.assertIn("ok=false", v.reason)

    def test_partial_is_not_success(self) -> None:
        body = partial_report({"accounts": []}, [
            source_status("qoder", True, detail={"n": 1}),
            source_status("trae", False, error="upstream_timeout"),
        ])
        self.assertTrue(body["partial"])
        self.assertFalse(body["ok"])
        v = classify(200, body)
        self.assertTrue(v.http_ok)
        self.assertFalse(v.business_ok)
        self.assertFalse(v.success)

    def test_available_false_is_not_success(self) -> None:
        v = classify(501, {"available": False, "code": "not_implemented", "detail": "x"})
        self.assertFalse(v.http_ok)
        self.assertFalse(v.envelope_ok)
        self.assertFalse(v.success)

    def test_upstream_error_envelope(self) -> None:
        v = classify(502, {"error": {"code": "upstream_error", "message": "down"}})
        self.assertFalse(v.envelope_ok)
        self.assertFalse(v.success)

    def test_409_is_rejection_not_stub_proof(self) -> None:
        """409 只代表这次业务拒绝；能力状态不能被它"自动"推断成永久桩。"""
        v = classify(409, {"detail": "本部署不支持多用户管理"})
        self.assertFalse(v.http_ok)
        self.assertFalse(v.success)
        # 提示函数会给出 degraded，但真正的能力状态必须来自人工核对表
        self.assertEqual(caps.capability_state_from_status(409), caps.STATE_DEGRADED)
        self.assertEqual(capability_index()["users.multi"].state, caps.STATE_UNSUPPORTED)

    def test_text_body_has_no_envelope(self) -> None:
        v = classify(200, "# HELP qtg_requests_total ...")
        self.assertTrue(v.success)

    def test_non_2xx_never_success(self) -> None:
        for code in (301, 400, 401, 403, 404, 500, 503):
            with self.subTest(code=code):
                self.assertFalse(classify(code, {"ok": True}).success)


class PartialReportTest(unittest.TestCase):
    def test_all_ok(self) -> None:
        rep = partial_report(None, [source_status("a", True), source_status("b", True)])
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["partial"])
        self.assertEqual(rep["sources_total"], 2)

    def test_all_failed(self) -> None:
        rep = partial_report(None, [
            source_status("a", False, error="boom"),
            source_status("b", False, error="timeout"),
        ])
        self.assertFalse(rep["ok"])
        self.assertFalse(rep["partial"])
        self.assertEqual(rep["sources_ok"], 0)

    def test_source_status_has_three_elements(self) -> None:
        src = source_status("trae", False, error="502 bad gateway", duration_ms=12.5)
        for key in ("source", "at", "error"):
            self.assertIn(key, src)
        self.assertEqual(src["source"], "trae")
        self.assertIn("error", src)
        self.assertTrue(src["error"])
        self.assertTrue(src["at"])


class CapabilityTableTest(IdentityTestBase):
    def test_table_shape(self) -> None:
        r = self.client.get("/api/v1/capabilities", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["capabilities"])
        self.assertEqual(sum(body["counts"].values()), len(body["capabilities"]))
        for cap in body["capabilities"]:
            self.assertIn(cap["state"], caps.STATE_LABELS)
            self.assertTrue(cap["evidence"], f"{cap['id']} 缺 evidence")
            self.assertTrue(cap["endpoints"], f"{cap['id']} 没有端点")
            if cap["state"] in (caps.STATE_UNSUPPORTED, caps.STATE_DEGRADED):
                self.assertTrue(cap["error"], f"{cap['id']} 是 {cap['state']} 却没写 error")

    def test_unsupported_rows_are_locked(self) -> None:
        for cap in CAPABILITIES:
            if cap.state == caps.STATE_UNSUPPORTED:
                self.assertTrue(cap.locked, f"{cap.id} 标了不支持但没有服务端拒绝")

    def test_state_filter(self) -> None:
        r = self.client.get("/api/v1/capabilities", headers=self.auth,
                           params={"state": caps.STATE_DEGRADED})
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["capabilities"]
        self.assertTrue(rows)
        self.assertTrue(all(row["state"] == caps.STATE_DEGRADED for row in rows))

    def test_detail_and_404(self) -> None:
        ok = self.client.get("/api/v1/capabilities/keys.check_models", headers=self.auth)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["capability"]["state"], caps.STATE_DEGRADED)
        missing = self.client.get("/api/v1/capabilities/nope.nope", headers=self.auth)
        self.assertEqual(missing.status_code, 404)

    def test_live_partial_failure(self) -> None:
        """`?live=1` 时上游部分失败要带 来源/时间/错误，且 ok=false + partial=true。"""
        self.rt.wb.health = AsyncMock(return_value={"up": False, "last_error": "未配置"})
        r = self.client.get("/api/v1/capabilities", headers=self.auth, params={"live": 1})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["partial"])
        self.assertFalse(body["ok"])
        self.assertEqual(len(body["sources"]), 3)
        failed = [s for s in body["sources"] if not s["ok"]]
        self.assertTrue(failed)
        for src in body["sources"]:
            self.assertTrue(src["at"])
            if not src["ok"]:
                self.assertTrue(src["error"])
        self.rt.qoder.health.assert_awaited()


class RealEndpointEnvelopeTest(IdentityTestBase):
    """用真实端点验证"三层"确实不同（不是纸面约定）。"""

    def test_login_failure_is_200_but_not_success(self) -> None:
        r = self.client.post("/api/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(r.status_code, 200)
        v = classify(r.status_code, r.json())
        self.assertTrue(v.http_ok)
        self.assertFalse(v.envelope_ok)
        self.assertFalse(v.success)

    def test_check_models_no_longer_fakes_success(self) -> None:
        r = self.client.post("/api/keys/check-models", json={"models": ["m1"]},
                             headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["checked"], "不做校验不能回 checked=true（假成功）")
        self.assertFalse(body["available"])
        self.assertTrue(body["reason"])
        v = classify(r.status_code, body)
        self.assertFalse(v.success)

    def test_unimplemented_endpoint_is_501_and_unavailable(self) -> None:
        r = self.client.get("/api/not-a-real-endpoint", headers=self.auth)
        self.assertEqual(r.status_code, 501, r.text)
        body = r.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["code"], "not_implemented")
        self.assertFalse(classify(r.status_code, body).success)

    def test_v1_unknown_is_404(self) -> None:
        r = self.client.get("/api/v1/definitely-missing", headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)


class UnsupportedIsServerEnforcedTest(IdentityTestBase):
    """「不支持」必须由服务端拒绝，而不是只在前端隐藏。"""

    CASES = (
        ("post", "/api/keys/import-local", 409),
        ("post", "/api/users", 409),
        ("post", "/api/tokens", 409),
        ("post", "/api/proxy-routes", 409),
        ("post", "/api/system/update", 409),
        ("post", "/api/red-packets", 410),
    )

    def test_endpoints_reject(self) -> None:
        for method, path, expected in self.CASES:
            with self.subTest(path=path):
                r = getattr(self.client, method)(path, headers=self.auth, json={})
                self.assertEqual(r.status_code, expected, f"{path}: {r.status_code} {r.text}")
                body = r.json()
                # 拒绝要可解释：至少给 detail 或 error，不能空体
                self.assertTrue(body.get("detail") or body.get("error"), body)

    def test_red_packet_carries_available_false(self) -> None:
        r = self.client.post("/api/red-packets", headers=self.auth, json={})
        self.assertEqual(r.status_code, 410)
        body = r.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["code"], "unsupported")


if __name__ == "__main__":
    unittest.main()
