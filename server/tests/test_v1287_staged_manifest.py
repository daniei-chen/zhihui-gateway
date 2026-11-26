"""v1.2.87：发布组装的校验只看「文件存在」，不看「文件正确」。

## 事故

2026-11-15：组装流程是「复制上一版 → 逐个下载变更文件 → 检查文件非空」。

这次 GitHub API 返回 403（速率限制），下载失败。**但因为是先复制的，文件仍然非空**，
「非空」这一关照常通过 —— 有一个文件静默地留在了上一版。

症状极难发现：服务照常运行、完整性闭包 ok、接口全部正常，只有一个元数据文件里的
版本号慢了一版。

## 修法

两个配套工具：

| 工具 | 作用 |
| --- | --- |
| `make-staged-manifest.py` | 从 **git 的某个提交**算出期望哈希（不是从磁盘） |
| `verify-staged-files.sh` | 逐文件比对内容，缺失与不符**分开报告** |

**从 git 而不是磁盘**：磁盘上可能有未提交的改动，那会把「本地试过但没发出去的东西」
当成期望值。

**缺失与不符分开**：缺失通常意味着下载失败（旧文件掩盖了它），不符意味着下到了别的
版本 —— 两种情况的处置不同，混在一起报会让人以为都是「内容被改」。

## 为什么这不算过度设计

上一版的实际损失是：一个声明了错误能力的元数据文件被发布出去，而所有常规检查都
显示正常。**校验内容比校验存在贵不了多少**，而漏掉的代价是「以为发对了」。
"""
from __future__ import annotations

import hashlib
import inspect
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / 'deploy'
MAKER = DEPLOY / 'make-staged-manifest.py'
CHECKER = DEPLOY / 'verify-staged-files.sh'


def _run_maker(repo, ref, out, paths):
    return subprocess.run(
        [sys.executable, str(MAKER), '--repo', str(repo), '--ref', ref,
         '--out', str(out), *paths],
        capture_output=True, text=True, encoding='utf-8', errors='replace')


class ManifestGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_it_writes_sha256sum_compatible_lines(self):
        out = self.tmp / 'expected.txt'
        result = _run_maker(ROOT, 'HEAD', out, ['VERSION', 'server/main.py'])
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [ln for ln in out.read_text(encoding='utf-8').splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            digest, rel = line.split('  ', 1)
            self.assertEqual(len(digest), 64)
            self.assertRegex(rel, r'^(VERSION|server/main\.py)$')

    def test_hashes_come_from_git_not_disk(self):
        """关键：拿一个与磁盘内容不同的 ref，算出来的必须是 ref 的内容。"""
        # 这条要历史 tag。actions/checkout 默认 fetch-depth: 1 且不拉 tag，
        # 浅克隆里 v1.2.85 不存在，会让整条门禁红在一个与代码无关的环境差异上。
        # workflow 侧已补 fetch-depth: 0 + fetch-tags: true；这里再兜一层。
        probe = subprocess.run(['git', 'rev-parse', '--verify', '--quiet', 'v1.2.85^{commit}'],
                               cwd=ROOT, capture_output=True)
        if probe.returncode != 0:
            self.skipTest('浅克隆没有 v1.2.85 标签（需要 fetch-depth: 0 且 fetch-tags: true）')
        out = self.tmp / 'expected.txt'
        # v1.2.85 的 VERSION 与当前 HEAD 不同
        result = _run_maker(ROOT, 'v1.2.85^{commit}', out, ['VERSION'])
        self.assertEqual(result.returncode, 0, result.stderr)
        old_hash = out.read_text(encoding='utf-8').split()[0]

        out2 = self.tmp / 'expected2.txt'
        _run_maker(ROOT, 'HEAD', out2, ['VERSION'])
        new_hash = out2.read_text(encoding='utf-8').split()[0]
        self.assertNotEqual(old_hash, new_hash, '两个版本的 VERSION 不该同哈希')

    def test_missing_path_fails_loudly(self):
        out = self.tmp / 'expected.txt'
        result = _run_maker(ROOT, 'HEAD', out, ['VERSION', 'server/definitely-not-here.py'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('definitely-not-here', result.stderr)
        self.assertFalse(out.exists(), '失败时不该留下半个清单')

    def test_non_git_directory_fails(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        result = _run_maker(plain, 'HEAD', self.tmp / 'x.txt', ['VERSION'])
        self.assertEqual(result.returncode, 2)

    def test_empty_path_list_is_rejected_by_argparse(self):
        result = subprocess.run(
            [sys.executable, str(MAKER), '--repo', str(ROOT), '--ref', 'HEAD',
             '--out', str(self.tmp / 'x.txt')],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


class CheckerChecksContentNotExistenceTests(unittest.TestCase):
    """无 bash 的环境下静态确认关键逻辑；主机上另做实测。"""

    SOURCE = CHECKER.read_text(encoding='utf-8') if CHECKER.is_file() else ''

    def test_the_script_exists_and_is_executable_shaped(self):
        self.assertTrue(CHECKER.is_file())
        self.assertTrue(self.SOURCE.startswith('#!/bin/bash'))

    def test_it_compares_sha256_not_just_size(self):
        self.assertIn('sha256sum', self.SOURCE)
        self.assertIn('expected', self.SOURCE)
        self.assertIn('actual', self.SOURCE)

    def test_it_distinguishes_missing_from_differing(self):
        """缺失=下载没成功，不符=下到了别的版本。处置不同，不能合并报告。"""
        self.assertIn('MISSING', self.SOURCE)
        self.assertIn('DIFFER', self.SOURCE)
        self.assertIn('missing=', self.SOURCE)
        self.assertIn('differ=', self.SOURCE)

    def test_it_fails_when_anything_is_wrong(self):
        self.assertIn('exit 1', self.SOURCE)
        self.assertIn('STAGED-FILES-FAIL', self.SOURCE)

    def test_it_refuses_an_empty_manifest(self):
        """空清单一律通过 —— 那正是最危险的情况。"""
        self.assertIn('checked', self.SOURCE)
        self.assertIn('清单里没有任何可校验的条目', self.SOURCE)

    def test_it_tolerates_crlf_manifests(self):
        self.assertIn("${line%$'\\r'}", self.SOURCE)

    def test_it_skips_comments_and_blank_lines(self):
        self.assertIn("'#'*", self.SOURCE)

    def test_it_accepts_sha256sum_star_prefix(self):
        self.assertIn('rel=${rel#\\*}', self.SOURCE)


class TheLessonIsRecordedTests(unittest.TestCase):
    """把这个缺陷本身写进脚本注释，后来人不必重新踩一遍。"""

    def test_the_reason_is_documented(self):
        source = CHECKER.read_text(encoding='utf-8')
        self.assertIn('403', source)
        self.assertIn('非空', source)


class ExistingDeployToolsAreUntouched(unittest.TestCase):
    def test_signed_release_verifier_still_present(self):
        self.assertTrue((DEPLOY / 'verify_signed_release.py').is_file())

    def test_stage_script_still_present(self):
        self.assertTrue((DEPLOY / 'prepare-managed-stage.sh').is_file())

    def test_retry_script_still_present(self):
        self.assertTrue((DEPLOY / 'prepare-retry.sh').is_file())


if __name__ == '__main__':
    unittest.main()
