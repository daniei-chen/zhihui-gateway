"""v1.2.135：把数据库大小挂成指标，让容量曲线自己长出来。

## 上一版算出的那条时间线有个毛病

上一版做完容量审计，算出一条结论：

> 转发事件表每天约 2.3 MB，一年约 840 MB，当前剩余空间大约七年。

**这是手算的。**

**算完之后它就不再有更新** —— 除非又有人想起来去 `stat` 一遍。
这正是「人工结论」的通用毛病：**它只在那一天是对的。**

**挂成指标之后，容量就是一条能直接画的曲线**，而不是一次性的、已经过期的估算。

## 为什么只 `stat`，不查行数

行数比字节数更能说明「表在膨胀」，但 `count(*)` 在大表上是要扫的。
这个端点会被定时抓取，**不该带上一项 O(n) 的工作**。

`stat` 只读 inode，代价接近于零。

**代价是它看不到「表变小了但文件没缩」** —— 而那种情况本来就需要整理空闲页，
指标帮不上忙。

## 为什么连 sidecar 一起报

WAL 模式下主库旁边还有两个伴随文件。WAL 长时间不 checkpoint 会单独涨到
几十上百 MB，而只看主库文件会**完全看不见那一块**。

**它们是两个独立的故障形态**：主库涨是数据多，sidecar 涨是 checkpoint 没跑。
**混成一个指标就分不清了。**

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 两个指标都注册 | 容量又回到「需要有人想得起来才查」 |
| 都是 gauge | 用 counter 会一直累加，画出错误的增长曲线 |
| 不查表只 stat | 每次抓取都扫一遍大表 |
| sidecar 单独报 | WAL 膨胀与数据增长混在一起，分不清 |
| 失败不牵连其他 | 又回到「一个源坏了整段消失」 |
"""
from __future__ import annotations

import inspect
import unittest

from server import main


class TheTwoMetricsExistTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_main_database_gauge(self):
        self.assertIn('workbuddy_db_bytes', self.src)

    def test_sidecar_gauge(self):
        self.assertIn('workbuddy_db_sidecar_bytes', self.src)

    def test_both_are_gauges(self):
        for name in ('workbuddy_db_bytes', 'workbuddy_db_sidecar_bytes'):
            idx = self.src.find(name)
            self.assertGreater(idx, 0)
            self.assertIn("'gauge'", self.src[idx:idx + 100], '%s 不是 gauge' % name)


class ItOnlyStatsTests(unittest.TestCase):
    """`stat` 读 inode；`count(*)` 扫表。这个端点会被频繁抓，只能用前者。"""

    def setUp(self):
        self.src = inspect.getsource(main)

    def _block(self):
        idx = self.src.find('workbuddy_db_bytes')
        return self.src[max(0, idx - 1400):idx + 400]

    def _code_lines(self):
        """只看代码行。

        注释里会提到 `count(*)` 来说明「为什么不用它」——
        那是解释，不是实现。按整段文本匹配会把解释当成违规。
        """
        out = []
        for line in self._block().splitlines():
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            out.append(line)
        return '\n'.join(out)

    def test_it_uses_stat(self):
        self.assertIn('.st_size', self._code_lines())

    def test_it_does_not_count_rows(self):
        code = self._code_lines().lower()
        self.assertNotIn('count(*)', code,
                         '这个指标里出现了 count(*) —— 每次抓取都会扫表')
        self.assertNotIn('query_one', code)
        self.assertNotIn('query(', code)


class TheSidecarIsReportedSeparatelyTests(unittest.TestCase):
    """WAL 膨胀不等于数据增长，两者要能分开看。"""

    def setUp(self):
        self.src = inspect.getsource(main)

    def test_it_sums_both_suffixes(self):
        idx = self.src.find('workbuddy_db_sidecar_bytes')
        block = self.src[max(0, idx - 1200):idx]
        self.assertIn("'-wal'", block)
        self.assertIn("'-shm'", block)

    def test_it_does_not_merge_into_the_main_gauge(self):
        """主库指标必须是纯主库大小，不能把 sidecar 加进去。

        取从「主库指标之前」到「它的值参数」之间的窗口 ——
        太窄会看不到实参，太宽会把 sidecar 那一段也圈进来。
        """
        idx = self.src.find("_m('workbuddy_db_bytes'")
        self.assertGreater(idx, 0)
        window = self.src[idx:idx + 400]
        self.assertIn('_main)', window.replace(' ', ''),
                      '主库指标的值不是 _main')
        self.assertNotIn('_side)', window.replace(' ', ''),
                         '主库指标把 sidecar 也加进去了')


class ItCannotBreakTheOtherMetricsTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_the_block_is_wrapped(self):
        idx = self.src.find('database size metrics unavailable')
        self.assertGreater(idx, 0, '没有单独的容错分支')
        self.assertIn('try:', self.src[max(0, idx - 1200):idx])

    def test_it_logs_with_a_stack(self):
        idx = self.src.find('database size metrics unavailable')
        self.assertIn('exc_info=True', self.src[idx:idx + 160])

    def test_it_does_not_raise(self):
        idx = self.src.find('database size metrics unavailable')
        self.assertNotIn('raise ', self.src[max(0, idx - 400):idx + 200])

    def test_missing_file_does_not_raise(self):
        """新部署、库还没建时不能崩。"""
        idx = self.src.find('workbuddy_db_bytes')
        block = self.src[max(0, idx - 1400):idx + 400]
        self.assertIn('.exists()', block)


if __name__ == '__main__':
    unittest.main()
