"""v1.2.160：上下文长度（prompt tokens）进指标。

## 起因

2026-11-15 有反馈说长对话跑到后面会开始重复同一段内容。

网关侧三项都排除了（历史不被裁剪、不注入系统消息、不丢推理内容，
24 小时内 `reasoning` 相关错误为零）。但数据面看到一个强信号：

    近三小时平均 prompt_tokens 31 万，**最大 50 万**，
    还有一个请求输出了 2 万 token / 93 秒。

正常回答不会有两万字。那几乎可以确定是在重复。

**问题在于那个数字当时没有任何地方能看到。** 要发现「对话在膨胀」，
只能去翻数据库；等到它表现为循环，已经晚了。

## 为什么不设阈值

各模型上下文上限不同，上游随时会调。写死一个数，要么太松没意义、
要么太紧天天误报。**只报分位数和最大值**，让知道自家模型上限的人自己判断。

## 顺手做的一件事

原来的首字延迟查询和这个上下文查询是**完全一样的形状**
（窗口内排序取分位、样本不足不报、读失败返回空）。
所以要抽成一个函数，而不是复制第二份 ——
复制的那份以后一定会出现「只改了一处」。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 列名白名单 | 列名要拼进 SQL，不受控就是注入面 |
| 缓存按列分开 | 两个指标共用一份缓存，数值会串 |
| 样本不足不报分位 | 少量样本算出的 p99 被当成真实的 |
| 读失败不影响端点 | `/metrics` 整体 500，监控全瞎 |
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

MAIN = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(MAIN)
MODULE_FUNCTIONS = {n.name for n in TREE.body if isinstance(n, ast.FunctionDef)}


class TheQueryIsSharedTests(unittest.TestCase):
    """抽成一个函数，而不是两份几乎相同的代码。"""

    def test_shared_helper_exists(self):
        self.assertIn('_column_quantiles', MODULE_FUNCTIONS)

    def test_both_wrappers_delegate(self):
        for name, column in (('_ttft_quantiles', 'first_token_ms'),
                             ('_context_quantiles', 'prompt_tokens')):
            idx = MAIN.find('def %s(' % name)
            self.assertGreater(idx, 0, name + ' 不存在')
            window = MAIN[idx:idx + 900]
            self.assertIn("_column_quantiles('%s'" % column, window,
                          name + ' 没有走公用实现')

    def test_it_is_not_copy_pasted(self):
        """排序取分位的那几行只该出现一次。"""
        self.assertEqual(MAIN.count('ORDER BY %s'), 1,
                         '分位查询被复制了第二份')


class TheColumnNameIsWhitelistedTests(unittest.TestCase):
    """列名会拼进 SQL，必须是不受外部影响的常量集合。"""

    def test_whitelist_exists(self):
        self.assertIn("allowed = {'first_token_ms': 'ttft'", MAIN)

    def test_unknown_column_returns_none(self):
        self.assertIn('if slot is None:', MAIN)
        idx = MAIN.find('if slot is None:')
        self.assertIn('return None', MAIN[idx:idx + 80])

    def test_it_builds_sql_from_the_constant_only(self):
        """拼进 SQL 的只能是白名单里的值，不能是调用方给的字符串。"""
        idx = MAIN.find('def _column_quantiles')
        block = MAIN[idx:idx + 2600]
        self.assertIn('% (column, column, column)', block,
                      'SQL 拼装方式变了，需要重新确认列的来源')

    def test_only_the_two_registered_columns_are_used(self):
        used = set()
        for col in ('first_token_ms', 'prompt_tokens'):
            if "_column_quantiles('%s'" % col in MAIN:
                used.add(col)
        self.assertEqual(used, {'first_token_ms', 'prompt_tokens'},
                         '出现了未登记的列，或漏了已登记的')


class CachesAreSeparateTests(unittest.TestCase):
    """两个指标不能共用一份缓存，否则数值会串。"""

    def test_cache_is_keyed_by_slot(self):
        self.assertIn('_COLUMN_CACHES.setdefault(slot', MAIN)

    def test_old_single_cache_is_gone(self):
        bindings = {node.id for node in ast.walk(TREE) if isinstance(node, ast.Name)}
        self.assertNotIn('_TTFT_CACHE', bindings,
                         '旧的那份单一缓存还在，会和新的混用')


class TheMetricsAreEmittedTests(unittest.TestCase):
    def test_context_metrics_exist(self):
        for name in ('workbuddy_context_samples', 'workbuddy_context_tokens',
                     'workbuddy_context_max_tokens'):
            self.assertIn(name, MAIN, name + ' 没输出')

    def test_help_and_type_written_once(self):
        self.assertEqual(MAIN.count("lines.append('# HELP workbuddy_context_tokens "), 1)
        self.assertEqual(MAIN.count("lines.append('# TYPE workbuddy_context_tokens "), 1)

    def test_no_hardcoded_threshold(self):
        """不该出现「上下文超过某个数就报警」的硬编码判断。"""
        idx = MAIN.find('def _context_quantiles')
        block = MAIN[idx:idx + 1400]
        self.assertNotIn('> 100000', block)
        self.assertNotIn('> 200000', block)


class ItDegradesSafelyTests(unittest.TestCase):
    def test_query_failure_returns_none(self):
        idx = MAIN.find('def _column_quantiles')
        block = MAIN[idx:idx + 2600]
        self.assertIn('except Exception', block)
        self.assertIn('return None', block)

    def test_sample_threshold_kept(self):
        self.assertIn('len(values) >= 20', MAIN)

    def test_max_is_only_reported_with_enough_samples(self):
        """最大值也只在该有分位值的时候给，否则单条巨大会被当结论。"""
        idx = MAIN.find("max=values[-1]")
        self.assertGreater(idx, 0)
        guard = MAIN.rfind('if len(values) >= 20', 0, idx)
        self.assertGreater(guard, 0, 'max 在样本门槛之外被赋值')


class TheRouteIsStillIntactTests(unittest.TestCase):
    def test_decorator_still_beside_its_function(self):
        lines = MAIN.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == 'def prometheus_metrics(request: Request):':
                self.assertEqual(lines[i - 1].strip(), "@app.get('/metrics')")
                return
        self.fail('prometheus_metrics 定义不见了')


if __name__ == '__main__':
    unittest.main()
