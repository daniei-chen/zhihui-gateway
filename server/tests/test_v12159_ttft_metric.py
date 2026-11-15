"""v1.2.159：把首字延迟分位数暴露成指标。

## 为什么

2026-11-15 查性能时发现：平均首字延迟 7.3 秒、p99 57 秒，
而**这些数字当时只存在于数据库里** —— 看板看不见，也就没人会盯。

两个账号的首字延迟接近 40 秒（其他 6–13 秒），差距六倍，直接把 p99 拉高。
这类「少数账号拖垮长尾」的问题，不上趋势图就只能等用户投诉。

端到端延迟会把这个指标藏起来：总延迟 10 秒里 7 秒是等第一个字，
只看总数会以为是「模型慢」，看不出是「排队等首字」。

## 两个刻意的取舍

**样本少于 20 时不报分位值。** 10 个样本算出的 p99 就是那 10 个里最大的一个，
展示出去会让人以为看到了长尾。**宁可空着，也不给一个看起来像结论的数字。**

**带 30 秒缓存。** 查询很便宜（`ts` 有索引，两小时窗口实测 4 毫秒），
缓存不是为了省查询，是避免多个监控实例同时抓时把同一份数据算很多遍。

## 写这一版时差点弄坏 `/metrics`

插入新函数时用的锚点是 `def prometheus_metrics(...)` 那一行，
结果新代码被塞进了**装饰器和函数之间**：

```python
@app.get('/metrics')
#: 我的新注释...
_TTFT_CACHE = {...}
def _ttft_quantiles(...): ...
def prometheus_metrics(...):      # ← 装饰器已经失去作用对象
```

`ast.parse` 直接报错才发现的。**如果没有跑语法检查就发版，`/metrics` 会返回 500** ——
而 `/metrics` 断了，监控就全瞎了，包括这条新指标本身。

所以本测试**第一条**就钉住装饰器必须紧邻函数。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

MAIN = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(MAIN)
MODULE_FUNCTIONS = {n.name for n in TREE.body if isinstance(n, ast.FunctionDef)}


class TheRouteStillWorksTests(unittest.TestCase):
    """装饰器脱离函数，`/metrics` 直接 500。

    这不是假想：写这一版时就是这么错的，靠 `ast.parse` 报错才发现。
    """

    def _lines(self):
        return MAIN.splitlines()

    def test_decorator_is_immediately_above_the_function(self):
        lines = self._lines()
        for i, line in enumerate(lines):
            if line.strip() == "def prometheus_metrics(request: Request):":
                previous = lines[i - 1].strip()
                self.assertEqual(previous, "@app.get('/metrics')",
                                 '装饰器与函数之间被插进了别的东西：' + previous)
                return
        self.fail('没找到 prometheus_metrics 定义')

    def test_no_code_between_any_decorator_and_its_function(self):
        """通用检查：任何 `@app.` 装饰器后面必须紧跟 def 或另一个装饰器。"""
        lines = self._lines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith('@app.'):
                continue
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith('@'):
                j += 1
            follower = lines[j].strip() if j < len(lines) else ''
            self.assertTrue(follower.startswith('def ') or follower.startswith('async def '),
                            '装饰器 %s 后面不是函数定义：%s' % (stripped, follower))


class ItEmitsTheQuantilesTests(unittest.TestCase):
    def test_helper_exists_at_module_scope(self):
        self.assertIn('_ttft_quantiles', MODULE_FUNCTIONS,
                      '没在模块级定义 —— 可能又被塞进了别的函数里')

    def test_metric_is_emitted(self):
        self.assertIn('workbuddy_ttft_ms', MAIN)

    def test_sample_count_is_emitted(self):
        self.assertIn('workbuddy_ttft_samples', MAIN)

    def test_help_and_type_are_written_once(self):
        """带标签的序列是手工拼的，HELP/TYPE 必须只**写**一份。

        注意：字符串 `# HELP workbuddy_ttft_ms ` 在源码里出现两次是正常的 ——
        一次是去重检查 `any(x.startswith(...))`，一次才是真正写入。
        第一版测试数了全部出现次数，被自己的去重检查绊倒。
        """
        self.assertEqual(MAIN.count("lines.append('# HELP workbuddy_ttft_ms "), 1)
        self.assertEqual(MAIN.count("lines.append('# TYPE workbuddy_ttft_ms "), 1)


class ItWithholdsQuantilesOnThinSamplesTests(unittest.TestCase):
    def test_threshold_is_twenty(self):
        self.assertIn('len(values) >= 20', MAIN,
                      '没设样本门槛，少量样本会被当成有意义的 p99')

    def test_quantiles_start_as_none(self):
        idx = MAIN.find("result = {'samples': len(values)")
        self.assertGreater(idx, 0)
        window = MAIN[idx:idx + 160]
        self.assertIn("'p99': None", window, '默认值不是 None，会先报一个假分位')


class ItCachesTests(unittest.TestCase):
    def test_cache_exists(self):
        # v1.2.160 起改成按列分开的缓存槽，名字变了但性质没变：
        # 必须有一份带上限的缓存，避免并发抓取重复计算。
        self.assertIn('_COLUMN_CACHES', MAIN)

    def test_cache_has_a_bounded_ttl(self):
        self.assertIn('<= 30', MAIN, '缓存没有上限，指标会变成陈旧值')


class ItDegradesSafelyTests(unittest.TestCase):
    def test_query_failure_does_not_break_metrics(self):
        # v1.2.160 起实现搬到了公用函数里，`_ttft_quantiles` 只剩一行委托。
        # 断言必须跟着实现走，否则它检查的是一段空壳。
        idx = MAIN.find('def _column_quantiles')
        block = MAIN[idx:idx + 2600]
        self.assertIn('except Exception', block,
                      '查库失败会让 /metrics 整体 500')
        self.assertIn('return None', block)

    def test_query_uses_the_ts_index(self):
        """窗口过滤必须带 ts，否则会全表扫 33 万行。"""
        idx = MAIN.find('def _column_quantiles')
        block = MAIN[idx:idx + 2600]
        self.assertIn('WHERE ts > ?', block, '没按时间过滤，会全表扫描')


if __name__ == '__main__':
    unittest.main()
