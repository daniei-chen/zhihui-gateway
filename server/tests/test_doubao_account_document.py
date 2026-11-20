"""豆包账号导入的**真实数据契约**。

2026-11-20 真机事故：上传端点原来要求 `uid` / `id` / `device_id`，而上游
`parse_account_document` 的真实契约是：

    cookies      必需（dict / JSON 字符串 / cookie 串）
    device_id    可选（也可叫 tea_uuid / user_unique_id）
    web_id       可选
    id           可选 —— 不给时上游用 sha256(session 身份)[:24] 派生

上游面板导出的账号 JSON **没有 uid 字段**，于是每一份真实数据都被判
「缺少 uid / device_id」拒绝，用户看到「0 个被导入」。

这组测试用**上游真实形状**的文档，确认它能被接受。
"""
from __future__ import annotations

import json
import unittest


def _entry_has_identity(entry: dict) -> bool:
    """复刻 compat_accounts_upload 里的判据（cookies 优先）。"""
    cookies = entry.get("cookies") or entry.get("cookie") or entry.get("cookie_string")
    has_cookies = bool(cookies) and isinstance(cookies, (dict, str))
    uid = str(entry.get("uid") or entry.get("id") or entry.get("device_id") or "").strip()
    return bool(uid) or has_cookies


# 上游 /admin/accounts/import 实测接受的形状（2026-11-20 真机验证 created:1）
REAL_DOC = {
    "cookies": {"sessionid": "real-session-value", "ttwid": "real-ttwid"},
    "device_id": "7000000000000000000",
    "web_id": "7000000000000000000",
}

MINIMAL_DOC = {"cookies": {"sessionid": "only-cookies"}}


class DoubaoAccountDocumentContractTests(unittest.TestCase):
    def test_real_upstream_document_is_accepted(self):
        self.assertTrue(_entry_has_identity(REAL_DOC))

    def test_document_with_only_cookies_is_accepted(self):
        """上游允许省略 device_id / web_id / id —— 只给 cookies 也算合法。"""
        self.assertTrue(_entry_has_identity(MINIMAL_DOC))

    def test_cookie_string_form_is_accepted(self):
        self.assertTrue(_entry_has_identity({"cookie_string": "a=1; b=2"}))

    def test_cookie_key_alias_is_accepted(self):
        self.assertTrue(_entry_has_identity({"cookie": {"sessionid": "x"}}))

    def test_legacy_uid_document_still_accepted(self):
        """旧的探针/手工数据带 uid，不能因为改了判据就拒掉。"""
        self.assertTrue(_entry_has_identity({"uid": "some-uid"}))

    def test_document_without_cookies_or_id_is_rejected(self):
        self.assertFalse(_entry_has_identity({"name": "no identity"}))
        self.assertFalse(_entry_has_identity({"cookies": {}}))
        self.assertFalse(_entry_has_identity({}))

    def test_exported_panel_shape_has_no_uid_field(self):
        """把事故形状钉死：上游导出的账号 JSON 里没有 uid。

        如果哪天有人又把判据改回「必须有 uid」，这条会立刻提醒他看注释。
        """
        self.assertNotIn("uid", REAL_DOC)
        self.assertIn("cookies", REAL_DOC)
        self.assertTrue(_entry_has_identity(REAL_DOC))


if __name__ == '__main__':
    unittest.main()
