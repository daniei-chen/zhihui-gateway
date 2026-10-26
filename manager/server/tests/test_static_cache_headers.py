"""K37：Manager 静态资源的缓存响应头（精确值断言）。

## 背景：热缓存 0% 到底是哪一层漏了

`design/K37-K38-budget-a11y.md` 的实测是「热缓存节省 0%」。Lead 预查发现
`manager/server/main.py` 的 `cache_headers` 中间件规则看起来是对的（
`/_next/static/` → immutable），于是先要回答一个问题：**页面到底由谁返回**。

结论（见 `evidence/r2-k37/layer-attribution.md`）：

* 生产与本地 fixture 的 HTML/`_next` 静态文件由 **Nginx 直接 root 返回**，
  不经过 Manager —— 所以 `cache_headers` 中间件在那条路径上根本没机会运行；
* Nginx 侧（`ops/nginx/` 既有模板 + fixture 生成器）对 `/_next/` 只设了 root，
  没有任何 Cache-Control —— 这才是热缓存 0% 的位置（已在 `ops/nginx/`
  新增 map + server 片段，并在 VM 上 `curl -sI` 实测，见
  `evidence/r2-k37/nginx-cache.json`）。

Manager 这条路径**本来正确**，但一直缺回归保护：改中间件、加新的静态路由、
把静态服务从 mount 换成自定义路由，都可能悄悄丢掉这些头。本文件把三类资源的
头钉成**精确值**（不是"包含 immutable 即可"），并同时断言安全头没有被缓存逻辑
挤掉（N37 那个 `add_header` 覆盖坑在 Manager 侧的等价风险是：中间件 `return`
太早，安全头没写）。

## 为什么用真实产物里的文件

`/_next/static/**` 由 `app.mount('/_next', StaticFiles(...))` 提供，目录在
import main 时就固定了。测试直接遍历隔离视图里的真实构建产物找一个 `.js`，
不构造假目录 —— 否则测的是"假 StaticFiles"而不是线上的那条 mount。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server import config
from server.main import app

IMMUTABLE = 'public, max-age=31536000, immutable'
NO_STORE = 'no-store, no-cache, must-revalidate'


class StaticCacheHeaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.static = Path(config.STATIC_DIR)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    # ── helpers ────────────────────────────────────────────────────────
    def _first_static_asset(self, suffix: str) -> str | None:
        """返回 `/_next/static/...` 下第一个真实产物的 URL 路径。"""
        root = self.static / '_next' / 'static'
        if not root.is_dir():
            return None
        for path in sorted(root.rglob(f'*{suffix}')):
            if path.is_file():
                return '/_next/static/' + path.relative_to(root).as_posix()
        return None

    def _assert_security_headers(self, headers) -> None:
        self.assertEqual(headers.get('x-content-type-options'), 'nosniff')
        self.assertEqual(headers.get('x-frame-options'), 'DENY')
        self.assertEqual(headers.get('referrer-policy'), 'no-referrer')
        self.assertIn('default-src', headers.get('content-security-policy', ''))

    # ── `_next/static/**`（含内容哈希）→ 一年 immutable ─────────────────
    def test_next_static_js_is_immutable(self) -> None:
        url = self._first_static_asset('.js')
        if url is None:
            self.skipTest('隔离视图里没有构建产物（未跑 web build）')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        self.assertEqual(response.headers.get('cache-control'), IMMUTABLE)
        self._assert_security_headers(response.headers)

    def test_next_static_css_and_font_are_immutable(self) -> None:
        checked = 0
        for suffix in ('.css', '.woff2'):
            url = self._first_static_asset(suffix)
            if url is None:
                continue
            checked += 1
            with self.subTest(kind=suffix):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, url)
                self.assertEqual(response.headers.get('cache-control'), IMMUTABLE)
        if checked == 0:
            self.skipTest('隔离视图里没有 css/字体产物')

    # ── HTML 文档 → no-cache（发布后必须立刻拿到新版本）─────────────────
    def test_html_document_is_revalidated(self) -> None:
        response = self.client.get('/', headers={'accept': 'text/html'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('cache-control'), 'no-cache')
        self._assert_security_headers(response.headers)

    def test_nested_page_html_is_revalidated(self) -> None:
        response = self.client.get('/dashboard/', headers={'accept': 'text/html'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('cache-control'), 'no-cache')

    # ── RSC / 导航 payload（.txt）→ no-cache ───────────────────────────
    def test_rsc_payload_is_not_cached(self) -> None:
        root = self.static
        candidate = None
        for path in sorted(root.rglob('*.txt')):
            if path.is_file():
                candidate = path
                break
        if candidate is None:
            self.skipTest('隔离视图里没有 .txt 导航 payload')
        url = '/' + candidate.relative_to(root).as_posix()
        response = self.client.get(url, headers={'rsc': '1', 'accept': '*/*'})
        self.assertEqual(response.status_code, 200, url)
        self.assertEqual(response.headers.get('cache-control'), 'no-cache')

    # ── 图标/字体等稳定资源 → 一天 ─────────────────────────────────────
    def test_favicon_is_cached_one_day(self) -> None:
        if not (self.static / 'favicon.ico').is_file():
            self.skipTest('隔离视图里没有 favicon.ico')
        response = self.client.get('/favicon.ico')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('cache-control'), 'public, max-age=86400')

    def test_png_under_favicon_is_cached_one_day(self) -> None:
        target = None
        for path in sorted((self.static / 'favicon').rglob('*.png')):
            if path.is_file():
                target = path
                break
        if target is None:
            self.skipTest('隔离视图里没有 favicon/*.png')
        url = '/' + target.relative_to(self.static).as_posix()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        self.assertEqual(response.headers.get('cache-control'), 'public, max-age=86400')

    # ── API / 指标 → 禁止任何缓存 ─────────────────────────────────────
    def test_api_responses_are_no_store(self) -> None:
        response = self.client.get('/api/healthz')
        self.assertIn(response.status_code, (200, 401, 403))
        self.assertEqual(response.headers.get('cache-control'), NO_STORE)
        self.assertEqual(response.headers.get('pragma'), 'no-cache')

    # ── 反向断言：静态资源的头不能"顺手"变成 no-store ──────────────────
    def test_static_and_api_do_not_share_policy(self) -> None:
        url = self._first_static_asset('.js')
        if url is None:
            self.skipTest('隔离视图里没有构建产物')
        static = self.client.get(url).headers.get('cache-control')
        api = self.client.get('/api/healthz').headers.get('cache-control')
        self.assertNotEqual(static, api)
        self.assertEqual(static, IMMUTABLE)
        self.assertEqual(api, NO_STORE)


if __name__ == '__main__':
    unittest.main()
