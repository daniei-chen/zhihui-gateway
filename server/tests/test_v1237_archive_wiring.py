"""v1.2.37：接上一条定义了却从未执行的归档策略。

`retention` 为 `budget_reservations` 写了完整策略（180 天、只动已结算/已释放、
未决与 unknown 永不归档），但 `archiveworker.DATASETS` 只列了两张日志表。
策略与执行分居两个文件，漏一处不会报错，只会悄悄不生效 —— 生产上这张表已经
19.3 万行 / 27 MB，而且没有任何上限。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from server import archiveworker, retention


class ArchiveDatasetsAreWiredTests(unittest.TestCase):
    def test_every_archivable_dataset_is_actually_processed(self):
        """凡是策略里声明「先归档再清理」的表，都必须进处理清单。"""
        archivable = {
            definition.name
            for definition in retention.datasets()
            if definition.action == retention.ACTION_ARCHIVE_THEN_PRUNE
        }
        self.assertTrue(archivable, 'retention 里应当至少有可归档的数据集')
        missing = sorted(archivable - set(archiveworker.DATASETS))
        self.assertEqual(missing, [], f'这些数据集的归档策略没有接线：{missing}')

    def test_budget_reservations_is_included(self):
        self.assertIn('budget_reservations', archiveworker.DATASETS)

    def test_every_listed_dataset_has_a_definition(self):
        """反过来：清单里的每一项都得有策略，不能凭空多出一个名字。"""
        for name in archiveworker.DATASETS:
            retention.dataset(name)      # 不存在会抛 KeyError

    def test_unreserved_states_are_never_archived(self):
        """归档只针对已结算/已释放；未决与 unknown 必须留在表里等人工核对。"""
        definition = retention.dataset('budget_reservations')
        self.assertEqual(definition.archive_filter, "state IN ('settled','released')")
        self.assertEqual(definition.delete_filter, "state IN ('settled','released')")
        self.assertNotIn('reserved', definition.archive_filter)
        self.assertNotIn('unknown', definition.archive_filter)


if __name__ == '__main__':
    unittest.main()
