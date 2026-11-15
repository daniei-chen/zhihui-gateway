"""v1.2.129：同源判定必须是**全等**，不是后缀也不是子域。

## 为什么补这个

现有的 Origin 校验测试覆盖已经不错：跨站、`null`、多重 Origin、跨站 referer、跨站 logout
各有用例。

**但它们没有一条能区分「全等比较」和「后缀匹配」。**

源码注释写着这条判定**不做子串匹配**，可这四个字没有测试撑着：

- 如果哪天有人把它改成 `host.endswith(expected)`，现有测试**全绿**
- 而那种写法会让 `kaogong.art.evil.example` 通过

**这就是「注释承诺了、测试没管」的典型缺口。**

## 补的四类混淆

| 形态 | 例子 | 如果判错会怎样 |
| --- | --- | --- |
| **后缀** | `testserver.evil.example` | 攻击者注册 `x.evil.example` 就能通过 |
| **子域** | `evil.testserver` | 任何能控制子域的人都能发起「同源」请求 |
| **端口** | `testserver:8080` | 同主机上的另一个服务可以冒用身份 |
| **scheme** | `https://` 打 http 源 | 降级请求被当成本站 |

## 这四处都是「看起来像、实际不是」

判定必须逐段比较 scheme / host / port，任何一段不同就是不同源。
**用字符串包含或前缀后缀去判，是最容易出错的写法** —— 而且错了之后
现有的一批测试不会发现。

## 顺带说明本测试的定位

它**不重复**已有的那些用例（跨站/`null`/多重 Origin 已经覆盖得很好了）。
它只补「同源判定的**边界形态**」这一类，正是前面那批测试留下的空白。
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from server.routers import auth


class OriginMustMatchExactlyTests(unittest.TestCase):
    """判定必须是全等 —— 这四条各自能区分「全等」与「包含/后缀/前缀」。"""

    def setUp(self):
        app = FastAPI()
        app.include_router(auth.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        patcher = mock.patch.object(
            auth, '_complete_login', return_value=JSONResponse({'ok': True}))
        self.verify = patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, origin: str):
        return self.client.post('/api/login', json={'username': 'u', 'password': 'p'},
                                headers={'origin': origin})

    def test_suffix_is_not_the_same_origin(self):
        """`testserver.evil.example` 以本站名字开头，但它不是本站。"""
        response = self._post('https://testserver.evil.example')
        self.assertEqual(response.status_code, 403,
                         '后缀形态被放行了 —— 说明判定在做前缀匹配')
        self.verify.assert_not_called()

    def test_subdomain_is_not_the_same_origin(self):
        """`evil.testserver` 是子域，不是本站。"""
        response = self._post('http://evil.testserver')
        self.assertEqual(response.status_code, 403,
                         '子域被放行了 —— 说明判定在做后缀匹配')
        self.verify.assert_not_called()

    def test_port_must_match(self):
        """同主机不同端口是不同源。"""
        response = self._post('http://testserver:8080')
        self.assertEqual(response.status_code, 403,
                         '不同端口被放行了 —— 说明判定忽略了端口')
        self.verify.assert_not_called()

    def test_scheme_must_match(self):
        """scheme 不同则源不同。"""
        response = self._post('https://testserver')
        self.assertEqual(response.status_code, 403,
                         '不同 scheme 被放行了 —— 说明判定忽略了协议')
        self.verify.assert_not_called()


class TheRejectionIsLabelledTests(unittest.TestCase):
    """被拒时要说明原因，别只给一个 403。"""

    def setUp(self):
        app = FastAPI()
        app.include_router(auth.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        patcher = mock.patch.object(
            auth, '_complete_login', return_value=JSONResponse({'ok': True}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_shape_carries_a_reason_header(self):
        for origin in ('https://testserver.evil.example',
                       'http://evil.testserver',
                       'http://testserver:8080',
                       'https://testserver'):
            with self.subTest(origin=origin):
                response = self.client.post(
                    '/api/login', json={}, headers={'origin': origin})
                self.assertEqual(response.status_code, 403)
                reason = response.headers.get('x-manager-csrf-reason', '')
                self.assertTrue(reason, '被拒但没给原因')
                self.assertTrue(reason.startswith('csrf_'),
                                '原因命名不符合既有约定：%s' % reason)


class ExistingGuaranteesStillHoldTests(unittest.TestCase):
    """补测不能把原来能用的路径弄坏。"""

    def setUp(self):
        app = FastAPI()
        app.include_router(auth.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        patcher = mock.patch.object(
            auth, '_complete_login', return_value=JSONResponse({'ok': True}))
        self.verify = patcher.start()
        self.addCleanup(patcher.stop)

    def test_exact_origin_still_passes(self):
        response = self.client.post('/api/login', json={},
                                    headers={'origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200)
        self.verify.assert_called_once()

    def test_non_browser_client_still_passes(self):
        """没有来源头的客户端（CLI / CI）不该被误伤。"""
        response = self.client.post('/api/login', json={})
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
