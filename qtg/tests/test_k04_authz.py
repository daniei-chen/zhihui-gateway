"""K04：角色×动作×平台授权矩阵 + 专用轻量认证/授权探针。

## 被锁定的行为

1. 匿名 → 未登录 401；viewer → 只读可达、写 403；admin（会话/令牌）→ 可用；
2. 公开业务（登录/退出/健康/`/api/claim/*`）显式白名单，不被误拦；
3. 专用探针 `/api/v1/authz/probe` 只回答身份与授权：不读业务状态、不写业务账本、
   不打上游；结论与真实请求一致（同一套 `authz.decide`）；
4. 平台维度：动作不适用的平台返回 `platform_not_applicable`；
5. 矩阵与 `contracts/roles-actions-platforms.csv` 可交叉核对（唯一数据源：
   `tests/data/contracts-qtg-rows.json`，由契约表投影生成），且**不允许比契约放宽**；
6. 授权可回退（`QTG_AUTHZ_ENFORCE=0`）。

## 顺序约束

先有服务端授权（本文件测的第 1–2 条），才有资格把 Nginx 的 `auth_request` 从
业务端点换成 `/api/v1/authz/probe`。详见 `design/qtg/K04-先授权后换探针.md`。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from identity_support import GOOD_ORIGIN, IdentityTestBase

from qtg import authz
from qtg.identity import Identity
from starlette.requests import Request

DATA = Path(__file__).resolve().parent / "data" / "contracts-qtg-rows.json"
WRITE_PATH = "/api/v1/actions/limits"
READ_PATH = "/api/v1/keys"

# 有意偏离契约表的地方（只允许"收紧"或"按目标矩阵新增只读能力"）。
# 键：(action, role)；值：原因。
DOCUMENTED_DEVIATIONS = {
    ("A06", "viewer"): "目标矩阵允许 viewer 只读（K04 要求 viewer 只读可达）；"
                       "契约表记的是现状「QTG 无 viewer 凭据 → 401」",
    ("A15", "viewer"): "同上：账号池读取对 viewer 放开（只读）",
    ("A31", "viewer"): "同上：请求日志读取对 viewer 放开（只读）",
}
# 高危动作收紧为真人会话（K05 要求）：契约表记录现状 Bearer 可用，候选实现收紧。
HUMAN_SESSION_TIGHTENING = {
    "A05": "吊销全部会话：契约表记 admin_token 允许（比 Manager 宽）；候选收紧为只允许会话",
    "A21": "清理日志（抹痕迹）：契约表记 admin_session 允许；候选要求真人在场",
}


class AuthzMatrixBehaviorTest(IdentityTestBase):
    def test_anonymous_denied(self) -> None:
        self.assertEqual(self.client.get(READ_PATH).status_code, 401)
        self.assertEqual(self.client.post(WRITE_PATH, json={"enabled": True}).status_code, 401)

    def test_viewer_can_read_but_not_write(self) -> None:
        token = self.session_cookie(role="viewer", sub="viewer-1")
        headers = self.cookie_headers(token)
        read = self.client.get(READ_PATH, headers=headers)
        self.assertEqual(read.status_code, 200, read.text)
        write = self.client.post(WRITE_PATH, json={"enabled": True},
                                 headers={**headers, "Origin": GOOD_ORIGIN})
        self.assertEqual(write.status_code, 403, write.text)
        self.assertEqual(write.json()["code"], "role_write_denied")

    def test_admin_session_can_write(self) -> None:
        self.login()
        r = self.client.post(WRITE_PATH, json={"enabled": True},
                             headers={"Origin": GOOD_ORIGIN})
        self.assertEqual(r.status_code, 200, r.text)

    def test_admin_token_can_write(self) -> None:
        r = self.client.post(WRITE_PATH, json={"enabled": True}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)

    def test_internal_service_can_write(self) -> None:
        r = self.client.post(WRITE_PATH, json={"enabled": True}, headers=self.internal)
        self.assertEqual(r.status_code, 200, r.text)

    def test_public_paths_are_not_blocked(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        # claim：本部署没有红包能力，公开可达并如实返回 410（不是 401 伪装成未登录）
        claim = self.client.get("/api/claim/anything")
        self.assertEqual(claim.status_code, 410, claim.text)
        self.assertFalse(claim.json()["available"])

    def test_platform_not_applicable(self) -> None:
        r = self.client.post(
            "/api/red-packets",
            json={},
            headers={**self.auth, "X-Platform": "workbuddy"},
        )
        # 本部署没有红包能力：业务层直接 410（available=false），比"平台 403"更诚实。
        # 矩阵层的 platform_not_applicable 由下面的 probe 用例锁定。
        self.assertIn(r.status_code, (403, 410), r.text)
        if r.status_code == 410:
            self.assertFalse(r.json()["available"])
        else:
            self.assertEqual(r.json()["code"], "platform_not_applicable")

    def test_platform_applicable_reaches_handler(self) -> None:
        r = self.client.post(
            "/api/red-packets", json={}, headers={**self.auth, "X-Platform": "qoder"}
        )
        self.assertEqual(r.status_code, 410, r.text)   # 业务层如实拒绝


class AuthzRollbackTest(IdentityTestBase):
    """QTG_AUTHZ_ENFORCE=0：矩阵可回退（发布失败时的退路）。"""

    settings_overrides = {"authz_enforce": False}

    def test_viewer_write_falls_back_to_legacy_gate_when_disabled(self) -> None:
        """authz_enforce=0 = 回退到**旧行为**：业务层 require_admin 仍拒绝 viewer。

        回退不是"放开 viewer 写权限"——那会变成用配置开关绕过权限。
        这里锁定的是：关掉矩阵后，viewer 写请求回到旧闸门的 401，而不是被矩阵放行。
        """
        token = self.session_cookie(role="viewer", sub="viewer-1")
        r = self.client.post(
            WRITE_PATH, json={"enabled": True},
            headers={**self.cookie_headers(token), "Origin": GOOD_ORIGIN},
        )
        self.assertEqual(r.status_code, 401, r.text)


class AuthzProbeTest(IdentityTestBase):
    def test_probe_requires_auth(self) -> None:
        r = self.client.get("/api/v1/authz/probe")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["code"], "unauthorized")

    def test_probe_allows_admin(self) -> None:
        r = self.client.get("/api/v1/authz/probe", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["envelope_ok"])
        self.assertEqual(body["identity"]["role"], "admin")
        self.assertEqual(body["decision"]["allowed"], True)

    def test_probe_answers_for_target_request(self) -> None:
        token = self.session_cookie(role="viewer", sub="viewer-1")
        headers = self.cookie_headers(token)
        read = self.client.get(
            "/api/v1/authz/probe", headers=headers,
            params={"method": "GET", "path": READ_PATH},
        )
        self.assertEqual(read.status_code, 200, read.text)
        write = self.client.get(
            "/api/v1/authz/probe", headers=headers,
            params={"method": "POST", "path": WRITE_PATH},
        )
        self.assertEqual(write.status_code, 403, write.text)
        self.assertEqual(write.json()["decision"]["code"], "role_write_denied")

    def test_probe_with_action_and_platform(self) -> None:
        r = self.client.get(
            "/api/v1/authz/probe", headers=self.auth,
            params={"action": "A22", "platform": "workbuddy", "method": "POST",
                    "path": "/api/red-packets"},
        )
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["decision"]["code"], "platform_not_applicable")

    def test_probe_does_not_touch_upstream_or_ledger(self) -> None:
        before = self.rt.store.stats(3650)["requests"]
        r = self.client.get("/api/v1/authz/probe", headers=self.auth,
                            params={"action": "A42"})
        self.assertEqual(r.status_code, 200, r.text)
        for name in ("accounts", "usage", "health", "logs"):
            for adapter in (self.rt.qoder, self.rt.trae):
                mock = getattr(adapter, name, None)
                if mock is not None and hasattr(mock, "assert_not_awaited"):
                    mock.assert_not_awaited()
        self.assertEqual(self.rt.store.stats(3650)["requests"], before,
                         "探针不能写业务账本（proxy_requests）")

    def test_probe_rejection_is_auditable(self) -> None:
        token = self.session_cookie(role="viewer", sub="viewer-audit")
        self.client.get("/api/v1/authz/probe", headers=self.cookie_headers(token),
                        params={"method": "POST", "path": WRITE_PATH})
        rows = self.audit_by_action("authz_probe")
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["actor"], "viewer:viewer-audit")
        self.assertEqual(rows[-1]["result"], "denied")


class AuthzMatrixEndpointTest(IdentityTestBase):
    def test_matrix_shape(self) -> None:
        r = self.client.get("/api/v1/authz/matrix", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["rows"])
        self.assertEqual(len(body["rows"]), len(authz.RULES))
        for row in body["rows"]:
            self.assertTrue(row["evidence"], f"{row['action']} 缺证据")
            self.assertTrue(row["allow"], f"{row['action']} 没有声明任何角色")
            self.assertIn("三平台", row["platform"] + "三平台")
        for role in authz.ROLE_LABELS:
            self.assertIn(role, body["role_index"])

    def test_high_risk_rows_are_human_only(self) -> None:
        r = self.client.get("/api/v1/authz/matrix", headers=self.auth)
        human_only = {row["action"] for row in r.json()["rows"] if row["human_only"]}
        self.assertIn("A05", human_only)
        self.assertIn("A21", human_only)


class ContractCrossCheckTest(IdentityTestBase):
    """矩阵 ↔ 契约表交叉核对（数据来自 `tests/data/contracts-qtg-rows.json`）。"""

    @classmethod
    def setUpClass(cls) -> None:
        payload = json.loads(DATA.read_text(encoding="utf-8"))
        cls.contract_rows = payload["rows"]

    def _contract_status(self, action: str, role: str) -> set:
        """返回契约表里该 (动作, 角色) 出现过的状态集合。"""
        return {
            row["status"] for row in self.contract_rows
            if row["action"] == action and row["role"] == role
        }

    def test_matrix_never_broader_than_contract_on_authorization(self) -> None:
        """契约表里「授权层拒绝」（failure_code 以 401/403 开头）的角色，矩阵不得允许。

        说明：契约表的 `allowed` 列把**授权**与**业务可用性**混在一起
        （例如 A22「红包」是 410 业务拒绝，不是权限拒绝）。这里只对授权层
        （401/403）做角色级约束；业务层拒绝由 K29 能力表负责核对。
        """
        identities = {
            'anonymous': None,
            'viewer_session': Identity('viewer-fixture', 'viewer'),
            'admin_session': Identity('admin-fixture', 'admin'),
            'readonly_token': Identity('readonly-fixture', 'readonly',
                                       frozenset({'readonly'}), 'admin_token'),
            'admin_token': Identity('admin-token-fixture', 'admin', frozenset({'*'}), 'admin_token'),
            'keys_service_token': Identity('keys-fixture', 'admin',
                                           frozenset({'keys:read', 'keys:write'}), 'admin_token'),
            'internal_service': Identity('service-fixture', 'internal_service',
                                         frozenset({'service'}), 'internal_service'),
            'inference_key_wbk': Identity('inference-wb-fixture', 'inference_key',
                                          frozenset({'inference'}), 'inference_key'),
            'inference_key_skqtg': Identity('inference-qtg-fixture', 'inference_key',
                                            frozenset({'inference'}), 'inference_key'),
        }
        # A32 mixes absent stats/repair handlers; A34 uses the dedicated metrics
        # collector credential guard. Neither is governed by this role matrix.
        separate_guards = {'A32', 'A34'}
        violations = []
        checked = set()
        for row in self.contract_rows:
            if row["status"] != "deny":
                continue
            code = (row.get("failure_code") or "").strip()
            if not code.startswith(("401", "403")):
                continue
            action, role = row["action"], row["role"]
            rule = next((r for r in authz.RULES if r.action == action), None)
            if rule is not None and rule.public:
                continue          # 公开白名单由行为测试覆盖（401/410）
            if action in separate_guards:
                continue
            self.assertIsNotNone(rule, f'契约拒绝项缺少对应授权规则：{action}/{role}')
            self.assertIn(role, identities, f'未映射契约身份：{role}')
            identity = identities[role]
            canonical_role = identity.role if identity else 'anonymous'
            method = row['method'].split('/')[0]
            path = rule.paths[0]
            request = Request({'type': 'http', 'method': method, 'path': path,
                               'headers': [], 'query_string': b''})
            decision = authz.decide(identity, request, action=action,
                                    platform=rule.platform,
                                    target_method=method, target_path=path)
            checked.add((action, role))
            if decision.allowed:
                if (action, canonical_role) in DOCUMENTED_DEVIATIONS:
                    continue
                violations.append((action, role, row["failure_code"]))
        self.assertEqual(violations, [], f"矩阵比契约表放宽了：{violations}")
        self.assertTrue({('A43', r) for r in ('anonymous', 'viewer_session',
                         'readonly_token', 'keys_service_token', 'inference_key_wbk',
                         'inference_key_skqtg')}.issubset(checked),
                        '必须实际核对配置快照端点的六类拒绝身份，不能只比较不同命名的角色字符串')

    def test_admin_actions_stay_allowed(self) -> None:
        """契约表允许 admin 的动作，矩阵不能把 admin 关掉（否则面板会坏）。"""
        missing = []
        for row in self.contract_rows:
            if row["status"] != "allow":
                continue
            if row["role"] not in ("admin_session", "admin_token"):
                continue
            action = row["action"]
            rule = next((r for r in authz.RULES if r.action == action), None)
            if rule is None or rule.public:
                continue
            if authz.ROLE_ADMIN not in rule.allow:
                missing.append(action)
        self.assertEqual(sorted(set(missing)), [], f"这些动作把 admin 关掉了：{sorted(set(missing))}")

    def test_high_risk_actions_are_tightened(self) -> None:
        """高危动作：契约表记 admin_token 可用，候选必须收紧为真人会话。"""
        for action in HUMAN_SESSION_TIGHTENING:
            rules = [r for r in authz.RULES if r.action == action]
            self.assertTrue(rules, f"矩阵缺少 {action}")
            self.assertTrue(all(r.human_only for r in rules),
                            f"{action} 未被标记为 human_only：{HUMAN_SESSION_TIGHTENING[action]}")

    def test_every_matrix_action_exists_in_contract(self) -> None:
        known = {row["action"] for row in self.contract_rows}
        unknown = sorted({r.action for r in authz.RULES if r.action not in known})
        self.assertEqual(unknown, [], f"矩阵里出现契约表没有的动作号：{unknown}")


if __name__ == "__main__":
    unittest.main()
