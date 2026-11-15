"""v1.2.170：把发布组装固化成仓库里的脚本。

## 为什么

2026-11-15 一天里手写了**五个**临时构建脚本，每个都在 `/tmp`，每个都少了点东西 ——
其中一个把 HTTP 403 的响应体当成文件内容写进了产物。

**临时脚本不会积累防护。** 上一次踩过的坑，下一个临时脚本照样踩。

## 三道校验，强弱不同

| 关卡 | 检查 | 弱点 |
| --- | --- | --- |
| 存在 | 文件非空 | **最弱** —— 先复制了上一版，文件永远非空 |
| 形状 | JSON 以 `{` 开头、不含 HTTP 错误标记 | **不依赖期望值** |
| 哈希 | 与清单逐条比对 | 期望值本身可能填错 |

**第二关的价值在于它不看我的期望值。** 期望值是人填的，人是会抄错的；
「这个 JSON 文件里写着 rate limit exceeded」是不需要参照就能判断的荒唐。

## 本测试守什么

脚本无法在 Windows 上执行（没有 bash），所以这里钉结构：
**三道校验必须都在**、**形状校验不能被挪到哈希之后**（那等于没用）、
**失败必须中止而不是继续组装**。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / 'deploy' / 'build-release.sh'
SCRIPT = SCRIPT_PATH.read_text(encoding='utf-8')


class ItExistsAndIsSafeTests(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(SCRIPT_PATH.is_file())

    def test_strict_mode(self):
        self.assertIn('set -uo pipefail', SCRIPT)

    def test_refuses_existing_target(self):
        """覆盖一个已存在的发布目录是事故，不是便利。"""
        self.assertIn('REFUSE: 目标已存在', SCRIPT)

    def test_requires_previous_release(self):
        self.assertIn('上一版目录不存在', SCRIPT)

    def test_explains_why_previous_release_matters(self):
        """前端产物无法在主机重建，必须继承。"""
        self.assertIn('web/out', SCRIPT)
        self.assertIn('无法在主机重建', SCRIPT)


class ItChecksThreeLayersTests(unittest.TestCase):
    def test_layer_one_non_empty(self):
        self.assertIn('[ ! -s "$OUT/$rel" ]', SCRIPT)

    def test_layer_two_shape(self):
        self.assertIn('POLLUTED', SCRIPT)

    def test_layer_two_catches_http_errors(self):
        """匹配的是带结构的完整形态，不是裸词。

        两次误伤都是因为裸词：注释和断言里写一写就命中，而脚本检查自己时
        必然命中自己的注释。
        """
        self.assertIn('"message":[[:space:]]*"API rate limit', SCRIPT)
        self.assertIn('"documentation_url":"https://docs', SCRIPT)
        self.assertIn("^Bad credentials$", SCRIPT)

    def test_markdown_is_exempt_from_shape_check(self):
        """文档必须能引用错误响应原文来描述这个坑 —— 否则它说不清发生过什么。

        这是第三次自指：前两次靠收紧标记解决，这一次不能那样解。
        """
        idx = SCRIPT.find('*.md) ;;')
        self.assertGreater(idx, 0, '.md 没有被豁免')

    def test_code_and_config_still_checked(self):
        idx = SCRIPT.find('POLLUTED')
        before = SCRIPT[max(0, idx - 600):idx]
        self.assertIn('*)', before, '没有对非文档文件保留检查')

    def test_the_pattern_has_structure_not_bare_words(self):
        """裸词会命中注释。这里断言模式里带引号或锚点。"""
        line = [l for l in SCRIPT.splitlines() if 'grep -qiE' in l or 'grep -qE' in l]
        self.assertTrue(line, '没找到形状校验行')
        for pattern in line:
            if 'Bad credentials' in pattern:
                self.assertIn('^Bad credentials$', pattern)
            if 'rate limit' in pattern:
                self.assertIn('"message"', pattern)

    def test_it_does_not_use_ambiguous_markers(self):
        """`Not Found` 太通用 —— 第一次真跑用它误伤了两个正常文件。

        校验误伤的代价和漏判一样高：它会让人开始手动绕过校验，
        那样任何校验都不再有意义。
        """
        # 只看形状校验那一行的匹配模式，不看注释（注释里引用这些词是正常的）
        line = [l for l in SCRIPT.splitlines() if 'grep -qiE' in l and 'POLLUT' not in l]
        self.assertTrue(line, '没找到形状校验的 grep 行')
        pattern = line[0]
        self.assertNotIn('Not Found', pattern,
                         '形状校验的匹配模式里仍有 Not Found，会误伤正常文本')

    def test_layer_two_checks_json_opens_with_brace(self):
        self.assertIn("grep -q '^{'", SCRIPT)

    def test_layer_three_hash(self):
        self.assertIn('HASH-MISMATCH', SCRIPT)

    def test_shape_runs_before_hash(self):
        """顺序反了就失去意义：哈希先报错，形状检查永远走不到。"""
        shape = SCRIPT.find('POLLUTED')
        hashed = SCRIPT.find('HASH-MISMATCH')
        self.assertGreater(shape, 0)
        self.assertGreater(hashed, 0)
        self.assertLess(shape, hashed, '形状校验必须在哈希之前')

    def test_it_says_shape_does_not_depend_on_expectations(self):
        """这是加这一关的全部理由，必须写在代码里。"""
        self.assertIn('不依赖任何人填的期望值', SCRIPT)


class ItAbortsInsteadOfContinuingTests(unittest.TestCase):
    def test_abort_on_failure(self):
        self.assertIn('不组装', SCRIPT)

    def test_it_removes_the_partial_output(self):
        """半成品留在磁盘上，下次可能被误用。"""
        idx = SCRIPT.find('ABORT')
        window = SCRIPT[idx:idx + 200]
        self.assertIn('rm -rf "$OUT"', window)

    def test_empty_manifest_is_an_error(self):
        self.assertIn('清单里没有条目', SCRIPT)


class ItAvoidsTheApiQuotaTrapTests(unittest.TestCase):
    def test_uses_partial_clone(self):
        self.assertIn('--filter=blob:none', SCRIPT)

    def test_explains_why_not_the_api(self):
        self.assertIn('API', SCRIPT)
        self.assertIn('额度', SCRIPT)

    def test_verifies_the_commit_actually_matches(self):
        """克隆到别的 commit 却继续组装，会得到一版内容不对的发布。"""
        self.assertIn('HEAD_SHA', SCRIPT)
        self.assertIn('仍然对不上', SCRIPT)


class ItRestoresPermissionsTests(unittest.TestCase):
    def test_executable_bits_are_restored(self):
        """`cp` 会丢掉可执行位，入口脚本变成不可执行就起不来。"""
        self.assertIn('chmod 755', SCRIPT)
        self.assertIn('backup_application.py', SCRIPT)

    def test_changelog_copy(self):
        self.assertIn('server/CHANGELOG.md', SCRIPT)


class ItLeavesTheDirectoryBeforeDeletingItTests(unittest.TestCase):
    """第一次真的跑这个脚本时挂在这里，而错误信息看起来像网络问题。

    `rm -rf` 执行时 bash 的当前目录正在被删的目录里，删完 cwd 就是个不存在的
    inode —— 之后任何命令都报 "Unable to read current working directory"，
    连 `git clone` 都起不来。
    """

    def test_cd_before_rm(self):
        """匹配带缩进的实际代码行 —— 注释里也提到过这条命令，按裸字符串找会命中注释。"""
        marker = '\n    rm -rf "$CACHE/repo"\n'
        idx = SCRIPT.find(marker)
        self.assertGreater(idx, 0, '没找到删缓存目录的实际代码行')
        before = SCRIPT[max(0, idx - 400):idx]
        self.assertIn('cd / || exit 1', before, '删目录前没有先离开，cwd 会变成悬空的')

    def test_it_explains_the_symptom(self):
        self.assertIn('Unable to read current working directory', SCRIPT)

    def test_cd_has_a_failure_guard(self):
        self.assertIn('cd / || exit 1', SCRIPT)


class PermissionsUseAGlobNotAListTests(unittest.TestCase):
    """v1.2.175 加了新脚本却忘了改进手写清单，它落地后是 Permission denied。

    列举式清单的问题不是「这次漏了」，是**每次新增都要记得回来改**，
    而漏掉时不会有任何声音 —— 那一版发布完全没报错，因为没人去执行它。
    """

    def test_deploy_scripts_use_a_glob(self):
        self.assertIn('"$DST"/deploy/*.sh', SCRIPT)

    def test_no_hand_written_executable_list(self):
        """旧写法是把每个脚本名逐行列出来。"""
        self.assertNotIn('for exe in server/backup_application.py', SCRIPT)

    def test_it_records_the_permission_denied_incident(self):
        self.assertIn('Permission denied', SCRIPT)

    def test_non_entry_python_gets_restored_to_644(self):
        """通配给 server/*.py 全 755 是过宽的，非入口必须收回。"""
        self.assertIn('chmod 644 "$f"', SCRIPT)

    def test_backup_application_stays_executable(self):
        """它是部署时被直接执行的入口。"""
        self.assertIn('backup_application.py) chmod 755', SCRIPT)


if __name__ == '__main__':
    unittest.main()
