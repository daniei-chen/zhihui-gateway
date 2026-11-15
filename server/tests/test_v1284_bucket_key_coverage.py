"""v1.2.84：我加了一个桶却忘了初始化，监控循环每轮都崩。

## 事故经过

v1.2.83 在 `budget.py` 里加了 `review_buckets['new_24h'] += 1`，用来统计「最近 24 小时
新增」。**但 `review_buckets` 是普通 dict，不是 `Counter`** —— 键不存在时 `+= 1`
直接抛 `KeyError`。

部署后生产的监控循环抛了两次 traceback：

```
File "server/budget.py", line 725, in unresolved_summary
    review_buckets['new_24h'] += 1
KeyError: 'new_24h'
```

**后果比一个计数器坏掉严重得多**：`_monitor_tick` 在 `_budget.unresolved_summary()`
那一行抛异常，于是**整轮健康评估停摆** —— 磁盘水位、账号池、账本积压、告警状态机
全都没跑。

## 为什么测试没抓住

我写了 18 个测试，全都只测 `operational` 里的**纯函数**（喂一个 summary dict 进去）。
**没有一个真的执行 `budget.unresolved_summary`** —— 而 bug 恰好在那一侧。

## 这个测试怎么抓住它

不跑数据库，而是静态检查：把 `review_buckets = {...}` 的初始化键，与函数体内所有
`review_buckets['xxx'] +=` 用到的键做集合比较。**少一个就红。**
"""
from __future__ import annotations

import ast
import inspect
import unittest

from server import budget


def _initialised_keys() -> set[str]:
    """从源码里取出 `review_buckets = {...}` 的键。"""
    source = inspect.getsource(budget.unresolved_summary)
    tree = ast.parse(source.strip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name)]
        if not any(t.id == 'review_buckets' for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            raise AssertionError('review_buckets 应当是一个字面量 dict；'
                                 '如果换成 Counter 请更新这个测试的意图')
        return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError('没找到 review_buckets 的定义')


def _mutated_keys() -> set[str]:
    """取出所有 `review_buckets['xxx'] +=` 用到的键。"""
    source = inspect.getsource(budget.unresolved_summary)
    tree = ast.parse(source.strip())
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AugAssign):
            continue
        if not isinstance(node.target, ast.Subscript):
            continue
        value = node.target.value
        if not (isinstance(value, ast.Name) and value.id == 'review_buckets'):
            continue
        index = node.target.slice
        if isinstance(index, ast.Constant):
            keys.add(index.value)
    return keys


class EveryMutatedBucketIsInitialisedTests(unittest.TestCase):
    def test_no_mutation_targets_a_missing_key(self):
        """这就是 v1.2.83 崩掉的原因：加键忘了初始化。"""
        missing = _mutated_keys() - _initialised_keys()
        self.assertEqual(missing, set(),
                         f'这些桶被 += 但没有初始化，会抛 KeyError：{sorted(missing)}')

    def test_new_24h_is_both_initialised_and_used(self):
        """钉住这个具体的桶，防止它又被删掉初始化。"""
        self.assertIn('new_24h', _initialised_keys())
        self.assertIn('new_24h', _mutated_keys())

    def test_the_scan_finds_something(self):
        """如果解析器坏了，上面两条会空过 —— 这里保证它真的看到了东西。"""
        self.assertGreaterEqual(len(_initialised_keys()), 5)
        self.assertGreaterEqual(len(_mutated_keys()), 3)


class TheFunctionIsActuallyReachableTests(unittest.TestCase):
    """纯函数测试抓不到这次的 bug —— 至少确认函数签名与返回结构没变。"""

    def test_signature_takes_now_keyword(self):
        params = inspect.signature(budget.unresolved_summary).parameters
        self.assertIn('now', params)

    def test_it_is_what_the_monitor_calls(self):
        from server import operational
        source = inspect.getsource(operational._monitor_tick)
        self.assertIn('unresolved_summary', source)


class DictVersusCounterTests(unittest.TestCase):
    """把这个坑本身记成一条断言，后来的人一眼能看懂发生了什么。"""

    def test_a_plain_dict_raises_on_missing_key_augassign(self):
        box = {'a': 0}
        with self.assertRaises(KeyError):
            box['b'] += 1          # noqa: F841 - 就是要它抛

    def test_a_counter_does_not(self):
        from collections import Counter
        box = Counter({'a': 0})
        box['b'] += 1
        self.assertEqual(box['b'], 1)

    def test_review_buckets_is_still_a_plain_dict(self):
        """如果哪天换成 Counter，上面那条静态检查就失效了 —— 那时要改这个测试。"""
        source = inspect.getsource(budget.unresolved_summary)
        self.assertIn('review_buckets = {', source)
        self.assertNotIn('review_buckets = Counter(', source)


if __name__ == '__main__':
    unittest.main()
