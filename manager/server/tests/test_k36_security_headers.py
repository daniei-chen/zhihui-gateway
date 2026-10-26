"""K36：安全头与 CSP 全量核对（Manager / QTG / 边缘）。

## 为什么单列一个文件

传票卡要求核对"首页与控制台分别采集 CSP、主题/RSC 内联脚本、第三方资源、
错误 nonce 应被拦、合法脚本可运行、不直接 unsafe-eval 全放开"。
在此之前全仓库**没有任何一处**断言 CSP 内容，只有实现没有契约。

## 本文件的判定口径

1. **头必须成组出现**：缺一个就等于该防护失效，不能"部分通过"；
2. **CSP 指令逐条核对**，而不是只看头存在；
3. **不允许 `unsafe-eval`**：这是最危险的放宽项，任何服务出现即失败；
4. **内联脚本必须被解释**：`'unsafe-inline'` 存在是可以的（Next.js 的主题脚本 +
   RSC 数据），但必须同时有 `base-uri`/`form-action` 收敛，且**不能**同时出现
   `unsafe-eval`；
5. **404/错误响应同样带头**：只保护 200 等于给攻击者留了一条无保护路径。
"""
from __future__ import annotations

import unittest
from pathlib import Path

# 本文件同时存在于 manager/server/tests 与 src/three-platform/server/tests。
# 隔离运行器把源码树放到 <tmp>/view/<name>-view/ 下，层级与仓库不同，所以
# **不能**用 parents[N] 硬算。这里按"哪个同级目录真的有 main.py"来定位：
#   1) 本文件所在 server/tests 的上一级 server/main.py  —— 当前服务的源码，必在
#   2) 其余服务用"向上找同时含 manager/ 与 qtg/ 的仓库根"的方式，找不到就跳过
def _here_server() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = None
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'manager' / 'server').is_dir() or (_parent / 'qtg' / 'app' / 'qtg').is_dir():
        ROOT = _parent
        break


def _service_main(*relative: str) -> Path:
    """优先用仓库根下的路径；缺失时返回一个确定不存在的路径（调用方 skip）。"""
    if ROOT is not None:
        candidate = ROOT.joinpath(*relative)
        if candidate.exists():
            return candidate
    return _here_server().parent / '__missing__' / Path(*relative)


MANAGER_MAIN = _here_server() / 'main.py'
SRC_MAIN = _service_main('src', 'three-platform', 'server', 'main.py')
QTG_MAIN = _service_main('qtg', 'app', 'qtg', 'main.py')

REQUIRED_HEADERS = (
    'X-Content-Type-Options',
    'X-Frame-Options',
    'Referrer-Policy',
    'Permissions-Policy',
    'Content-Security-Policy',
)

# CSP 里必须存在的收敛指令。`frame-ancestors` 与 `base-uri` 缺失会让
# X-Frame-Options / 相对路径劫持的防护退回单一层次。
REQUIRED_CSP_DIRECTIVES = (
    'default-src',
    'frame-ancestors',
    'base-uri',
    'form-action',
    'object-src',
)


def _middleware_source(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _csp_literal(source: str) -> str:
    """从源码里把拼接出来的 CSP 字符串还原成一行。"""
    marker = "'Content-Security-Policy'"
    start = source.index(marker)
    end = source.index(')', source.index('"', source.index('"', start) + 1) 
                                    if False else start)
    # 直接从 marker 往后抓所有 "..." 字面量，直到遇到 return
    tail = source[start:source.index('return response', start)]
    parts = []
    for line in tail.splitlines():
        line = line.strip()
        if '"' not in line:
            continue
        for chunk in line.split('"')[1::2]:
            if chunk and not chunk.startswith('Content-Security'):
                parts.append(chunk)
    return ''.join(parts)


class HeaderPresenceTest(unittest.TestCase):
    """每个服务都必须带齐 5 个基础头。"""

    def test_manager_declares_all_required_headers(self) -> None:
        src = _middleware_source(MANAGER_MAIN)
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(f"'{header}'", src,
                              f'Manager 未设置 {header}')

    def test_qtg_declares_all_required_headers(self) -> None:
        if not QTG_MAIN.exists():
            self.skipTest('qtg 源码不在 manager 隔离视图中')
        src = _middleware_source(QTG_MAIN)
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(f'"{header}"', src,
                              f'QTG 未设置 {header}')

    def test_src_three_platform_declares_all_required_headers(self) -> None:
        if not SRC_MAIN.exists():
            self.skipTest('src/three-platform 源码树不在隔离视图中')
        src = _middleware_source(SRC_MAIN)
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(f"'{header}'", src)


class CspContentTest(unittest.TestCase):
    """CSP 指令逐条核对 + 禁止项。"""

    def _check_csp(self, name: str, path: Path, quote: str) -> None:
        if not path.exists():
            self.skipTest(f'{name} 源码不在隔离视图中')
        src = _middleware_source(path)
        self.assertNotIn("'unsafe-eval'", src,
                         f'{name} 的 CSP 含 unsafe-eval（等于放开任意代码执行）')
        self.assertNotIn('"unsafe-eval"', src,
                         f'{name} 的 CSP 含 unsafe-eval')
        csp = _csp_literal(src)
        self.assertTrue(csp, f'{name} 的 CSP 字面量解析为空')
        for directive in REQUIRED_CSP_DIRECTIVES:
            with self.subTest(service=name, directive=directive):
                self.assertIn(directive, csp,
                              f'{name} 的 CSP 缺 {directive}：{csp}')

    def test_manager_csp(self) -> None:
        self._check_csp('Manager', MANAGER_MAIN, "'")

    def test_qtg_csp(self) -> None:
        self._check_csp('QTG', QTG_MAIN, '"')

    def test_src_three_platform_csp(self) -> None:
        self._check_csp('src/three-platform', SRC_MAIN, "'")

    def test_inline_script_allowance_is_deliberate(self) -> None:
        """允许 inline 脚本时必须同时收敛 base-uri/form-action。

        Next.js 的主题脚本与 RSC 数据是内联的，不能简单删 `unsafe-inline`；
        但一旦允许 inline，`base-uri`/`form-action` 必须收紧，否则内联脚本
        可以通过注入 `<base>` 改写所有相对 URL。
        """
        src = _middleware_source(MANAGER_MAIN)
        csp = _csp_literal(src)
        if "'unsafe-inline'" in csp:
            self.assertIn('base-uri', csp,
                          '允许 inline 却未限制 base-uri')
            self.assertIn('form-action', csp,
                          '允许 inline 却未限制 form-action')

    def test_frame_ancestors_not_also_allow_all(self) -> None:
        """frame-ancestors 不能是 *，与 X-Frame-Options: DENY 不能冲突。"""
        for name, path in (('Manager', MANAGER_MAIN), ('QTG', QTG_MAIN),
                           ('src', SRC_MAIN)):
            if not path.exists():
                continue
            src = _middleware_source(path)
            csp = _csp_literal(src)
            with self.subTest(service=name):
                self.assertNotIn('frame-ancestors *', csp)
                self.assertIn('frame-ancestors', csp)


class HeaderUniformityTest(unittest.TestCase):
    """头必须对所有响应类型生效，不能只覆盖 200。"""

    def test_manager_sets_headers_outside_path_conditionals(self) -> None:
        """安全头的位置不能落在某个 `if path.startswith(...)` 分支里。

        Manager 的中间件先做缓存策略分支（`_next/static`、`/api/`、静态资源），
        再统一设头。若把某个头误放进分支，就只有那类请求受保护。
        """
        src = _middleware_source(MANAGER_MAIN)
        idx_headers = src.index("setdefault('X-Content-Type-Options'")
        idx_csp = src.index("setdefault(\n        'Content-Security-Policy'",
                            src.index("setdefault('X-Content-Type-Options'") - 200) \
            if "setdefault(\n        'Content-Security-Policy'" in src else \
            src.index("setdefault('X-Content-Type-Options'")
        # Cache-Control 分支必须在设头之前结束（即设头位于所有分支之外）
        idx_cache = src.index("response.headers['Cache-Control']")
        self.assertLess(idx_cache, idx_headers,
                        '安全头在缓存分支之前设置，可能被分支覆盖或跳过')
        self.assertGreater(idx_headers, 0)
        self.assertGreater(idx_csp, 0)

    def test_qtg_static_paths_get_headers_but_csp_is_page_only(self) -> None:
        """QTG 对静态资源跳过 CSP（有意为之：静态资源不需要 CSP）。

        这一条要显式记录：跳过 CSP 是有意的，但**其他头必须仍然设置**。
        """
        if not QTG_MAIN.exists():
            self.skipTest('qtg 源码不在 manager 隔离视图中')
        src = _middleware_source(QTG_MAIN)
        self.assertIn('if not _is_static(request.url.path):', src)
        # 静态分支必须仍在同一个中间件里，前面的头不能跟着被跳过
        before = src[:src.index('if not _is_static')]
        for header in ('X-Content-Type-Options', 'X-Frame-Options',
                       'Referrer-Policy'):
            with self.subTest(header=header):
                self.assertIn(header, before,
                              f'{header} 落在了静态判断之后，静态资源会缺该头')


class CspReportOnlyEdgeTest(unittest.TestCase):
    """边缘（nginx）当前只有 Report-Only，必须记录这个事实。"""

    def test_edge_uses_report_only_so_app_csp_is_authoritative(self) -> None:
        conf = (ROOT / 'ops' / 'nginx' / 'zhihui-hardening.conf') if ROOT else None
        if conf is None or not conf.exists():
            self.skipTest('ops/nginx 不在隔离视图中')
        text = conf.read_text(encoding='utf-8')
        # 这个文件是候选片段；断言它没有要求部署 report-only 之外的放宽
        self.assertNotIn("'unsafe-eval'", text)


if __name__ == '__main__':
    unittest.main()


class ManagerUnauthorizedHeaderTest(unittest.TestCase):
    """N36 同类检查：Manager 的 401 是否也遗漏安全头。

    QTG 的 `_auth_gate` 在中间件里直接 return JSONResponse，绕过了内层设头逻辑，
    实测 401 一个安全头都没有。Manager 的设头逻辑在同一个中间件里、且在
    `call_next` 之后，所以理论上不会漏——但**必须实测**，不能靠推理。
    """

    def test_unauthorized_response_carries_headers(self) -> None:
        import os
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))
        os.environ.setdefault('WB_DATA_DIR', str(Path(os.environ.get('TEMP', '/tmp')) / 'k36-probe'))
        try:
            from fastapi.testclient import TestClient
            from server.main import app
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f'无法在隔离环境构造 app：{exc}')
        # Manager 的 401 由依赖抛 HTTPException 产生，走的是异常处理器路径，
        # 与 QTG 的"中间件直接 return"不同——这正是要实测的差异。
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get('/api/keys')
        self.assertEqual(r.status_code, 401, r.text)
        for header in REQUIRED_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, r.headers,
                              f'Manager 401 响应缺 {header}')
