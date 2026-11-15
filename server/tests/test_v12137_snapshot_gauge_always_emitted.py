"""v1.2.137：让「没有快照」变成可报警的状态，而不是「指标不存在」。

## 起因：一次源码与运行时的对账

上一版的教训是「grep 一个前缀就断言不存在」。这一版把它变成系统性对账：
把源码里注册的指标名，和 `/metrics` 实际输出的名字逐个比对。

**对账过程本身也踩了同一个坑两次：**

1. 第一次只 grep 了 `_m(`，漏掉 `emit(`（另一个模块的注册函数）
2. 第二次把 `latency_ms_p50` 这类动态拼接的名字截成了前缀，报成「源码有、生产无」

**对完之后的结论是：55 个运行时指标全部能在源码里找到出处。**
`main.py` 41 个 + 运行时模块 11 个 + 循环/拼接生成的一批。

## 对账里唯一一个真实问题

有一条指标**注册了但不一定输出**：

```
_age = operational.snapshot_age_seconds()
if _age is not None:          # ← None 时整条序列消失
    _m('workbuddy_snapshot_age_seconds', ..., _age)
```

**后果是：「从未产出过快照」和「这个指标坏了」在监控上完全一样** ——
都是「这条序列不存在」。

**而前者恰恰是最该报警的状态。** 那段代码的注释自己就记着
2026-11-15 的故障：备份年龄一直 ok，加密快照断了整整一天没人发现。

**「缺一个数」比「数很大」更难被发现。** 告警规则通常写成
「值 > 阈值」，而序列不存在时它**安静地不触发**。

## 改法

**总是输出**：没有快照时报 `-1`。

这样告警可以直接写成「值 > 86400 或值 == -1」，
**不用再赌这条序列存不存在**。

## 这个模式值得记住

**「条件性输出」在监控里是一个陷阱**：它把「状态异常」伪装成「没有数据」。

一个 gauges 要么总在，要么总不在 —— **不该取决于它测的那个东西好不好**。
"""
from __future__ import annotations

import inspect
import unittest

from server import main


class TheSnapshotGaugeIsAlwaysEmittedTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def _block(self):
        idx = self.src.find('workbuddy_snapshot_age_seconds')
        self.assertGreater(idx, 0)
        return self.src[max(0, idx - 900):idx + 900]

    def test_it_is_registered(self):
        self.assertIn("_m('workbuddy_snapshot_age_seconds'", self.src)

    def test_registration_is_not_conditional_on_the_value(self):
        """注册那一行不该被 `if ... is not None` 包起来。

        这是本条测试的全部意义：序列必须总在。
        """
        block = self._block()
        # 找到注册语句所在的那一段，它前面不该有「值为 None 就跳过」的判断
        idx = block.find("_m('workbuddy_snapshot_age_seconds'")
        guard = block[:idx]
        # 允许取数后判 None 做别的处理，但不允许用它决定「注不注册」
        self.assertNotIn('if _snapshot_age is not None:', guard,
                         '注册仍然被「有值才输出」包着 —— 序列会消失')

    def test_missing_snapshot_becomes_minus_one(self):
        """没有快照时要报 -1，不是不报。"""
        block = self._block()
        self.assertIn('-1', block,
                      '没有把「缺失」编码成一个值')
        self.assertRegex(block, r'-1 if _snapshot_age is None',
                         '-1 的条件不是「年龄为 None」')

    def test_the_help_text_explains_the_sentinel(self):
        """-1 的含义必须写在 HELP 里，否则看图的人不知道。"""
        block = self._block()
        self.assertIn('-1 means none exists', block)


class ItStillCannotBreakTheEndpointTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_failure_is_swallowed(self):
        idx = self.src.find('workbuddy_snapshot_age_seconds')
        block = self.src[idx:idx + 1400]
        self.assertIn('except Exception:', block)
        self.assertNotIn('raise ', block)

    def test_it_is_a_gauge(self):
        idx = self.src.find("_m('workbuddy_snapshot_age_seconds'")
        self.assertIn("'gauge'", self.src[idx:idx + 100])


class NoOtherGaugeIsConditionallyEmittedTests(unittest.TestCase):
    """同一个陷阱不该在别处也有。

    这条测试把整个 `/metrics` 实现扫一遍，找出「注册被 `is not None` 包着」的形态。
    **它不追求零命中**（有些指标确实该有条件），而是要求：
    命中的每一个都必须在 HELP 文本里说明「缺席意味着什么」。
    """

    def setUp(self):
        self.src = inspect.getsource(main)

    def test_each_conditional_gauge_says_what_absence_means(self):
        import re
        problems = []
        for match in re.finditer(r'if ([a-z_]+) is not None:\s*\n\s+_m\(', self.src):
            start = match.start()
            window = self.src[start:start + 600]
            if 'means' not in window and 'absent' not in window and 'missing' not in window:
                name = re.search(r"_m\('([a-z0-9_]+)'", window)
                problems.append(name.group(1) if name else '<unknown>')
        self.assertEqual(problems, [],
                         '这些指标在值缺失时整条序列消失，而 HELP 没说明缺席的含义：%s'
                         % problems)
