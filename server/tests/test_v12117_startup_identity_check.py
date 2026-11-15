"""v1.2.117：让「登录进不去」在启动日志里就能看出来。

## 起因

上一版修掉了一次真实故障：管理员密码正确、登录接口返回 200，但 nginx 的
鉴权子请求读不到身份签名密钥、返回 503，于是**登录成功也进不去面板**。

根因是那条路径的来源被换成了一个**部署里从未设置过的配置项**。

## 这件事的可怕之处

它不是「写错了逻辑」。它是：

| 维度 | 看起来怎样 |
| --- | --- |
| 代码 | 完全正常（复用了更严格的读取实现） |
| 测试 | 全绿（20 项通过） |
| 静态检查 | 没有可疑模式 |
| 改动前的核对 | 「核过生产，文件权限都是 0600」—— 权限确实对 |
| **实际** | **那个配置项的值从未被读到过一次** |

**它只在真实用户登录时，以「进不去」的形式暴露。**

## 路径解析已经修好了

配置项优先、缺省回落默认位置，所以默认不再会断。这一版补的是**剩下那种情形**：
兜底路径本身也读不到（文件没部署、权限被改、内容不合规）。

那种情况下用户看到的仍然是同一句「登录进不去」。**而排查成本极高**——
要先想到去看 nginx 的鉴权子请求日志。

## 本版

启动时读一次密钥，把结论写进**启动日志**：

- 正常：一行 `identity signing key ready (<path>)`
- 异常：一行 WARNING，**点名后果**（登录会成功但进不去）并列出四项检查方向

**只记日志、不阻断启动。** 密钥读不到时面板仍可浏览，只是登录流程走不完；
把它变成启动失败会让一次配置错误升级成一次宕机。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 启动时确实检查 | 又回到「只有用户登录时才知道坏了」 |
| 正常时是 INFO 且带路径 | 没人知道它读的是哪个文件 |
| 异常时是 WARNING 且点名后果 | 又变成一句需要猜的日志 |
| **不阻断启动** | 配置错误升级成宕机 |
"""
from __future__ import annotations

import inspect
import unittest

from server import main


class TheStartupChecksTheKeyTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_the_check_exists(self):
        self.assertIn('identity signing key ready', self.src)

    def test_it_reports_the_resolved_path(self):
        """只报「ready」不报路径，出问题时没人知道读的是哪个文件。"""
        idx = self.src.find('identity signing key ready')
        self.assertIn('%s', self.src[idx:idx + 120])

    def test_it_uses_the_same_resolver_as_the_request_path(self):
        """检查用的路径必须和请求时用的是同一个 —— 否则自检通过、请求失败。"""
        idx = self.src.find('identity signing key ready')
        window = self.src[max(0, idx - 800):idx + 200]
        self.assertIn('identity_secret_path', window)

    def test_it_reuses_the_same_reader(self):
        idx = self.src.find('identity signing key ready')
        window = self.src[max(0, idx - 800):idx + 200]
        self.assertIn('_secret()', window)


class TheWarningNamesTheConsequenceTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_it_says_login_will_appear_to_work(self):
        """关键信息：登录会「成功」，所以别去查密码。"""
        idx = self.src.find('identity signing key UNREADABLE')
        window = self.src[idx:idx + 500]
        self.assertIn('login will succeed', window,
                      '必须点明「登录接口会返回成功」，否则排查者会先怀疑密码')

    def test_it_says_the_shell_cannot_pass(self):
        idx = self.src.find('identity signing key UNREADABLE')
        window = self.src[idx:idx + 500]
        self.assertIn('503', window)

    def test_it_lists_what_to_check(self):
        idx = self.src.find('identity signing key UNREADABLE')
        window = self.src[idx:idx + 500]
        for hint in ('0600', '4096', 'whitespace'):
            self.assertIn(hint, window, '缺少排查线索：%s' % hint)


class ItDoesNotBlockStartupTests(unittest.TestCase):
    """密钥不可读时面板仍可浏览；把它变成启动失败是另一种故障。"""

    def setUp(self):
        self.src = inspect.getsource(main)

    def test_the_check_is_wrapped(self):
        idx = self.src.find('identity signing key ready')
        window = self.src[max(0, idx - 900):idx + 1400]
        self.assertIn('try:', window)
        self.assertIn('except', window)

    def test_it_does_not_raise(self):
        idx = self.src.find('__REPORT__')
        # 定位自检块并确认里面没有 raise
        start = self.src.find('身份签名密钥的启动自检')
        self.assertGreater(start, 0)
        block = self.src[start:start + 2000]
        end = block.find('logger.warning(\'identity signing key check failed\'')
        segment = block[:end] if end > 0 else block
        self.assertNotIn('raise ', segment,
                         '自检失败不该阻断启动')

    def test_the_reason_is_documented(self):
        start = self.src.find('身份签名密钥的启动自检')
        block = self.src[start:start + 1200]
        self.assertIn('不阻断启动', block,
                      '要写明为什么不阻断，否则后人会「顺手」改成抛出')


if __name__ == '__main__':
    unittest.main()
