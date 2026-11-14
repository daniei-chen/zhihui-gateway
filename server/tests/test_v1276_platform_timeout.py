"""v1.2.76：按平台设超时 —— qoder 不可用时要一分钟内可见，而不是让调用方干等。

## 怎么发现的

对比三个平台的参数行为时，`qoder` 的请求**挂了 25 秒没有任何响应**，
连健康检查都是 200。

查它的日志：

```
[qd-proxy] request error: model=glm-5.3-flash 183509ms status=502
           msg=upstream rate limit: {"code":"403", ... "10605" ...}
```

**183 秒才回 502。** 而它只有**一个**上游账号（`/opt/qoder-hub/accounts/` 下
只有一个 `.json`），那次被限流了。

## 为什么这是网关该管的事

网关的超时是 `UPSTREAM_TIMEOUT=300`、首字节 240 秒。所以它老实等 3 分钟 ——
而调用方（nginx、浏览器、SDK 默认）早就在 60 秒左右放弃了。

**用户看到的是「卡住」，不是「qoder 不可用」。**

## 为什么不能整体下调

workbuddy 侧实测有 **158 次首字节超过 60 秒**（长 prompt 的固有 prefill），
最长 186 秒。那是正常业务，一刀切会切断它。

## 所以按平台分

qoder 自己的历史延迟：近 7 天 43 次，平均 **2.2 秒**、最大 **12.8 秒**。
60 秒窗口完全够用，同时让不可用在一分钟内变成可见错误。
"""
from __future__ import annotations

import inspect
import unittest

from server import config
from server.routers import gateway


class PlatformTimeoutConfigTests(unittest.TestCase):
    def test_qoder_has_a_shorter_timeout(self):
        self.assertIn('qoder', config.PLATFORM_TIMEOUT)
        self.assertLess(config.PLATFORM_TIMEOUT['qoder'], config.UPSTREAM_TIMEOUT,
                        'qoder 的窗口必须比全局更短，否则这个改动没有意义')

    def test_qoder_window_is_still_generous(self):
        """它历史上最大 12.8 秒，60 秒要留够余量。"""
        self.assertGreaterEqual(config.PLATFORM_TIMEOUT['qoder'], 30)

    def test_other_platforms_are_not_affected(self):
        for platform in ('workbuddy', 'trae'):
            self.assertNotIn(platform, config.PLATFORM_TIMEOUT,
                             f'{platform} 不该被这个改动波及')

    def test_the_table_is_overridable_by_env(self):
        """运维要能在不改代码的情况下调。"""
        source = (inspect.getsource(config))
        start = source.index('PLATFORM_TIMEOUT')
        window = source[start:start + 300]
        self.assertIn('WB_QODER_TIMEOUT', window)


class RoutingUsesThePlatformTimeoutTests(unittest.TestCase):
    SOURCE = inspect.getsource(gateway._chat)

    def test_it_looks_up_the_platform_table(self):
        self.assertIn('config.PLATFORM_TIMEOUT', self.SOURCE)

    def test_it_falls_back_to_the_global_timeout(self):
        """表里没有的平台必须回到全局值，不能变成 0 或 None。"""
        start = self.SOURCE.index('config.PLATFORM_TIMEOUT')
        window = self.SOURCE[start:start + 260]
        self.assertIn('config.UPSTREAM_TIMEOUT', window)

    def test_it_uses_the_resolved_platform_not_the_requested_one(self):
        """要用路由**之后**的平台 —— 请求里的前缀可能与实际路由不同。"""
        start = self.SOURCE.index('config.PLATFORM_TIMEOUT')
        window = self.SOURCE[max(0, start - 900):start]
        self.assertIn('actual_platform', window)

    def test_the_timeout_reaches_the_budget(self):
        start = self.SOURCE.index('_platform_timeout')
        window = self.SOURCE[start:start + 400]
        self.assertIn('request_budgets', window)


class TheFallbackTableWorksTests(unittest.TestCase):
    """按真实查表逻辑验证一遍，别只检查源码里有没有那行字。"""

    def _pick(self, platform):
        return config.PLATFORM_TIMEOUT.get(platform or '', config.UPSTREAM_TIMEOUT)

    def test_qoder_gets_its_own_window(self):
        self.assertEqual(self._pick('qoder'), config.PLATFORM_TIMEOUT['qoder'])

    def test_others_get_the_global_window(self):
        for platform in ('workbuddy', 'trae', '', None, 'unknown'):
            self.assertEqual(self._pick(platform), config.UPSTREAM_TIMEOUT)


if __name__ == '__main__':
    unittest.main()
