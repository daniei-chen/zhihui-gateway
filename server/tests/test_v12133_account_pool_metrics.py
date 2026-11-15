"""v1.2.133：把账号池的集中度变成可监控的指标。

## 为什么做这件事

前三个版本的审计查出一件事：**96 个账号文件里只有 48 个在产生流量，
用量最高与最低相差 228 倍**，而其中一个余额比第一名还多的账号只被调用了 3 次。

那件事本身是上游的（另一个仓库的调度器），这个仓库改不了。

**但「它不均衡」应该是一个能被监控看到的数字，而不是一次人工排查才发现的结论。**

在那之前，要知道池子是否在倾斜，只能：
1. 想起来去查
2. 手写一段 SQL 或翻容器日志
3. 自己算比例

**这一版把这三步变成一次 `/metrics` 抓取。**

## 暴露什么

| 指标 | 含义 |
| --- | --- |
| `workbuddy_account_pool_accounts` | 窗口内有流量的账号数 |
| `workbuddy_account_pool_requests` | 窗口内这些账号上的请求数 |
| `workbuddy_account_pool_top_share` | 最忙那个账号占的比例（0~1） |

**`top_share` 是核心那个。** 它接近 1.0 就说明负载全压在一个账号上。

## 三个口径决定

**窗口取一小时，不取 24 小时。** 这是一个 gauge，每次抓取都算 ——
长窗口会让它对「刚刚开始倾斜」无感，而早期发现才有价值。

**只统计带账号标记的对话请求。** 模型目录、路由阶段就被拒的请求不落到具体账号上，
把它们算进来会稀释比例。

**单独包一层 `try`。** 这一个取数源出问题，不该连带把上面那十几项指标一起弄没 ——
那正是同一段代码里踩过的坑（一个键缺失导致整段静默）。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 三个指标都注册了 | 池子倾斜又变成看不见的事 |
| `top_share` 定义正确 | 分子分母搞反会让它永远接近 0 |
| 空窗口时不给负数也不崩 | 新部署的头一小时就是这个状态 |
| 取数失败不牵连其他指标 | 又回到「一个源坏了整段消失」 |

## 这份能力不做什么

**它只是观测，不改变任何调度行为。** 上游为什么不按配置加权，仍然是未解的 ——
本版没有、也不能修那个。
"""
from __future__ import annotations

import inspect
import re
import unittest

from server import main


class TheThreeMetricsExistTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_accounts_gauge(self):
        self.assertIn('workbuddy_account_pool_accounts', self.src)

    def test_requests_gauge(self):
        self.assertIn('workbuddy_account_pool_requests', self.src)

    def test_top_share_gauge(self):
        self.assertIn('workbuddy_account_pool_top_share', self.src)

    def test_all_three_are_gauges(self):
        """它们是「当前值」，不是累加计数 —— 用 counter 会一直涨。"""
        for name in ('workbuddy_account_pool_accounts',
                     'workbuddy_account_pool_requests',
                     'workbuddy_account_pool_top_share'):
            idx = self.src.find(name)
            self.assertGreater(idx, 0)
            window = self.src[idx:idx + 120]
            self.assertIn("'gauge'", window, '%s 不是 gauge' % name)


class TheShareIsComputedRightTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_share_divides_top_by_total(self):
        """分子必须是「最忙那个」，分母必须是「总数」。"""
        idx = self.src.find('workbuddy_account_pool_top_share')
        window = self.src[max(0, idx - 1200):idx]
        self.assertIn('_top / _total', window,
                      '比例算法不是 最忙/总数 —— 反了会让它永远接近 0')

    def test_it_guards_against_zero_total(self):
        """窗口内没有请求时不能除零。"""
        idx = self.src.find('workbuddy_account_pool_top_share')
        window = self.src[max(0, idx - 1200):idx]
        self.assertRegex(window, r'if _total|_total else|if not _total',
                         '没有零分母保护')

    def test_it_rounds_to_four_places(self):
        """四位小数足够看出倾角，也不会让指标行过长。"""
        idx = self.src.find('workbuddy_account_pool_top_share')
        window = self.src[idx:idx + 600]
        self.assertIn('round(_share, 4)', window)


class TheQueryIsCheapTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_window_is_one_hour(self):
        """一小时窗口 —— 长窗口对「刚开始倾斜」无感。"""
        idx = self.src.find('workbuddy_account_pool_accounts')
        window = self.src[max(0, idx - 1600):idx]
        self.assertIn('- 3600', window, '窗口不是一小时')

    def test_it_filters_to_chat_requests(self):
        """只算带账号标记的对话请求，避免被目录类请求稀释。"""
        idx = self.src.find('workbuddy_account_pool_accounts')
        window = self.src[max(0, idx - 1600):idx]
        self.assertIn("endpoint LIKE '%chat%'", window)

    def test_it_skips_rows_without_an_account(self):
        idx = self.src.find('workbuddy_account_pool_accounts')
        window = self.src[max(0, idx - 1600):idx]
        self.assertIn("account != ''", window)


class ItCannotBreakTheOtherMetricsTests(unittest.TestCase):
    """这段新增不该让「一个源坏掉 → 整段静默」那个老问题复发。"""

    def setUp(self):
        self.src = inspect.getsource(main)

    def test_the_block_is_wrapped(self):
        idx = self.src.find('account pool metrics unavailable')
        self.assertGreater(idx, 0, '没有单独的容错分支')
        window = self.src[max(0, idx - 1600):idx + 200]
        self.assertIn('try:', window)

    def test_it_logs_with_a_stack(self):
        """又一个「只打类型就没法查」的位置。"""
        idx = self.src.find('account pool metrics unavailable')
        window = self.src[idx:idx + 160]
        self.assertIn('exc_info=True', window)

    def test_it_does_not_raise(self):
        idx = self.src.find('account pool metrics unavailable')
        window = self.src[max(0, idx - 400):idx + 200]
        self.assertNotIn('raise ', window)


if __name__ == '__main__':
    unittest.main()
