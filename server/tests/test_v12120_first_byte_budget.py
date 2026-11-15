"""v1.2.120：一个按平台收紧的超时，被环境变量静默覆盖了。

## 怎么发现的

不是读代码发现的，是**查真实请求记录**：

```
platform = qoder
actual_platform = qoder
latency = 183.6 秒
first_token_ms = 空      ← 首字节从未到达
status = 502
```

而 `config.PLATFORM_TIMEOUT` 里 qoder 是 **60 秒**，注释写着
「让『不可用』在**一分钟内**变成可见的错误」。

**183 秒 vs 承诺的 60 秒。**

## 根因：两个预算，一个被环境变量钉死

`Budgets.from_config()` 这样算：

```
base = upstream_timeout or WB_UPSTREAM_TIMEOUT      # qoder → 60
first_byte_s = WB_FIRST_BYTE_TIMEOUT_S or base      # 生产设了 240 ← 硬覆盖
total_s      = WB_TOTAL_TIMEOUT_S      or base      # → 60
```

**等首字节**走的是 `first_byte_s`（240），不是 `total_s`（60）。

于是那个 60 只对「读 body」生效，而请求卡在**等首字节**时，
窗口是 240 秒。183.6 秒落在里面，所以从未被切断。

## 为什么不能直接删掉那个 240

它是为 workbuddy 的长 prompt 设的 —— 实测有 158 次首字节超过 60 秒，
最长 186 秒，属于正常 prefill。workbuddy 的 base 是 300，**240 < 300，不受影响**。

## 修法：取 min

```
first_byte_s = min(WB_FIRST_BYTE_TIMEOUT_S, base)
```

| 平台 | base | 环境值 | 修前 | 修后 |
| --- | --- | --- | --- | --- |
| workbuddy | 300 | 240 | 240 | **240**（不变） |
| qoder | 60 | 240 | **240** | **60** |

**显式配置仍然优先，但不能比这条请求自己的总预算还宽** ——
比总预算还宽的首字节窗口没有意义，它允许一条请求在「总时长」之外接着等。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| qoder 的首字节被收到 60 | 又回到「一分钟内可见」的承诺不兑现 |
| workbuddy 的 240 不受影响 | 正常的长 prompt 被误切 |
| 无显式环境值时仍用 base | 默认行为被改坏 |
| total 仍按平台 | 只修了首字节，别的没动 |
"""
from __future__ import annotations

import unittest
from unittest import mock

from server import deadline


class ThePlatformWindowIsHonouredTests(unittest.TestCase):
    def test_qoder_first_byte_is_capped_by_its_base(self):
        """核心断言：183 秒那种请求必须在一分钟内被切断。"""
        with mock.patch.dict('os.environ', {'WB_FIRST_BYTE_TIMEOUT_S': '240'}):
            b = deadline.request_budgets(60.0)
        self.assertLessEqual(b.first_byte_s, 60.0,
                             'qoder 的首字节窗口不该超过它的总预算')

    def test_workbuddy_keeps_its_long_window(self):
        """242 是为长 prompt 设的，不能被顺手削掉。"""
        with mock.patch.dict('os.environ', {'WB_FIRST_BYTE_TIMEOUT_S': '240'}):
            b = deadline.request_budgets(300.0)
        self.assertEqual(b.first_byte_s, 240.0)

    def test_without_env_it_still_uses_base(self):
        with mock.patch.dict('os.environ', {}, clear=False):
            for name in ('WB_FIRST_BYTE_TIMEOUT_S',):
                import os
                os.environ.pop(name, None)
            b = deadline.request_budgets(60.0)
        self.assertLessEqual(b.first_byte_s, 60.0)


class NothingElseMovedTests(unittest.TestCase):
    def test_total_still_follows_the_platform_value(self):
        with mock.patch.dict('os.environ', {'WB_FIRST_BYTE_TIMEOUT_S': '240'}):
            b = deadline.request_budgets(60.0)
        self.assertLessEqual(b.total_s, 60.0)

    def test_connect_timeout_is_untouched(self):
        with mock.patch.dict('os.environ', {'WB_FIRST_BYTE_TIMEOUT_S': '240'}):
            b = deadline.request_budgets(60.0)
        self.assertLessEqual(b.connect_s, 60.0)

    def test_normalisation_still_runs(self):
        """预算对象要保持 normalized() 的不变量（首字节 ≤ 总时长）。"""
        with mock.patch.dict('os.environ', {'WB_FIRST_BYTE_TIMEOUT_S': '240'}):
            b = deadline.request_budgets(60.0)
        self.assertLessEqual(b.first_byte_s, b.total_s)


class TheCaseIsRecordedTests(unittest.TestCase):
    def test_source_names_the_measured_seconds(self):
        import inspect
        src = inspect.getsource(deadline.Budgets.from_config)
        self.assertIn('183', src,
                      '要写明实测秒数，否则看不出这个约束是怎么来的')

    def test_source_says_why_min_and_not_removal(self):
        import inspect
        src = inspect.getsource(deadline.Budgets.from_config)
        self.assertIn('186', src,
                      '要写明长 prompt 的真实首字节，否则后人会直接删掉 240')


if __name__ == '__main__':
    unittest.main()
