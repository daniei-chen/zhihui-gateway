"""v1.2.175：校验前端调用的每个 API 路径，后端都注册了。

## 为什么需要

前端产物是**预构建**的，无法在这台机器重建。所以「前端调了一个后端没有的路径」
**在构建时不会被发现** —— 要等用户点下去看到 404。

## 这个审计我一开始做错了四次

| 尝试 | 失败原因 |
| --- | --- |
| 逐个 curl 探测 | 路径参数路由会先匹配，**「路由不存在」和「资源不存在」都是 404** |
| grep 完整路径 | 后端写的是相对于 prefix 的 `'/check-models'` |
| 取 OpenAPI | `/openapi.json` 在生产是 404（**这是对的**） |
| import 应用 | 被命令守卫拦下 |

## 两个必须处理的坑

**一：prefix 必须与路径在同一文件内配对。** 跨文件猜 prefix 会拼错。

**二：空路径不是「跳过」，那是集合端点。**
第一版有 `[ -z "$route" ] && continue`，于是 `@router.get('')`（即 `/api/keys` 本身）
被全部丢掉，对比结果凭空多出六个假缺失 —— **而那六个实际都返回 401（存在）**。

## 本测试守什么

脚本无法在 Windows 执行（没有 bash），所以钉结构：两个坑的防护必须都在、
退出码语义正确、归一化要处理路径参数与尾斜杠。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / 'deploy' / 'check-route-coverage.sh'
SCRIPT = SCRIPT_PATH.read_text(encoding='utf-8')


class ItExistsAndIsSafeTests(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(SCRIPT_PATH.is_file())

    def test_strict_mode(self):
        self.assertIn('set -uo pipefail', SCRIPT)

    def test_cleans_up_temp_dir(self):
        self.assertIn('trap', SCRIPT)

    def test_checks_both_directories(self):
        self.assertIn('前端产物目录不存在', SCRIPT)
        self.assertIn('后端源码目录不存在', SCRIPT)

    def test_fails_when_extraction_finds_nothing(self):
        """提取规则与代码不符时，空结果必须报错而不是「全部通过」。"""
        self.assertIn('一条路由都没提取到', SCRIPT)
        self.assertIn('一个 API 路径都没找到', SCRIPT)


class TheTwoTrapsAreGuardedTests(unittest.TestCase):
    def test_prefix_is_pair_with_path_in_same_file(self):
        """跨文件猜 prefix 会把 /api/keys 和 /check-models 拼错。"""
        idx = SCRIPT.find('APIRouter\\(prefix=')
        self.assertGreater(idx, 0)
        # prefix 与 route 的循环必须在同一个 for f 体内
        for_idx = SCRIPT.find('for f in "$BACKEND"/routers/*.py')
        self.assertLess(for_idx, idx, 'prefix 提取不在文件循环内')

    def test_empty_route_is_not_skipped(self):
        """集合端点写成 @router.get('')，跳过它就丢掉了 /api/keys 本身。

        匹配带缩进的实际代码行 —— 注释里引用这段代码是正常的，
        按裸字符串找会命中注释（这个坑在构建脚本那边已经踩过一次）。
        """
        self.assertNotIn('\n        [ -z "$route" ] && continue\n', SCRIPT,
                         '还在按「路径为空」跳过，会丢掉集合端点')

    def test_skip_condition_requires_both_empty(self):
        self.assertIn('[ -z "${prefix}${route}" ] && continue', SCRIPT)

    def test_it_records_the_false_missing_incident(self):
        self.assertIn('假缺失', SCRIPT)
        self.assertIn('401', SCRIPT)


class ItNormalisesBeforeComparingTests(unittest.TestCase):
    def test_path_parameters_are_normalised(self):
        self.assertIn("s/{[^}]*}/{}/g", SCRIPT)

    def test_trailing_slash_is_normalised(self):
        """`/api/keys` 与 `/api/keys/` 不该算差异。"""
        self.assertIn("s:/$::", SCRIPT)


class ExitCodesAreMeaningfulTests(unittest.TestCase):
    def test_ok_exits_zero(self):
        idx = SCRIPT.find('ROUTE-COVERAGE-OK')
        window = SCRIPT[idx:idx + 120]
        self.assertIn('exit 0', window)

    def test_fail_exits_one(self):
        idx = SCRIPT.find('ROUTE-COVERAGE-FAIL')
        window = SCRIPT[idx:idx + 400]
        self.assertIn('exit 1', window)

    def test_fail_message_explains_the_user_impact(self):
        self.assertIn('用户点下去就是 404', SCRIPT)


class ItRecordsWhyTheNaiveApproachFailsTests(unittest.TestCase):
    """这条是为了让下一个人不要重走那四次。"""

    def test_it_documents_the_probe_failure(self):
        self.assertIn('资源不存在', SCRIPT)

    def test_it_documents_the_openapi_failure(self):
        self.assertIn('/openapi.json', SCRIPT)

    def test_it_says_closing_openapi_is_correct(self):
        self.assertIn('生产环境不该暴露', SCRIPT)


if __name__ == '__main__':
    unittest.main()
