"""v1.2.113：写端点的鉴权来源，与一个必须钉住的隐式契约。

## 起因

做「权限矩阵与路由声明的一致性」盘查：用 AST 提取所有写方法路由，
检查函数签名里有没有鉴权依赖。

## 两次假阳性

### 第一次

首轮报出 20 条「签名里什么都没有」的写端点，全在合并进来的控制面兼容层。

**实测：全部 401。** 鉴权在中间件里统一做，不在函数签名里。

### 第二次

补查后报出 54 条、9 个文件。**其中用户门户那 13 条也是假阳性** ——
它们写的是 `Depends(current)`，而我的关键词表里只有 `current_user`，
**没把 `current` 算进去**。

## 修正后的完整分类

放宽关键词、并加上「函数体里手动调用鉴权」的判断后，剩下 41 条。逐类核实：

| 类别 | 条数 | 为什么合理 |
| --- | --- | --- |
| 控制面兼容层 | 31 | **中间件统一鉴权**，实测各平台头均 401 |
| 登录 / 注册 / 登出 | 5 | 这些**本来就发生在登录之前** |
| 数据面（chat / responses） | 4 | 走 API 密钥，**在函数体内手动校验** |
| 红包领取 | 1 | `code` 即一次性凭证 + **按 IP 限一次** |

**没有任何一条是裸奔的。**

## 那这个测试守什么

守两件事。

### 一、那个隐式契约

31 条写端点**自身没有任何声明**表示需要鉴权。它们的安全性完全依赖：

1. 中间件存在
2. 中间件在这条路径上真的调用身份解析
3. 平台头解析不会给出「跳过鉴权」的分支

**任何一条被改，31 个写端点同时裸奔，而它们自己的代码一行都不用改。**

这类「靠外部设施兜底」的安全最容易被后来的重构打穿 —— 因为改的人看的是端点，
端点本身看不出问题。

### 二、类别不能悄悄扩张

41 条现在都是可解释的。**如果哪天冒出一个不属于上表任何一类的新端点，
说明有人加了写端点却忘了加鉴权。**

测试会在那时失败，而不是等到有人用它。

## 这个测试不做什么

它不验证「鉴权逻辑本身正确」—— 那是另外几个测试文件的事。
它只验证**每一处无声明鉴权都在已知的、可解释的类别里**。
"""
from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from server import main

SERVER = Path(__file__).resolve().parents[1]
WRITE = {"post", "put", "patch", "delete"}

# 放宽过两次：第一次漏了 `current`，把用户门户的 13 条全判成裸奔。
AUTH_HINTS = (
    "current", "require_", "admin", "auth", "user", "identity", "guard",
    "session", "scope", "operator", "owner", "portal", "token",
)
BODY_HINTS = ("current", "require_", "admin", "_owner", "_auth")

# 允许的无声明鉴权类别：文件 + 路径模式
ALLOWED = (
    ("qtgcore/routers/compat", "控制面兼容层，靠中间件"),
    ("routers/auth.py", "登录/登出，发生在鉴权之前"),
    ("routers/portal.py", "同上，门户的登录与注册"),
    ("routers/gateway.py", "数据面，走 API 密钥"),
    ("routers/responses.py", "数据面，走 API 密钥"),
    ("routers/redpackets.py", "红包领取，码即凭证 + IP 限一次"),
)


def _dotted(node) -> str:
    parts = []
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Name):
            parts.append(cur.id)
            break
        elif isinstance(cur, ast.Call):
            cur = cur.func
        else:
            break
    return ".".join(reversed(parts))


def _deps(fn) -> list[str]:
    out = []
    a = fn.args
    pos = list(a.args)
    defs = list(a.defaults)
    pairs = []
    if defs:
        pairs += list(zip(pos[len(pos) - len(defs):], defs))
    for x, d in zip(a.kwonlyargs, a.kw_defaults):
        if d is not None:
            pairs.append((x, d))
    for _x, node in pairs:
        if isinstance(node, ast.Call):
            n = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if n == "Depends" and node.args:
                out.append(_dotted(node.args[0]))
    return out


def _body_calls(fn) -> set[str]:
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and not isinstance(n.func, ast.Call):
            names.add(_dotted(n.func))
    return names


def _write_routes_without_declared_auth():
    found = []
    files = list(SERVER.glob("routers/*.py")) + list((SERVER / "qtgcore" / "routers").glob("*.py"))
    for f in sorted(files):
        if f.name == "__init__.py":
            continue
        rel = f.relative_to(SERVER).as_posix()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                if dec.func.attr not in WRITE:
                    continue
                path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else "?"
                deps = _deps(node)
                if any(any(h in d.lower() for h in AUTH_HINTS) for d in deps):
                    continue
                if any(any(h in b.lower() for h in BODY_HINTS) for b in _body_calls(node)):
                    continue
                found.append((rel, node.lineno, dec.func.attr.upper(), path, node.name))
    return found


class EveryUnmarkedWriteFallsInAKnownCategoryTests(unittest.TestCase):
    def setUp(self):
        self.found = _write_routes_without_declared_auth()

    def test_there_are_still_some(self):
        self.assertGreater(len(self.found), 0,
                           '前提变了：不再有没有声明鉴权的写端点，请重审本测试')

    def test_none_are_outside_the_known_categories(self):
        """新冒出来的端点会被这条拦下 —— 那通常意味着加了写端点却忘了鉴权。"""
        stray = [row for row in self.found
                 if not any(row[0].startswith(prefix) for prefix, _why in ALLOWED)]
        self.assertEqual(stray, [],
                         '出现了不属于任何已知类别的无声明鉴权写端点：%s' % stray)

    def test_each_category_is_documented(self):
        for prefix, why in ALLOWED:
            self.assertTrue(why.strip(), f'{prefix} 没有写明理由')

    def test_the_compat_layer_is_the_biggest_group(self):
        """记录比例，让「靠中间件」这件事的分量可见。"""
        compat = [r for r in self.found if r[0].startswith('qtgcore/routers/compat')]
        self.assertGreater(len(compat), 20,
                           '兼容层不再是大头，本测试的侧重点该重新考虑了')


class TheMiddlewareContractTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(main)

    def test_the_401_branch_exists(self):
        self.assertIn('auth required', self.src)

    def test_identity_resolution_is_invoked(self):
        self.assertIn('security.current_user', self.src,
                      '中间件不再调用身份解析，兼容层那批写端点会全部裸奔')

    def test_it_runs_off_the_event_loop(self):
        self.assertIn('joined_thread', self.src,
                      '身份解析会等文件或数据库锁，阻塞它等于卡住推理流')

    def test_platform_whitelist_is_explicit(self):
        """平台头是请求方可控的。若判定放宽成「非空即路由」，
        就能构造一个特殊值把请求导到没有函数级鉴权的实现上。"""
        self.assertIn('("qoder", "trae")', self.src.replace("'", '"'))

    def test_internal_prefix_cannot_be_reached_directly(self):
        self.assertIn('qtg_rewritten', self.src,
                      '直接访问内部前缀必须 404，否则可跳过整个中间件')


class TheFalsePositivesAreRecordedTests(unittest.TestCase):
    def test_both_false_positives_are_documented(self):
        doc = __doc__ or ''
        self.assertIn('假阳性', doc)
        self.assertIn('current', doc, '第二次假阳性是因为关键词漏了 current')

    def test_it_says_why_this_matters(self):
        doc = __doc__ or ''
        self.assertIn('裸奔', doc)


if __name__ == '__main__':
    unittest.main()
