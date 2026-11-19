"""平台编号一致性的回归测试（2026-11-19 修豆包接入后新增）。

## 守的是什么

`fixed_upstreams` 与 `UPSTREAM_IDS` 曾经各写一份平台编号：

    fixed_upstreams:  row(1,qoder) row(2,trae) row(3,workbuddy) row(4,doubaowork)
    UPSTREAM_IDS:     {1:doubaowork, 2:trae, 3:workbuddy}

两份打架的直接后果：前端拿到 id=4 的豆包分组，按 `upstream_id=4` 去取账号，
而 `_rows_of` 里**没有 4 这个分支**，落到 `all_accounts(agg)` ——
豆包页面显示的是「三个平台的账号混在一起」。用户看到的就是「豆包接入不好」。

这类 bug 不会抛异常、不会 500，只会**安静地显示错的数据**，所以必须靠断言守。
"""
from __future__ import annotations

import unittest

from server.qtgcore.routers import compat_fixes as cf


class UpstreamIdConsistencyTests(unittest.TestCase):
    """编号只有一处来源，且三处视图必须一致。"""

    def test_upstream_ids_matches_upid(self):
        """`UPSTREAM_IDS`（id→name）与 `_UPID`（name→id）必须互逆。"""
        self.assertEqual({v: k for k, v in cf._UPID.items()}, cf.UPSTREAM_IDS)

    def test_upstream_names_matches(self):
        self.assertEqual(dict(cf._UPNAME), cf.UPSTREAM_NAMES)

    def test_every_platform_has_an_id(self):
        for p in cf.PLATFORMS:
            self.assertIn(p, cf._UPID, f'{p} 没有 upstream_id')
            self.assertIn(cf._UPID[p], cf.UPSTREAM_IDS, f'{p} 的 id 不在 UPSTREAM_IDS 里')

    def test_ids_are_dense_from_one(self):
        """编号必须是从 1 开始的连续整数，不留空洞。

        留空洞会让前端逐组扇出时多一组永远空的（「1 号平台」这个位置缺一个）。
        """
        ids = sorted(cf._UPID.values())
        self.assertEqual(ids, list(range(1, len(cf._UPID) + 1)))

    def test_doubaowork_holds_slot_one(self):
        """豆包顶替已摘除的 qoder 占 1 号 —— 这是对外契约，不能悄悄变。"""
        self.assertEqual(cf._UPID['doubaowork'], 1)

    def test_no_qoder_in_numbering(self):
        self.assertNotIn('qoder', cf._UPID)
        self.assertNotIn('qoder', cf.UPSTREAM_IDS.values())

    def test_rows_of_covers_every_id(self):
        """`_rows_of` 必须对每个已分配的 id 都有分支。

        少一个分支就会落到 `all_accounts(agg)`，把三个平台的账号混在一起 ——
        这正是本次修的 bug。
        """

        class _Agg:
            qoder_accounts: list = []
            trae_accounts: list = ['trae-acc']
            wb_accounts: list = ['wb-acc']
            doubaowork_accounts: list = ['dbw-acc']

        # 聚合对象上的属性名与平台 id 不同名（workbuddy → wb_accounts），
        # 这里显式映射一次，避免测试自己按名字猜。
        _ATTR = {'workbuddy': 'wb_accounts', 'trae': 'trae_accounts',
                 'doubaowork': 'doubaowork_accounts'}
        agg = _Agg()
        for plat, uid in cf._UPID.items():
            rows = cf._rows_of(agg, uid)
            expected = getattr(agg, _ATTR[plat])
            self.assertEqual(rows, list(expected),
                             f'{plat}(id={uid}) 取到了别的平台的账号: {rows}')

    def test_rows_of_none_returns_all(self):
        class _Agg:
            qoder_accounts: list = []
            trae_accounts: list = ['t']
            wb_accounts: list = ['w']
            doubaowork_accounts: list = ['d']

        rows = cf._rows_of(_Agg(), None)
        self.assertIn('t', rows)
        self.assertIn('w', rows)
        self.assertIn('d', rows)


class PlatformToggleOrderTests(unittest.TestCase):
    """显示顺序与编号是两件事，别混。"""

    def test_display_order_starts_with_workbuddy(self):
        self.assertEqual(cf.PLATFORMS[0], 'workbuddy',
                         'WorkBuddy 是主力平台，显示顺序排第一')

    def test_display_order_has_no_qoder(self):
        self.assertNotIn('qoder', cf.PLATFORMS)

    def test_display_order_matches_id_set(self):
        """显示顺序里的平台集合必须与编号表完全一致（顺序可以不同）。"""
        self.assertEqual(set(cf.PLATFORMS), set(cf._UPID))


if __name__ == '__main__':
    unittest.main()
