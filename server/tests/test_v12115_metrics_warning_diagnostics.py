"""v1.2.115：一个被自己文档预言过的可观测性缺陷。

## 怎么发现的

不是靠读代码，是**去看近 24 小时的日志**，按出现次数排序：

```
15  ERROR operational health evaluation failed
12  WARNING server.main: Metrics admission snapshot unavailable (KeyError)
```

逐条追下去，前两个都已经是**旧版本的问题**（一个在 v1.2.83，一个已被今天的修复
补上了缺键；实测三个指标现在都出数、近一小时零告警）。

**但第三条留下了一个仍然存在的问题**：那条警告**只有异常类型，没有栈**。

原文：

```python
logger.warning('Metrics admission snapshot unavailable (%s)', type(exc).__name__)
```

**知道是 KeyError，不知道哪个键、哪一行。**

## 讽刺的地方

同一段代码的文档里写着：

> 这类问题的共同点是「少一个键 → 整段静默」：异常被捕获、服务照常、
> **日志一行警告容易被忽略**。

**它准确描述了这种失败模式，却没让自己变得可诊断。**

## 本版

把异常类型那一行保留，追加 `exc_info=True`。

**为什么不是换成别的**：习惯看简短警告的人不会觉得变吵（第一行还是
`unavailable (KeyError)`），而要查的人第一次就能看到位置。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 警告带栈 | 下次出问题又要靠猜 |
| 仍然只捕获、不抛出 | 一个取数源坏掉不该让整个指标端点 500 |
| 异常类型仍出现在文案里 | 扫日志时还能一眼看出是哪类错 |
"""
from __future__ import annotations

import inspect
import unittest

from server import main


class TheWarningCarriesAStackTraceTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_exc_info_is_passed(self):
        self.assertIn('exc_info=True', self.src,
                      '警告又变回只有类型了，下次出问题没法定��')

    def test_it_is_on_the_metrics_warning(self):
        """确保 `exc_info=True` 挂在这一条上，而不是别的日志。"""
        idx = self.src.rfind('Metrics admission snapshot unavailable')
        self.assertGreater(idx, 0)
        window = self.src[idx:idx + 200]
        self.assertIn('exc_info=True', window)

    def test_the_type_is_still_in_the_message(self):
        """扫日志时仍要能一眼看出是哪类异常。"""
        # 注释里也引用了这句原文，取最后一次出现才是真的 logger 调用
        idx = self.src.rfind('Metrics admission snapshot unavailable')
        window = self.src[idx:idx + 200]
        self.assertIn('%s', window)
        self.assertIn('type(exc).__name__', window)


class ItStillDegradesInsteadOfFailingTests(unittest.TestCase):
    """修的是可诊断性，不是「改成抛出去」。"""

    def test_it_still_swallows(self):
        src = inspect.getsource(main)
        idx = src.find('Metrics admission snapshot unavailable')
        # 往回找最近的 except，确认它是捕获而不是重新抛出
        before = src[:idx]
        last = before.rfind('except Exception')
        self.assertGreater(last, 0)
        segment = src[last:idx]
        self.assertIn('logger.warning', segment)
        self.assertNotIn('raise', segment,
                         '一个取数源坏掉不该让整个指标端点 500')

    def test_the_reason_for_swallowing_is_documented(self):
        src = inspect.getsource(main)
        idx = src.find('Metrics admission snapshot unavailable')
        window = src[max(0, idx - 1600):idx]
        self.assertIn('不该让整个端点', window,
                      '要写明为什么继续吞异常，否则后人会想当然改成抛出')


if __name__ == '__main__':
    unittest.main()
