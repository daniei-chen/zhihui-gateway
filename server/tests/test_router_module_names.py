"""守住 `qtgcore/routers/*.py` 里「用了没导入的名字」这一类错误。

背景（两次真实事故，同一个形状）：
  * 2026-11-20 `compat_fixes.fixed_upstreams` 用了 `Path` 但没 import
    → `/api/upstreams` 对 trae / doubaowork 都是裸 500。
  * 同日 `compat.compat_accounts_upload` 用了裸 `json`（该模块只有
    `import json as _json`）→ 上传接口 500 NameError。

两次都只在**真机调用那条分支**时才炸，单元测试没覆盖到那条分支。

实现要点：不自己写作用域分析（嵌套函数 / 闭包 / 推导式极易误报），
改用编译器已解析好的字节码：逐个 code object 找 `LOAD_GLOBAL` 的目标名，
再看它是否在模块命名空间或内建里。`LOAD_GLOBAL` 天然不含局部名与参数，
所以零误报。
"""
from __future__ import annotations

import builtins
import dis
import importlib
import types
import unittest

MODULES = [
    'server.qtgcore.routers.compat',
    'server.qtgcore.routers.compat_fixes',
    'server.qtgcore.routers.compat_stats',
    'server.qtgcore.routers.compat_settings',
    'server.qtgcore.routers.compat_accounts_ext',
    'server.qtgcore.routers.proxy',
]


def _all_codes(code: types.CodeType) -> list[types.CodeType]:
    """递归收集该 code object 及其内部所有函数的 code object。"""
    out = [code]
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out.extend(_all_codes(const))
    return out


def _unresolved(module: types.ModuleType) -> list[str]:
    """模块里被函数以「全局」方式引用、但模块里找不到的名字。"""
    missing: set[str] = set()
    for code in _all_codes(module.__dict__['__code__'] if '__code__' in module.__dict__
                           else compile(open(module.__file__, encoding='utf-8').read(),
                                        module.__file__, 'exec')):
        for ins in dis.get_instructions(code):
            if ins.opname not in ('LOAD_GLOBAL', 'LOAD_NAME'):
                continue
            name = str(ins.argval)
            if name in module.__dict__ or name in dir(builtins):
                continue
            missing.add(name)
    return sorted(missing)


class RouterModulesResolveEveryGlobalTests(unittest.TestCase):
    """每个 router 模块里被函数引用的全局名，必须能在模块里找到。"""

    def test_every_router_module_resolves_its_globals(self) -> None:
        problems: list[str] = []
        for dotted in MODULES:
            try:
                module = importlib.import_module(dotted)
            except Exception as exc:  # noqa: BLE001 - 导入失败本身就是问题
                problems.append('%s: 导入失败 %s: %s' % (dotted, type(exc).__name__, exc))
                continue
            missing = _unresolved(module)
            if missing:
                problems.append('%s: %s' % (dotted.rsplit('.', 1)[-1], ', '.join(missing)))
        self.assertEqual([], problems,
                         '以下 router 模块用了未定义的名字（真机会 500）：\n  '
                         + '\n  '.join(problems))


class KnownPastRegressionsTests(unittest.TestCase):
    """把两次真实事故钉成回归断言。"""

    def test_compat_fixes_resolves_every_global(self) -> None:
        mod = importlib.import_module('server.qtgcore.routers.compat_fixes')
        self.assertEqual([], _unresolved(mod),
                         'compat_fixes 曾因未 import Path 让 /api/upstreams 裸 500')

    def test_compat_resolves_every_global(self) -> None:
        mod = importlib.import_module('server.qtgcore.routers.compat')
        self.assertEqual([], _unresolved(mod),
                         'compat 曾因裸 json 让 /api/accounts/upload 裸 500')

    def test_compat_stats_resolves_every_global(self) -> None:
        mod = importlib.import_module('server.qtgcore.routers.compat_stats')
        self.assertEqual([], _unresolved(mod))

    def test_proxy_resolves_every_global(self) -> None:
        mod = importlib.import_module('server.qtgcore.routers.proxy')
        self.assertEqual([], _unresolved(mod))


if __name__ == '__main__':
    unittest.main()
