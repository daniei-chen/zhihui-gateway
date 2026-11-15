"""v1.2.121：三个死配置，和一份防止继续腐化的清单。

## 起因

上一版查到一个真 bug：一个按平台收紧的超时被环境变量静默覆盖。那是「配置存在
但不生效」的一种形态。

这一版顺着同一个问题往下问：**还有多少配置是定义了却根本没人读的？**

## 第一次统计是错的

第一版脚本只查「其他文件里有没有 `config.X` 的引用」，报出 **7 个**。

其中 `GATEWAY_MAX_RESPONSE_MB` 是**假阳性** —— 它在 `config.py` **内部**就被用了
（两处 `BoundedTransport` 的上限）。我的脚本没把定义文件自己算进去。

**这是同一类错误第三次**：用一个维度的证据去断言另一件事不存在。

## 修正后的结果

把 `config.py` 自身也算上、并排除"定义那一行"之后，剩 **3 个**：

| 常量 | 形态 |
| --- | --- |
| `TENCENT_CHECKIN` | 完整 URL |
| `TENCENT_BILLING` | 完整 URL |
| `TENCENT_HEADERS` | 请求头字典 |

它们都挨着一对**基地址**常量（`TENCENT_BASE` / `TENCENT_BILLING_BASE`），
而代码里统一走基地址 —— 完整 URL 那一套是重构后留下的。

## 为什么没有删

`TENCENT_HEADERS` 的注释写着：

> 本常量暂无调用点。**保留**但它也必须与那边口径一致：
> 否则将来有人照着这里取值，就会发出「形态不像官方客户端」的请求

**原作者是有意留的。** 另两个挨着它，很可能是同一批决定。

**删除的收益是两个常量，风险是动一段没有测试覆盖的腾讯侧代码。** 不值得。

## 所以这一版做什么

把那 3 个名字写进清单，并断言：

- 现有清单**不多不少就是这 3 个**
- 出现第 4 个 → **失败**

这样「配置腐化」从一件没人发现的事，变成一件会立刻失败的事。

**清单变长时的含义**：有人加了常量、又忘了接线。那正是要拦的时刻。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]

#: 已知无调用点的常量。三个都是腾讯侧的完整 URL / 请求头，
#: 与一对基地址常量并存，代码统一走基地址。
#:
#: **这份清单只允许变短，不允许变长。** 变长意味着有人加了配置却没接线。
KNOWN_UNUSED = frozenset({
    'TENCENT_CHECKIN',
    'TENCENT_BILLING',
    'TENCENT_HEADERS',
})


def _sources():
    out = []
    for p in SERVER.rglob('*.py'):
        if 'tests' in p.parts:
            continue
        try:
            out.append((p, p.read_text(encoding='utf-8')))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _config_names():
    cfg = (SERVER / 'config.py').read_text(encoding='utf-8')
    return {m.group(1) for m in re.finditer(r'^([A-Z][A-Z0-9_]{2,})\s*=', cfg, re.M)}


def _unused():
    cfg_path = SERVER / 'config.py'
    cfg_text = cfg_path.read_text(encoding='utf-8')
    sources = _sources()
    unused = set()
    for name in _config_names():
        # 定义那一行本身不算引用
        m = re.search(r'^' + re.escape(name) + r'\s*=.*$', cfg_text, re.M)
        span = (m.start(), m.end()) if m else None
        hits = 0
        for p, text in sources:
            for hit in re.finditer(r'\b' + re.escape(name) + r'\b', text):
                if p == cfg_path and span and span[0] <= hit.start() < span[1]:
                    continue
                hits += 1
        if hits == 0:
            unused.add(name)
    return unused


class NoNewDeadConfigTests(unittest.TestCase):
    def setUp(self):
        self.unused = _unused()

    def test_the_known_three_are_still_the_three(self):
        """清单是精确匹配 —— 少了说明有人接了线（好），多了就是失败。"""
        self.assertEqual(self.unused, set(KNOWN_UNUSED),
                         '出现了新的无调用点常量：%s' % sorted(self.unused - KNOWN_UNUSED))

    def test_no_new_dead_config(self):
        """分开写一条，失败信息更直接。"""
        new = self.unused - KNOWN_UNUSED
        self.assertEqual(new, set(),
                         '这些常量定义了但没有任何引用：%s\n'
                         '要么接上调用点，要么加进 KNOWN_UNUSED 并写明理由' % sorted(new))

    def test_the_list_does_not_rot(self):
        """如果某个常量的引用被删掉、清单里又没有，上面那条会红；
        反过来，清单里的常量如果重新被引用，这条会红。"""
        stale = KNOWN_UNUSED - self.unused
        self.assertEqual(stale, set(),
                         '这些常量已经有引用了，从清单里删掉：%s' % sorted(stale))


class TheEnvironmentBackedOnesAreWatchedTests(unittest.TestCase):
    """从环境变量取的常量最危险 —— 它们看起来"配了就生效"。"""

    def test_any_unused_env_backed_constant_fails_loudly(self):
        cfg = (SERVER / 'config.py').read_text(encoding='utf-8')
        env_backed = set()
        for name in _config_names():
            m = re.search(r'^' + re.escape(name) + r'\s*=\s*(.+)$', cfg, re.M)
            if m and '_env' in m.group(1):
                env_backed.add(name)
        unused_env = _unused() & env_backed
        # 这三个都不是从环境变量取的，所以这里应当为空
        self.assertEqual(unused_env, set(),
                         '这些从环境变量读的配置没有任何引用，'
                         '「配了就生效」的假设会是假的：%s' % sorted(unused_env))


if __name__ == '__main__':
    unittest.main()
