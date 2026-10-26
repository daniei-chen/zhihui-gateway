"""K36：QTG 侧安全头与 CSP（在 qtg target 里跑，能看到 qtg 源码）。

与 manager/server/tests/test_k36_security_headers.py 是**同一契约的两个视角**：
那边断言 Manager 的中间件，这边断言 QTG 的中间件并用真实响应验证头确实发出。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from identity_support import IdentityTestBase

# qtg/tests/<this> → qtg/app/qtg/main.py（隔离视图里是 view/qtg/app/qtg/main.py）
QTG_MAIN = Path(__file__).resolve().parents[1] / 'app' / 'qtg' / 'main.py'

REQUIRED_HEADERS = ('X-Content-Type-Options', 'X-Frame-Options',
                    'Referrer-Policy', 'Permissions-Policy')
REQUIRED_CSP = ('default-src', 'object-src', 'frame-ancestors',
                'base-uri', 'form-action')


def _src() -> str:
    return QTG_MAIN.read_text(encoding='utf-8')


def _csp_literal(src: str) -> str:
    start = src.index('"Content-Security-Policy"')
    tail = src[start:src.index('return response', start)]
    return ''.join(chunk for line in tail.splitlines()
                   for chunk in line.split('"')[1::2]
                   if chunk and not chunk.startswith('Content-Security-Policy'))


class QtgHeaderSourceTest(unittest.TestCase):
    def test_all_required_headers_declared(self) -> None:
        src = _src()
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(f'"{header}"', src, f'QTG 未设置 {header}')

    def test_no_unsafe_eval(self) -> None:
        self.assertNotIn("unsafe-eval", _src(),
                         'QTG 的 CSP 不得放开 unsafe-eval')

    def test_csp_has_all_directives(self) -> None:
        csp = _csp_literal(_src())
        self.assertTrue(csp, 'CSP 字面量解析为空')
        for directive in REQUIRED_CSP:
            with self.subTest(directive=directive):
                self.assertIn(directive, csp, f'QTG 的 CSP 缺 {directive}：{csp}')
        # QTG 面板没有内联脚本需求，script-src 不应带 unsafe-inline
        script = re.search(r"script-src([^;]*);", csp)
        self.assertIsNotNone(script, f'CSP 里找不到 script-src：{csp}')
        self.assertNotIn('unsafe-inline', script.group(1),
                         'QTG 的 script-src 不应允许 inline 脚本')

    def test_headers_are_set_before_static_skip(self) -> None:
        """除 CSP 外的头必须设在 `if not _is_static(...)` 之前，静态资源也要有。"""
        src = _src()
        idx = src.index('if not _is_static(request.url.path):')
        before = src[:idx]
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, before,
                              f'{header} 落在静态判断之后，静态资源会缺该头')


class QtgHeaderResponseTest(IdentityTestBase):
    """真实响应上的头（不只是源码里有）。"""

    def test_healthz_carries_security_headers(self) -> None:
        r = self.client.get('/healthz')
        self.assertEqual(r.status_code, 200, r.text)
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, r.headers)
        self.assertIn('Content-Security-Policy', r.headers)

    def test_api_response_carries_security_headers(self) -> None:
        r = self.client.get('/api/v1/meta', headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, r.headers)
        csp = r.headers['Content-Security-Policy']
        for directive in REQUIRED_CSP:
            with self.subTest(directive=directive):
                self.assertIn(directive, csp)

    def test_unauthorized_response_still_carries_headers(self) -> None:
        """401 也必须带头：错误响应不能成为无保护路径。"""
        r = self.client.get('/api/v1/meta')
        self.assertEqual(r.status_code, 401, r.text)
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, r.headers)

    def test_request_id_header_present(self) -> None:
        """x-request-id 是 K47 的关联锚点，必须每个响应都有。"""
        r = self.client.get('/healthz')
        self.assertIn('x-request-id', r.headers)
        self.assertTrue(r.headers['x-request-id'])


if __name__ == '__main__':
    unittest.main()
