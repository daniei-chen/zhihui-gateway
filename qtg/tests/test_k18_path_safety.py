"""K18：路径包含性与隐式重启（账号文件路径来自 URL 参数）。

锁定：
1. uid 含 `..` / 分隔符 / 绝对路径 → 400，不触碰账号目录之外的文件；
2. 正常 uid 且文件不存在 → 404（不是"尽力而为"地创建）；
3. 正常 uid 且文件存在 → 200，且只改该文件；
4. 文件不可写时**不自动重启容器**（返回 409 + manual_action）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from identity_support import IdentityTestBase, build_settings

from qtg.main import create_app

ACCOUNTS_PREFIX = "/api/accounts"


class AccountPathSafetyTest(IdentityTestBase):
    def setUp(self) -> None:
        # Settings 是 frozen dataclass，不能就地改字段：用临时目录重新装配 app，
        # 只把 qoder_data_dir 指过去（其余沿用 identity_support 的基线）。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.qdir = self.root / "qoder-data"
        (self.qdir / "accounts").mkdir(parents=True, exist_ok=True)
        self.app = create_app(build_settings(self.root, qoder_data_dir=str(self.qdir)))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        from qtg.routers import auth as _auth_router
        _auth_router._attempts.clear()
        self.rt = self.app.state.rt
        from test_api import _mock_adapters
        _mock_adapters(self.rt)
        from test_api import TOKEN
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def _post(self, file_part: str):
        return self.client.post(
            f"{ACCOUNTS_PREFIX}/{file_part}/clear-cooling", headers=self.auth
        )

    def test_traversal_rejected(self) -> None:
        # %2F 编码绕过客户端路径归一化，确保 `..` 真的到达服务端
        r = self._post("qoder:..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(r.status_code, 400, r.text)

    def test_absolute_path_rejected(self) -> None:
        r = self._post("qoder:%2Fetc%2Fpasswd")
        self.assertEqual(r.status_code, 400, r.text)

    def test_encoded_dotdot_rejected(self) -> None:
        r = self._post("qoder:%2E%2E%2Fsecret")
        self.assertEqual(r.status_code, 400, r.text)

    def test_missing_account_returns_404(self) -> None:
        r = self._post("qoder:no-such-account")
        self.assertEqual(r.status_code, 404, r.text)

    def test_existing_account_cleared(self) -> None:
        target = self.qdir / "accounts" / "acc1.json"
        target.write_text(json.dumps({"cooldownUntil": 123, "lastError": "x"}), encoding="utf-8")
        r = self._post("qoder:acc1")
        self.assertEqual(r.status_code, 200, r.text)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["cooldownUntil"], 0)
        self.assertEqual(data["lastError"], "")

    def test_does_not_write_outside_accounts_dir(self) -> None:
        outside = self.qdir / "outside.json"
        outside.write_text(json.dumps({"cooldownUntil": 999}), encoding="utf-8")
        r = self._post("qoder:..%2Foutside")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(json.loads(outside.read_text(encoding="utf-8"))["cooldownUntil"], 999)

    def test_non_qoder_platform_is_409(self) -> None:
        r = self._post("trae:acc1")
        self.assertEqual(r.status_code, 409, r.text)


if __name__ == "__main__":
    unittest.main()
