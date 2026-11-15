"""v1.2.110：数据面那条 IP 提取路径会无条件采信伪造的转发头。

## 发现经过

上一版修掉「同一凭据三套读取标准」后，继续找「同一件事多份实现」。这次查的是
**取客户端 IP**：

| 位置 | 用法 |
| --- | --- |
| `iputil.client_ip()` | **54 处**（正规入口，做可信代理校验） |
| `qtgcore/routers/proxy.py::_client_ip` | 数据面自己一套 |
| 其它直接读 `request.client.host` | 6 处 |

那个自成一体的实现是这样写的：

```
xr = request.headers.get("x-real-ip");  if xr: return xr          # 无条件采信
xf = request.headers.get("x-forwarded-for"); if xf: return xf.split(",")[0]  # 取最左
```

## 为什么这是漏洞形态

`iputil.client_ip()` 的文档里写着一段历史：**这个形态曾被实测绕过**。

转发头是客户端可伪造的普通头。反代用 `$remote_addr` 覆写 `X-Real-IP` 只成立于
「请求确实经过那个反代」；服务一旦直接可达，加一行 `X-Real-IP: 9.9.9.9`
就能冒充任意来源。

而那份实现还多错一层：它取 `X-Forwarded-For` 的**最左侧**，那正是客户端自己写的
那段；右侧才是反代追加的真实地址。

**这个结果直接喂给 `_ip_allowed()`，也就是密钥的 IP 白名单。**

## 当前可达性：不可达

2026-11-15 核对：该模块**没有被任何路由使用** —— `@router.` 零匹配、`_auth_key`
零引用、`router` 从未被 include。

**所以漏洞当前不可达。** 但「暂时不可达」不是安全边界：将来谁把它挂上去，
绕过就随之生效。修在这里，而不是等它被启用。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 伪造 `X-Real-IP` 无效 | IP 白名单可被一个请求头绕过 |
| 伪造 `X-Forwarded-For` 无效 | 同上 |
| 走 `iputil` 而不是自己解析 | 又出现两套标准 |
| 部署开关对它生效 | `WB_TRUST_PROXY` 管不到这条路径 |
"""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from server import config, iputil
from server.qtgcore.routers import proxy


def _request(headers=None, client=('127.0.0.1', 1234)):
    class _Client:
        host = client[0] if client else None

    class _Req:
        pass

    req = _Req()
    req.headers = headers or {}
    req.client = _Client() if client else None
    return req


def _code_lines(func) -> list[str]:
    """去掉 docstring 与注释后的源码行。"""
    source = inspect.getsource(func)
    lines = source.splitlines()
    out = []
    in_doc = False
    for line in lines:
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        out.append(line)
    return out


class ForgedHeadersDoNotWorkTests(unittest.TestCase):
    """核心断言：伪造来源头不能改变 IP 白名单看到的地址。"""

    def test_forged_x_real_ip_is_ignored_when_proxy_untrusted(self):
        with mock.patch.object(config, 'TRUST_PROXY', False):
            got = proxy._client_ip(_request({'x-real-ip': '9.9.9.9'}))
        self.assertEqual(got, '127.0.0.1',
                         '不可信来源的 X-Real-IP 必须被忽略')

    def test_forged_x_forwarded_for_is_ignored_when_proxy_untrusted(self):
        with mock.patch.object(config, 'TRUST_PROXY', False):
            got = proxy._client_ip(_request({'x-forwarded-for': '9.9.9.9'}))
        self.assertEqual(got, '127.0.0.1')

    def test_forged_header_from_untrusted_peer_is_ignored(self):
        """即使开了 TRUST_PROXY，对端不在可信网段时也不能采信转发头。"""
        with mock.patch.object(config, 'TRUST_PROXY', True), \
                mock.patch.object(iputil, '_is_trusted_proxy', return_value=False):
            got = proxy._client_ip(_request({'x-real-ip': '9.9.9.9'}, client=('203.0.113.7', 1)))
        self.assertEqual(got, '203.0.113.7',
                         '来自不可信对端的转发头必须被忽略')

    def test_trusted_proxy_header_is_honoured(self):
        """反过来也要对：可信反代写进来的真实地址必须被采用。"""
        with mock.patch.object(config, 'TRUST_PROXY', True), \
                mock.patch.object(iputil, '_is_trusted_proxy', return_value=True):
            got = proxy._client_ip(_request({'x-real-ip': '198.51.100.9'}))
        self.assertEqual(got, '198.51.100.9')


class XffRightToLeftTests(unittest.TestCase):
    def test_takes_the_rightmost_hop_not_the_leftmost(self):
        """最左侧是客户端自己写的；右侧才是反代追加的真实地址。"""
        with mock.patch.object(config, 'TRUST_PROXY', True), \
                mock.patch.object(config, 'TRUSTED_PROXY_HOPS', 1), \
                mock.patch.object(iputil, '_is_trusted_proxy', return_value=True):
            got = proxy._client_ip(_request({
                'x-forwarded-for': '9.9.9.9, 198.51.100.9',
            }))
        self.assertEqual(got, '198.51.100.9',
                         '取最左侧就等于让客户端自己指定 IP')


class ItDelegatesTests(unittest.TestCase):
    def test_source_has_no_hand_rolled_header_parsing(self):
        """只看代码行 —— docstring 里正在解释原来的错误写法，那是应该出现的。"""
        code = "\n".join(_code_lines(proxy._client_ip))
        for bad in ('x-real-ip', 'x-forwarded-for', ".headers.get"):
            self.assertNotIn(bad, code,
                             '不该在这条路径上自己解析转发头：%s' % bad)

    def test_source_calls_the_shared_parser(self):
        source = inspect.getsource(proxy._client_ip)
        self.assertIn('client_ip(request)', source)

    def test_unreachability_is_recorded(self):
        """「当前不可达」这个事实必须写下来，否则后来人会以为已经出过事。"""
        source = inspect.getsource(proxy._client_ip)
        self.assertIn('不可达', source)

    def test_it_says_why_fixing_now_matters(self):
        source = inspect.getsource(proxy._client_ip)
        self.assertIn('安全边界', source)


if __name__ == '__main__':
    unittest.main()
