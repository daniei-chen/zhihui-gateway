"""签到记录的 realm 参数必须在**所有平台**生效。

2026-11-20 真机事故：`/api/checkin-logs` 的 realm 校验与过滤只写在
`plat == "qoder"` 的分支里。Trae 走的是另一条分支，那条分支完全不看 realm：

    realm=（空）/ cn / global / bogus / xyz  →  一律返回同样的 18 条

两个后果：
  · 切到国际版后仍显示国内版的签到记录，用户以为国际版也有历史；
  · 非法 realm 静默通过（本该 422）。

Trae 的签到行是上游的**按天汇总**（没有 uid），无法逐条判断版本归属，
所以只能按「这个版本有没有账号」来决定：该版本没有账号时返回空列表。
"""
from __future__ import annotations

import inspect
import unittest

from server.qtgcore.routers import compat


class CheckinLogsRealmTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(compat.compat_checkin_logs)

    def test_realm_validation_is_not_inside_the_qoder_branch(self):
        """校验必须在函数级，不能只写在 qoder 分支里。"""
        idx_validate = self.src.find("if realm not in ('', 'cn', 'global')")
        idx_qoder = self.src.find('if plat == "qoder" and callable')
        self.assertGreater(idx_validate, -1, '缺少 realm 校验')
        self.assertGreater(idx_qoder, -1, '找不到 qoder 分支')
        self.assertLess(idx_validate, idx_qoder,
                        'realm 校验必须在 qoder 分支之前（所有平台共用）')

    def test_trae_branch_consults_realm(self):
        """Trae 的汇总行没有 uid，但分支仍必须看 realm 决定给不给数据。"""
        # 取 else 分支（Trae / 其它）那一段
        idx_else = self.src.rfind('    else:\n')
        self.assertGreater(idx_else, -1)
        branch = self.src[idx_else:]
        self.assertIn('_has_realm', branch,
                      'Trae 分支没有按 realm 决定是否返回记录')

    def test_invalid_realm_raises_422(self):
        self.assertIn("status_code=422", self.src)
        self.assertIn('Invalid history realm', self.src)

    def test_empty_realm_still_returns_everything(self):
        """`realm=''`（前端未指定）必须保持原行为：不过滤。"""
        self.assertIn("if realm else True", self.src,
                      '空 realm 时不应过滤，否则未传 realm 的调用方会拿不到数据')


if __name__ == '__main__':
    unittest.main()
