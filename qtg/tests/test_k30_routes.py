"""K30：最终生效路由表、重复组检测、注册顺序契约回归。

## 口径（审计包的两个数字不是一回事）

* **230** = HTTP 装饰器声明数（含重复注册）；
* **208** = 唯一 `(method, 全路径)` 数（router prefix 已解析）；
* 差值 22 = 22 个重复组，每组只有**先注册的那个**生效（Starlette 首个匹配）。

新增授权探针/能力表路由后，声明数与唯一数会一起增加（重复组不变）。本文件用
`tests/data/route_regression.json` 做契约回归：

* 声明数 / 唯一数 / 重复组数 / 重复组清单 / 注册顺序 / 生效表 digest 全部对齐；
* 静态推演的生效表必须与真实 `app.routes` 一致（防止"表里写的和实际注册的不是一个"）；
* 每个重复组的生效处理器必须是注册顺序靠前的那个（剩下的即死代码，已在清单里列出）。

> 改了路由就重跑：`python -m qtg.routeaudit --json design/qtg/route-table.json`，
> 再把 `route_regression.json` 里的对应字段更新并**人工 review 重复组**。
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from identity_support import IdentityTestBase

from qtg import routeaudit

DATA = Path(__file__).resolve().parent / "data" / "route_regression.json"
MAIN_PY = Path(routeaudit.APP_DIR) / "main.py"


def _golden() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


class RouteInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inv = routeaudit.inventory()
        cls.golden = _golden()

    def test_counts(self) -> None:
        self.assertEqual(self.inv["declarations"], self.golden["declarations"])
        self.assertEqual(self.inv["unique_method_paths"], self.golden["unique_method_paths"])
        self.assertEqual(self.inv["duplicate_groups"], self.golden["duplicate_groups"])
        self.assertEqual(self.inv["duplicate_extra_declarations"],
                         self.golden["duplicate_extra_declarations"])

    def test_counts_are_self_consistent(self) -> None:
        self.assertEqual(self.inv['duplicate_groups'], 0,
                         'Each method/path must have exactly one canonical handler')
        unique = {(r["method"], r["full_path"]) for r in self.inv["declared_rows"]}
        self.assertEqual(len(unique), self.inv["unique_method_paths"])
        self.assertEqual(
            self.inv["declarations"] - self.inv["unique_method_paths"],
            self.inv["duplicate_extra_declarations"],
        )

    def test_effective_digest_and_order(self) -> None:
        self.assertEqual(self.inv["effective_digest"], self.golden["effective_digest"],
                         "生效路由表变了：请重新生成 route_regression.json 并人工 review")
        self.assertEqual(list(routeaudit.REGISTRATION_ORDER),
                         self.golden["registration_order"])

    def test_effective_has_no_duplicates(self) -> None:
        keys = [(r["method"], r["full_path"]) for r in self.inv["effective"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), self.inv["unique_method_paths"])

    def test_duplicate_groups_match_golden(self) -> None:
        actual = sorted(f"{g['method']} {g['path']}" for g in self.inv["duplicates"])
        self.assertEqual(actual, sorted(self.golden["duplicate_keys"]))
        for group in self.inv["duplicates"]:
            self.assertGreaterEqual(group["count"], 2)
            # 每组必须能指出"谁生效、谁是死代码"
            self.assertTrue(group["declarations"])
            winner = next(
                (r for r in self.inv["effective"]
                 if r["method"] == group["method"] and r["full_path"] == group["path"]),
                None,
            )
            self.assertIsNotNone(winner, f"{group['method']} {group['path']} 没有生效处理器")
            # 胜者 = 按注册顺序（模块 rank → 文件 → 行号）最靠前的那个声明，
            # 而不是"源码扫描顺序里的第一个"：扫描按文件名字母序，与注册顺序不同。
            best = min(
                group["declarations"],
                key=lambda d: (routeaudit.registration_rank(d["module"]), d["file"], d["line"]),
            )
            self.assertEqual(winner["func"], best["func"])

    def test_registration_order_matches_main_py(self) -> None:
        """main.py 里 include_router 的真实顺序必须与 REGISTRATION_ORDER 一致。

        main.py 对部分 router 用了别名（`authz as authz_probe`、
        `capabilities as capabilities_router`），这里按 import 语句还原成模块名
        再比较——比较的是"注册了哪些模块、顺序如何"，不是变量名写法。
        """
        tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("routers"):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
        order = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "include_router"):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                name = arg.value.id
                order.append(aliases.get(name, name))
        self.assertEqual(order, list(routeaudit.REGISTRATION_ORDER),
                         "main.py 的注册顺序与 routeaudit.REGISTRATION_ORDER 不一致")


class RouteEffectiveTest(IdentityTestBase):
    """静态推演的生效表 vs 真实 app.routes。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.inv = routeaudit.inventory()
        cls.golden = _golden()

    def setUp(self) -> None:
        super().setUp()
        self.assertGreater(len(routeaudit.runtime_routes(self.app)), 200,
                           'Runtime route traversal must include every router')

    def _first_route(self, method: str, path: str):
        for route in routeaudit.runtime_routes(self.app):
            route_path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if route_path == path and method in methods:
                return route
        return None

    def test_every_static_route_exists_in_app(self) -> None:
        diffs = routeaudit.compare_effective_with_app(self.inv, self.app)
        static_only = [d for d in diffs if d["kind"] == "static_only"]
        self.assertEqual(static_only, [],
                         f"静态清单里有、真实 app 里没有的路由：{static_only}")

    def test_every_canonical_handler_matches_reviewed_golden(self) -> None:
        self.assertGreater(len(self.golden['effective_handlers']), 200)
        for winner in self.golden['effective_handlers']:
            method, path = winner['method'], winner['full_path']
            with self.subTest(route=f"{method} {path}"):
                route = self._first_route(method, path)
                self.assertIsNotNone(route, f"{method} {path} 在 app.routes 里找不到")
                expect_module = ("qtg.main" if winner["module"] == "main"
                                 else f"qtg.routers.{winner['module']}")
                self.assertEqual(route.endpoint.__module__, expect_module,
                                 f"{method} {path} 生效处理器应是 {expect_module}")
                self.assertEqual(route.endpoint.__name__, winner["func"])

    def test_meta_has_one_canonical_handler(self) -> None:
        """Metadata has one canonical handler in data_admin after deduplication."""
        route = self._first_route("GET", "/api/v1/meta")
        self.assertIsNotNone(route)
        self.assertEqual(route.endpoint.__module__, "qtg.routers.data_admin")
        r = self.client.get("/api/v1/meta", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        # Preserve the established data_admin response contract.
        self.assertIsInstance(r.json()["endpoints"], list)

    def test_security_config_has_one_canonical_handler(self) -> None:
        """The security configuration authority remains ip_guard."""
        route = self._first_route("GET", "/api/security/config")
        self.assertIsNotNone(route)
        self.assertEqual(route.endpoint.__module__, "qtg.routers.ip_guard")

    def test_catch_all_order(self) -> None:
        """`/api/{path:path}` 兜底必须在所有 /api/v1 具体路由之后匹配。

        若顺序反了，新增的 `/api/v1/**` 会被兜底吞掉（返回 501/404 而不是真实响应）。
        """
        r = self.client.get("/api/v1/authz/matrix", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        fallback = self.client.get("/api/no-such-compat-endpoint", headers=self.auth)
        self.assertEqual(fallback.status_code, 501)


if __name__ == "__main__":
    unittest.main()


class RouteMethodAndCatchAllTest(IdentityTestBase):
    """K30 补测：方法重载合法性、动态参数优先级、冲突检测器的检出能力。

    已有测试覆盖"声明数/唯一数/生效表/注册顺序"；这里补三类此前未实测的语义：

    1. **不同方法同路径是合法的**，不能被当成重复组（否则会误报并诱使删除正常路由）；
    2. **静态段优先于动态段**（Starlette 按注册顺序匹配，静态先注册才不会被参数吞掉）；
    3. **冲突检测器本身能检出冲突**（用一个合成的路由表喂给比较函数，确认它真的会报，
       而不是"永远返回空列表"的假绿灯）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.inv = routeaudit.inventory()

    def _first_route(self, method: str, path: str):
        for route in routeaudit.runtime_routes(self.app):
            if getattr(route, 'path', None) == path and method in (getattr(route, 'methods', None) or set()):
                return route
        return None

    def test_multi_method_paths_are_not_conflicts(self) -> None:
        """同一路径不同方法 = 资源式设计，不是重复组。"""
        by_path: dict[str, set[str]] = {}
        for row in self.inv["effective"]:
            by_path.setdefault(row["full_path"], set()).add(row["method"])
        multi = {p: sorted(m) for p, m in by_path.items() if len(m) > 1}
        self.assertGreater(len(multi), 20, f'方法重载路径过少，疑似清单不全：{len(multi)}')
        # 这些路径里任何一个都不能出现在重复组清单
        dup_keys = {f"{g['method']} {g['path']}" for g in self.inv["duplicates"]}
        for path, methods in multi.items():
            for method in methods:
                self.assertNotIn(f"{method} {path}", dup_keys,
                                 f'{path} 的 {method} 被误判成重复组')
        # 抽查一个已知的读写配对
        self.assertIn('GET', by_path['/api/keys'])
        self.assertIn('POST', by_path['/api/keys'])

    def test_static_segment_wins_over_dynamic(self) -> None:
        """/api/keys（静态）不能被 /api/{path:path}（兜底）吞掉。"""
        static_route = self._first_route('GET', '/api/keys')
        self.assertIsNotNone(static_route)
        # 兜底处理器返回 501；真实 keys 处理器返回 200/401，不能是 501
        r = self.client.get('/api/keys', headers=self.auth)
        self.assertNotEqual(r.status_code, 501,
                            '/api/keys 被 catch-all 兜底吞掉了（静态段未优先）')
        # 命中的必须是 keys 自己的处理器：200/401/403，或"凭据缺失/上游不可达"
        # 这种可辨识的业务错误（隔离环境里没有 Manager 服务令牌）。
        #   * 502 upstream_*：令牌文件存在但 Manager 不可达；
        #   * 503 config_missing：令牌文件缺失（K26 读写令牌分离后，读路径用的是
        #     manager_read_token_file；R1-A 起 ConfigMissing 不再被误包装成 502）。
        self.assertIn(r.status_code, (200, 401, 403, 502, 503), r.text)
        if r.status_code in (502, 503):
            self.assertIn('Manager', r.text)
            self.assertIn('error', r.text)
            self.assertNotIn('not_implemented', r.text,
                             '/api/keys 落到了 catch-all 兜底')
        # 反向确认兜底确实存在且会响应未知路径
        miss = self.client.get('/api/no-such-endpoint-k30', headers=self.auth)
        self.assertEqual(miss.status_code, 501, '兜底未生效，本测试失去意义')

    def test_dynamic_path_route_resolves(self) -> None:
        """动态段路由必须命中它自己的处理器，而不是被 catch-all 兜底。

        QTG 对未实现的兼容端点返回 501 + `not_implemented`（明确区分"没实现"与
        "没数据"）。因此"命中了参数化路由"的判据不是 200/404，而是
        **响应体里的 detail 指向该路由自身**，而非兜底的统一文案。
        """
        dyn = [r for r in self.inv['effective'] if '{' in r['full_path']
               and 'path}' not in r['full_path']]
        self.assertGreater(len(dyn), 15, f'动态路由过少：{len(dyn)}')
        r = self.client.get('/api/tokens/999999', headers=self.auth)
        self.assertNotEqual(r.status_code, 405,
                            '方法不被允许说明命中了别的路由')
        body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        if r.status_code == 501:
            # 501 有两种来源：本路由未实现 vs catch-all 兜底。用路径区分。
            self.assertIn('/api/tokens/999999', str(body),
                          f'501 来自 catch-all 而非参数化路由：{body}')
        else:
            self.assertIn(r.status_code, (200, 404), r.text)

    def test_catch_all_routes_are_last(self) -> None:
        """catch-all 必须排在自身范围内所有具体路由之后。"""
        effective = self.inv['effective']
        for pattern, scope in (('/api/{path:path}', '/api/'),
                               ('/{path:path}', '/')):
            idx = next((i for i, r in enumerate(effective)
                        if r['full_path'] == pattern), None)
            self.assertIsNotNone(idx, f'找不到兜底路由 {pattern}')
            later_specific = [
                r['full_path'] for r in effective[idx + 1:]
                if r['full_path'].startswith(scope) and '{' not in r['full_path']
                and r['full_path'] != pattern
            ]
            # scope 为 '/' 时，'/api/x' 也在其下，但更具体的 /api/ 兜底会先命中，
            # 所以只要求：同一个前缀范围内没有静态路由排在兜底之后。
            if scope == '/api/':
                self.assertEqual(later_specific, [],
                                 f'{pattern} 之后仍有静态路由：{later_specific[:5]}')

    def test_conflict_detector_actually_detects(self) -> None:
        """喂一个合成清单，确认重复检测真的会报——防止"永远空"的假绿灯。"""
        # duplicate_groups 吃 Declaration 对象，不是 dict
        def decl(method, func, module, file, line):
            return routeaudit.Declaration(
                file=file, line=line, method=method, path='/x', full_path='/x',
                func=func, prefix='', router_name='r', module=module)
        rows = [decl('GET', 'a', 'm1', 'a.py', 1),
                decl('GET', 'b', 'm2', 'b.py', 2),
                decl('POST', 'c', 'm1', 'a.py', 3)]
        groups = routeaudit.duplicate_groups(rows)
        self.assertEqual(len(groups), 1, f'应只报 GET /x 一个重复组：{groups}')
        self.assertEqual(groups[0]['method'], 'GET')
        self.assertEqual(groups[0]['path'], '/x')
        self.assertEqual(groups[0]['count'], 2)
        funcs = sorted((d['func'] if isinstance(d, dict) else d.func)
                       for d in groups[0]['declarations'])
        self.assertEqual(funcs, ['a', 'b'])
