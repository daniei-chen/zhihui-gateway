"""v1.2.114：一个持续两版的假阳性，根因是 2 秒缓存。

## 症状

`test_proxy_routes_api` 里两条测试**单独跑通过**，**与另一个文件一起跑就失败**。

我连着两个版本撞到它，每次都花一轮去确认「不是我改坏的」。

## 我猜错过两次

| 猜测 | 实际 |
| --- | --- |
| 数据库里留了代理配置，导致来源判定成「面板」 | **来源判定是对的**（就是「环境变量」） |
| 另一个测试改了全局配置没还原 | **它还原了** |

**两次都错在同一个毛病**：没看失败的具体断言，就照症状编原因。

## 真正的根因

失败的是 `effective` 为空，而 `source` 是对的。

`effective` 来自 `global_http_proxy()`，它有一个**模块级 TTL 缓存**：

```
if cached['generation'] == generation and time.monotonic() - cached['at'] < TTL:
    return cached['value']
```

TTL 是 **2 秒**。

**生产上改设置会调 `invalidate_global_proxy()` 把 generation 加一**，
缓存随之失效。但测试直接 patch 常量 `HTTP_PROXY` ——
**常量变了，generation 没变**，于是 2 秒内读回旧值。

两个文件一起跑时，前一个文件在 2 秒内读过一次，缓存就带着旧值过来了；
单独跑时没有前序调用，缓存是空的，所以通过。

**这解释得非常干净**：为什么是「组合失败、单独通过」，而不是随机失败。

## 修法

测试侧补上生产路径本来就会做的那一步 —— 调失效。

- `_Base.setUp`：patch 完常量后失效一次，并在 cleanup 里再失效一次
- `test_clear_falls_back_to_env`：**它先 PUT 再 patch**，PUT 已经让缓存算过一次
  （那时环境变量还是空的），所以 patch 之后要**再失效一次**

**没有改生产代码。** 2 秒缓存本身是合理的优化，问题在测试没有模拟
「改了配置就失效」这个约定。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 基类 setUp 里调了失效 | 又回到「组合失败、单独通过」 |
| cleanup 里也调了失效 | 本次用例的缓存泄漏给下一个 |
| 那个先 PUT 再 patch 的用例也失效 | 它拿到的是 PUT 时的旧值 |
| 根因被记录 | 后来人会重复我那两次猜测 |
"""
from __future__ import annotations

import inspect
import unittest
from pathlib import Path

TEST_FILE = Path(__file__).resolve().parent / "test_proxy_routes_api.py"


class TheInvalidationIsPresentTests(unittest.TestCase):
    def setUp(self):
        self.src = TEST_FILE.read_text(encoding="utf-8")

    def test_base_setup_invalidates_the_cache(self):
        self.assertIn('invalidate_global_proxy', self.src,
                      '基类没有失效缓存，会重新出现「组合失败、单独通过」')

    def test_it_invalidates_more_than_once(self):
        """setUp 一次、cleanup 一次、那个特殊用例再一次 —— 少于三次说明有人删了。"""
        self.assertGreaterEqual(
            self.src.count('invalidate_global_proxy'), 3,
            '失效调用的次数变少了；至少 setUp 与 cleanup 各一次，加上先 PUT 的用例一次')

    def test_the_cleanup_is_registered(self):
        self.assertIn('addCleanup(config.invalidate_global_proxy)', self.src,
                      '没有登记 cleanup，本案的缓存会泄露给下一个用例')


class TheRootCauseIsDocumentedTests(unittest.TestCase):
    def setUp(self):
        self.src = TEST_FILE.read_text(encoding="utf-8")

    def test_it_names_the_cache_and_its_window(self):
        start = self.src.find('invalidate_global_proxy')
        window = self.src[max(0, start - 900):start + 200]
        self.assertIn('2 秒', window, '要写明缓存窗口，否则看不出为什么单独跑能过')

    def test_it_records_that_the_production_path_invalidates(self):
        start = self.src.find('invalidate_global_proxy')
        window = self.src[max(0, start - 900):start + 200]
        self.assertIn('生产', window,
                      '要说明生产路径本来就会失效，测试补的是同一个动作')

    def test_it_says_the_symptom_was_misleading(self):
        """`source` 对、`effective` 空 —— 这个分裂是定位的关键线索，必须写下来。"""
        start = self.src.find('invalidate_global_proxy')
        window = self.src[max(0, start - 900):start + 800]
        self.assertIn('effective', window)


class TheProductionCacheIsUntouchedTests(unittest.TestCase):
    """修的是测试，不是缓存本身。"""

    def test_ttl_is_still_two_seconds(self):
        from server import config
        self.assertEqual(config._GLOBAL_PROXY_TTL, 2.0,
                         '不该为了让测试通过而改生产参数')

    def test_invalidate_still_bumps_the_generation(self):
        from server import config
        before = config._GLOBAL_PROXY_GENERATION
        config.invalidate_global_proxy()
        self.assertEqual(config._GLOBAL_PROXY_GENERATION, before + 1)


if __name__ == '__main__':
    unittest.main()
