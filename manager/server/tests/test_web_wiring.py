"""「已经做好、但没人接线」的入口组件（PB0012 的教训）。

## 为什么需要这份守卫

有一类缺陷是所有既有自动测试都看不见的：组件写好了、单测绿、类型也对，
**只是没有任何页面渲染它**。

语言切换器就这样躺了很久：`LanguageToggle` 组件（47 行，自带 `aria-label`）、
`I18nProvider`（locale 检测 + localStorage 持久化 + 按需加载字典）、5 套完整字典、
连 `language.switch` 的译文都在——但全站 0 处引用，`setLocale` 没有任何 UI 调用。
结果是「多语言能力完全空转」：用户只能被浏览器语言决定，而且**改不了**。

这类缺口不会被任何单点测试抓到（每个部件都是对的），只能靠「入口有没有被接线」
这种跨文件的结构断言。所以这里钉住已知的关键入口组件，并要求引用数下限。

引用统计刻意排除组件自己的定义文件：`export function LanguageToggle` 里那个名字
不算「被使用」——正是这条区别让它能抓到 PB0012。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _resolve_web() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / 'web' / 'app').is_dir():
            return parent / 'web'
    return None


_WEB = _resolve_web()
if _WEB is None:
    raise unittest.SkipTest(
        '本 target 不含前端资产（部署树 manager/ 只有 server/）；'
        '接线守卫请用 --target manager-assets')

#: 组件名 → (至少几处引用, 说明)
#:
#: 下限取 2 是因为语言切换在**登录前**与**登录后**都要有：只有控制台那处时，
#: 还没进来的人依然被浏览器语言锁住。
WIRED_MINIMUM = {
    'LanguageToggle': (2, '语言切换：控制台右上角 + 登录页（登录前的人也要能读懂界面）'),
}


#: 剥离 import 语句用的正则。
#:
#: 必须剥掉：`import {LanguageToggle} from '...'` 说明「这个文件知道它的存在」，
#: 不说明「这个文件渲染了它」。
_IMPORT_BLOCK = re.compile(r'import\b[\s\S]*?from\s+[\'"][^\'"]+[\'"];?')

#: 剥离注释（含 JSX 的 `{/* ... */}`）。
#:
#: 必须剥掉，否则**注释里提到组件名**就算接线。2026-10-27 的负向验证正是被这骗过的：
#: 把 `<LanguageToggle />` 注释掉、只留 import 和一句「位置就是 LanguageToggle 注释里写的…」，
#: 守卫连续两次报绿。剥掉注释后，被注释掉的 JSX 才真的不可见——这也是负向验证的意义：
#: **守卫自己必须先被证伪过一次**，否则它只是一段看起来很严肃的装饰。
_COMMENT_BLOCK = re.compile(r'/\*[\s\S]*?\*/|//[^\n]*')


def _code_only(text: str) -> str:
    """返回去掉 import 与注释之后的代码。"""
    return _COMMENT_BLOCK.sub('', _IMPORT_BLOCK.sub('', text))


def _usages(component: str) -> list[str]:
    """返回**真正渲染/调用**了该组件的文件（不含定义文件，也不含纯 import）。"""
    pattern = re.compile(r'\b' + re.escape(component) + r'\b')
    hits: list[str] = []
    for path in sorted(_WEB.rglob('*')):
        if path.suffix not in ('.ts', '.tsx'):
            continue
        # 构建产物不是源码；node_modules 不是我们的代码
        if 'node_modules' in path.parts or 'out' in path.parts or '.next' in path.parts:
            continue
        text = path.read_text(encoding='utf-8')
        if not pattern.search(text):
            continue
        # 排除「定义自己的那个文件」：里面有 export function <Name> 或文件名就是它
        if re.search(r'export\s+(?:default\s+)?function\s+' + re.escape(component) + r'\b', text):
            continue
        # 只 import 不渲染、或只出现在注释里，都不算接线
        if not pattern.search(_code_only(text)):
            continue
        hits.append(path.relative_to(_WEB).as_posix())
    return hits


class ComponentWiringTest(unittest.TestCase):
    def test_scan_is_not_vacuous(self) -> None:
        """先确认扫描真的在看文件，否则下面的断言会空转通过。"""
        self.assertTrue((_WEB / 'app').is_dir(), '前端 app 目录不存在——扫描失效')
        self.assertGreater(len(list(_WEB.rglob('*.tsx'))), 50,
                           'tsx 文件太少——扫描范围可能错了')

    def test_language_toggle_is_wired(self) -> None:
        # 不用 self.subTest：只有一个被检组件，而 subTest 在没有对应插件时会**吞掉**
        # 断言失败——2026-10-27 的负向验证就是这么被骗过的（摘掉接线后测试仍然全绿）。
        # 守卫一旦会假通过，比没有守卫更糟。
        component = 'LanguageToggle'
        minimum, why = WIRED_MINIMUM[component]
        hits = _usages(component)
        self.assertGreaterEqual(
            len(hits), minimum,
            f'{component} 只被 {len(hits)} 处真正渲染（要求 >= {minimum}）。{why}。'
            f'现有引用：{hits}')
        # 明确要求登录页那处：只有控制台的话，登录前的人仍然改不了语言
        self.assertTrue(any('login' in h for h in hits),
                        f'{component} 没有出现在登录页——登录前的人读不懂界面就只能猜；实际引用：{hits}')
        self.assertTrue(any('(main)/layout' in h for h in hits),
                        f'{component} 没有出现在控制台外壳——登录后反而改不了语言；实际引用：{hits}')
