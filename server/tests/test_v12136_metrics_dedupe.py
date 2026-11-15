"""v1.2.136：移除上一版加重的重复指标。

## 上一版做错了什么

我想把数据库大小挂成指标，于是加了一对：

| 我加的 | 语义 |
| --- | --- |
| `workbuddy_db_bytes` | **只算主库文件** |
| `workbuddy_db_sidecar_bytes` | `-wal` + `-shm` |

**而这两个早就存在**（不同的名字）：

| 已存在 | 语义 |
| --- | --- |
| `workbuddy_db_size_bytes` | **主库 + `-wal` + `-shm`**（总占用） |
| `workbuddy_db_wal_size_bytes` | **只有 `-wal`**（诊断 checkpoint 是否落后） |

**问题的根源是我上一版 grep 了一个前缀**（`workbuddy_account`），
就断定「没有容量指标」—— **「没查到」和「不存在」不是一回事。**

## 为什么删我那两个，而不是删已有的

**已有的那一对设计得更好，而且是两个不同语义：**

- **总占用**回答「磁盘还要留多少」—— 这必须含 WAL/SHM，
  因为未 checkpoint 时它们能到几十 MB
- **单独的 WAL** 回答「checkpoint 是不是落后了」—— 这是**诊断**指标，
  混进总量就看不出来了

**我加的 `workbuddy_db_bytes` 只算主库** —— 它**系统性地低估**磁盘占用，
而这正是那个指标存在的目的。**删掉它不会损失信息，它本来就在误导。**

`workbuddy_db_sidecar_bytes` 只是把已有的两项相加，**没有新增信息**。

## 这一版之后

`/metrics` 里关于数据库体积的指标回到 **2 条**，语义各自清晰：

| 指标 | 回答什么 |
| --- | --- |
| `workbuddy_db_size_bytes` | 磁盘还要留多少 |
| `workbuddy_db_wal_size_bytes` | checkpoint 落后了吗 |

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 我加的那两个不再出现 | 重复指标留在面板上，让人不知道该信哪个 |
| 已有的那两个还在 | 误删了真正有用的 |
| 总占用含 sidecar | 低估磁盘占用，正是它要防的事 |
| 单独 WAL 仍独立 | 诊断价值消失 |

## 顺带守一个更普遍的东西

`/metrics` 里**任何指标名都不该重复注册**。这一版加一条扫描：
把源码里出现的所有指标名收出来，检查没有两处注册同一个名字。

**重复注册在指标系统里是静默的** —— 后一个覆盖前一个，画出来的线看不出异常。
"""
from __future__ import annotations

import inspect
import re
import unittest

from server import main


class TheDuplicatesAreGoneTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_db_bytes_is_not_registered(self):
        """只算主库的那个 —— 系统性低估，不该存在。"""
        self.assertNotIn('workbuddy_db_bytes', self.src)

    def test_db_sidecar_bytes_is_not_registered(self):
        """把已有两项相加的那个 —— 无新增信息。"""
        self.assertNotIn('workbuddy_db_sidecar_bytes', self.src)


class TheOriginalsSurvivedTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_total_size_gauge_still_there(self):
        self.assertIn('workbuddy_db_size_bytes', self.src)

    def test_wal_size_gauge_still_there(self):
        self.assertIn('workbuddy_db_wal_size_bytes', self.src)

    def test_total_includes_sidecars(self):
        """总占用必须来自那个含 sidecar 的聚合值，不能退化成只算主库。"""
        idx = self.src.find('workbuddy_db_size_bytes')
        block = self.src[idx:idx + 500]
        self.assertIn("'total'", block,
                      '总占用用的不是聚合值 —— 会低估磁盘占用')

    def test_wal_is_reported_alone(self):
        """单独 WAL 是诊断指标，不能被改成总量。"""
        idx = self.src.find('workbuddy_db_wal_size_bytes')
        block = self.src[idx:idx + 400]
        self.assertIn("'wal'", block)
        self.assertNotIn("'total'", block)


class NoMetricNameIsRegisteredTwiceTests(unittest.TestCase):
    """整个 /metrics 实现里指标名不能重复注册。

    重复注册在指标系统里是**静默的**：后一个覆盖前一个，图上完全看不出异常，
    但你会永远看到错的那条值。
    """

    def test_no_duplicate_registration(self):
        src = inspect.getsource(main)
        # 只统计「注册调用」里的名字：_m('name', ...
        names = re.findall(r"_m\(\s*'([a-z0-9_]+)'", src)
        dupes = {n for n in names if names.count(n) > 1}
        self.assertEqual(dupes, set(),
                         '这些指标名被注册了不止一次：%s' % sorted(dupes))

    def test_the_scan_actually_found_something(self):
        """防止正则失效导致这条测试永远通过。"""
        src = inspect.getsource(main)
        names = re.findall(r"_m\(\s*'([a-z0-9_]+)'", src)
        self.assertGreater(len(names), 10,
                           '只匹配到 %d 个指标名，正则可能失效了' % len(names))


if __name__ == '__main__':
    unittest.main()
