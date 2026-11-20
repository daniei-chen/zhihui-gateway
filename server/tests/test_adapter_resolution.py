"""`_adapter_for` 必须覆盖每一个在用的平台。

2026-11-20 真机事故：`compat._adapter_for` 有 qoder / trae / workbuddy 三个分支，
**没有 doubaowork**，直接落到 `return None`。后果是豆包账号页点「停用/启用」返回

    {"success": false, "message": "doubaowork 平台未启用（QTG_DOUBAOWORK_ENGINE=off…）"}

而豆包上游明明在跑（`/health` 正常、账号能列出、模型能取到 8 个）。用户会照着
这句话去查 `QTG_DOUBAOWORK_ENGINE`，排查方向直接错。

这类「少一个分支」的错误不会让任何测试或类型检查失败 —— 它只在真机点那个按钮时
才暴露。所以这里用 `platformpolicy.PLATFORMS` 作为权威清单，逐个断言。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from server import platformpolicy
from server.qtgcore.routers import compat


class AdapterResolutionTests(unittest.TestCase):
    def _rt(self):
        """每个平台的适配器都给一个哨兵对象，缺分支时断言能看出来。"""
        return SimpleNamespace(
            qoder=object(), trae=object(), wb=object(), dbw=object(),
        )

    def test_every_platform_resolves_to_its_adapter(self):
        rt = self._rt()
        missing = []
        for plat in platformpolicy.PLATFORMS:
            adapter = compat._adapter_for(rt, plat)
            if adapter is None:
                missing.append(plat)
        self.assertEqual([], missing,
                         '这些平台在 _adapter_for 里没有分支，会被判成「平台未启用」：'
                         + ', '.join(missing))

    def test_doubaowork_maps_to_dbw_attribute(self):
        """豆包挂在 `rt.dbw` 上 —— 写错属性名同样是静默失败。"""
        sentinel = object()
        rt = SimpleNamespace(doubaowork=None, dbw=sentinel)
        self.assertIs(sentinel, compat._adapter_for(rt, 'doubaowork'))

    def test_unknown_platform_returns_none(self):
        self.assertIsNone(compat._adapter_for(self._rt(), 'not-a-platform'))

    def test_missing_attribute_returns_none_not_crash(self):
        """引擎没挂上时属性可能不存在，不能因此 500。"""
        rt = SimpleNamespace()
        for plat in platformpolicy.PLATFORMS:
            self.assertIsNone(compat._adapter_for(rt, plat))


class DisabledEndpointContractTests(unittest.TestCase):
    """`PUT /accounts/{file}/disabled` 的返回体必须含前端读的 `ok`。

    前端 `lib/api.ts` 的 setDisabled 类型是
    `{ok, file, disabled, changed, ...}`，后端原来只给 `success` ——
    前端拿不到 `ok` 就当成失败，且**不显示原因**。
    """

    def test_disabled_response_shape_documented(self):
        import inspect
        src = inspect.getsource(compat.compat_account_disabled)
        for field in ('"ok"', '"file"', '"disabled"', '"changed"'):
            self.assertIn(field, src,
                          'compat_account_disabled 的返回体缺少 %s 字段' % field)


if __name__ == '__main__':
    unittest.main()
