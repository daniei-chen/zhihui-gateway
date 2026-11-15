"""v1.2.79：把「每条路由都有认证」固化成测试。

## 为什么值得固化

2026-11-15 用 AST 扫了一遍所有路由，确认没有未认证的管理端点。但那是**一次性的
人工核对** —— 下次有人加一个 `@router.get` 忘了带 `Depends(security.require_admin)`，
没有任何东西会红。

这个测试就是那个东西。

## 三类「未加 Depends(security.*) 但安全」的情况

| 类别 | 为什么安全 |
| --- | --- |
| `/v1/*`、`/responses` | 用 **API key** 认证，在函数体内做（`_chat` / `responses_*`） |
| `portal.py` 的用户端 | 用 `current`：同源 + 签名 cookie + 过期 + **会话版本** |
| 登录 / 登出 / 注册 / 健康检查 | 本来就该匿名 |

白名单是**显式**的：新增端点要么带认证依赖，要么加进 `ANONYMOUS_BY_DESIGN`
并写清理由 —— 后者会强制作者想一遍。
"""
from __future__ import annotations

import ast
import pathlib
import unittest

ROUTERS = pathlib.Path(__file__).resolve().parents[1] / 'routers'

HTTP_VERBS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}

#: 依赖名里出现这些词就算「有认证」。
AUTH_DEPENDENCY_HINTS = ('require', 'current')

#: 明确允许匿名（或在函数体内自行认证）的 `文件:函数名`。
#:
#: 加进来的每一条都要能回答「它凭什么安全」。
ANONYMOUS_BY_DESIGN = {
    # —— API key 认证在函数体内做（`_chat` 解析 Authorization）——
    'gateway.py:chat_v1',
    'gateway.py:chat_v2',
    'gateway.py:list_models',
    'gateway.py:key_usage',
    'gateway.py:usage_v1',
    'responses.py:responses_root',
    'responses.py:responses_v1',
    'anthropic.py:messages',
    'anthropic.py:count_tokens',
    # —— 健康检查，负载均衡器要用 ——
    'gateway.py:gateway_health',
    'auth.py:healthz',
    'auth.py:readyz',
    # —— 登录/登出/注册：本来就该匿名 ——
    'auth.py:logout',
    'auth.py:login',
    'portal.py:login',
    'portal.py:logout',
    'portal.py:register',
    'portal.py:registration_policy',
    # —— 红包：本身凭码，码就是凭证 ——
    'redpackets.py:claim_info',
    'redpackets.py:claim_draw',
}


def _routes_in(path):
    """[(method, route_path, func_name, dependent_names)]"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        verbs = []
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if isinstance(func, ast.Attribute) and func.attr in HTTP_VERBS:
                route_path = ''
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    route_path = str(dec.args[0].value)
                verbs.append((func.attr.upper(), route_path))
        if not verbs:
            continue
        names = _dependent_names(node)
        for method, route_path in verbs:
            out.append((method, route_path, node.name, names))
    return out


def _dependent_names(node):
    args = node.args
    defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
    names = []
    for default in defaults:
        if not isinstance(default, ast.Call):
            continue
        func = default.func
        if (getattr(func, 'id', None) or getattr(func, 'attr', None)) != 'Depends':
            continue
        if not default.args:
            continue
        dep = default.args[0]
        name = getattr(dep, 'attr', None) or getattr(dep, 'id', None)
        if isinstance(dep, ast.Call):
            name = getattr(dep.func, 'attr', None) or getattr(dep.func, 'id', None)
        if name:
            names.append(name)
    return names


def _is_guarded(names):
    return any(hint in n.lower() for n in names for hint in AUTH_DEPENDENCY_HINTS)


class EveryRouteIsAccountedForTests(unittest.TestCase):
    def test_no_route_is_unguarded_and_unlisted(self):
        """新增端点必须带认证依赖，或显式进白名单。"""
        offenders = []
        total = 0
        for file in sorted(ROUTERS.glob('*.py')):
            try:
                rows = _routes_in(file)
            except SyntaxError:
                continue
            for method, route_path, func_name, names in rows:
                total += 1
                if _is_guarded(names):
                    continue
                key = f'{file.name}:{func_name}'
                if key in ANONYMOUS_BY_DESIGN:
                    continue
                offenders.append(f'{file.name} {method} {route_path} -> {func_name}')

        self.assertGreater(total, 50, '扫描到的路由太少，说明解析逻辑坏了')
        self.assertEqual(offenders, [],
                         '这些路由既没有认证依赖，也不在 ANONYMOUS_BY_DESIGN 里：\n  '
                         + '\n  '.join(offenders))

    def test_the_whitelist_has_no_stale_entries(self):
        """白名单里的条目必须真实存在 —— 否则它只是在积累噪音。"""
        live = set()
        for file in sorted(ROUTERS.glob('*.py')):
            try:
                rows = _routes_in(file)
            except SyntaxError:
                continue
            for _method, _path, func_name, _names in rows:
                live.add(f'{file.name}:{func_name}')
        stale = sorted(ANONYMOUS_BY_DESIGN - live)
        self.assertEqual(stale, [], f'白名单里这些已经不存在了：{stale}')


class TheKnownGuardsAreRealTests(unittest.TestCase):
    """抽查三条真实依赖，确认名字对得上。"""

    def test_portal_current_really_authenticates(self):
        source = (ROUTERS / 'portal.py').read_text(encoding='utf-8')
        start = source.index('def current(request: Request)')
        block = source[start:start + 700]
        self.assertIn('401', block, 'current 必须能拒绝未登录')
        self.assertIn('same_origin', block)

    def test_admin_dependency_exists(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'security.py').read_text(
            encoding='utf-8')
        self.assertIn('def require_admin', source)
        self.assertIn('def require_panel_reader', source)

    def test_require_admin_rejects_missing_session(self):
        """看一眼它是不是真的有拒绝分支，而不只是改了个名字。"""
        source = (pathlib.Path(__file__).resolve().parents[1] / 'security.py').read_text(
            encoding='utf-8')
        start = source.index('def require_admin')
        block = source[start:start + 400]
        self.assertIn('raise', block)


if __name__ == '__main__':
    unittest.main()
