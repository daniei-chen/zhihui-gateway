"""v1.2.109：同一个 secrets 目录，三套读取标准，最松的那套用在最要紧的凭据上。

## 发现经过

上一版修掉了「两个入口读同一个密钥、校验不一致」。顺着这条线把**所有**读凭据的
地方列出来，看到三套并存的实现：

| 位置 | 校验 |
| --- | --- |
| `identity_signing_key()` | 符号链接 + 常规文件 + 大小 + 权限 |
| `steward_config.secret_file()` | 上面全部 **+ `st_nlink` + 属主** |
| **本文件 `_read()`** | **无（裸 `Path.read_text()`）** |

而 `_read()` 的五个调用点里，**四个读的是凭据**：

| 调用点 | 读的东西 |
| --- | --- |
| 内部服务令牌（含 `.next` 双值） | 凭据 |
| 主令牌 | 凭据 |
| 上一版密钥（`.previous`） | 凭据 |
| 主密钥 | 凭据 |
| 撤销计数 | **非机密** |

## 为什么这算缺陷

攻击者若要动手，只需要挑最松的入口 —— **而这里正是它**，且它守的是服务令牌与签名密钥。

## 为什么不会打断现有部署

2026-11-15 核过生产：secrets 目录 `0700`，其中每个文件都是 `0600` 或 `0400`，
属主 `10001`（服务自己的 uid）。**四个凭据点读到的文件全部满足严格校验的每一条。**

所以收紧不改变现有行为，只是把「将来有人把权限配成 0640」从「静默继续用」
变成「读不到 → 鉴权失败」。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 四个凭据点走严格读取 | 又回到裸读 |
| **撤销计数仍走宽松读取** | 它不是机密，误改会让撤销机制失效 |
| 符号链接被拒 | 可指向攻击者控制的文件 |
| 权限过宽被拒 | 组或其他可读的令牌仍被使用 |
| 正常文件照常可读 | 修过头把鉴权改坏 |
"""
from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path

from server.qtgcore import security


def _code_lines(func) -> list[str]:
    out = []
    for line in inspect.getsource(func).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        out.append(line)
    return out


class WhichReaderEachSiteUsesTests(unittest.TestCase):
    """区分「凭据」与「非机密」是本版的核心 —— 两者用错一个都会出问题。"""

    def setUp(self):
        self.text = Path(security.__file__).read_text(encoding="utf-8")
        self.lines = self.text.splitlines()

    def _call_lines(self):
        return [(i, ln.strip()) for i, ln in enumerate(self.lines, 1)
                if '_read(' in ln or '_read_secret(' in ln]

    def test_credential_sites_use_the_strict_reader(self):
        strict = [ln for _i, ln in self._call_lines() if '_read_secret(' in ln]
        self.assertGreaterEqual(len(strict), 4,
                                '四个凭据点都该走严格读取；少了就是漏改')

    def test_revoke_counter_keeps_the_plain_reader(self):
        """撤销计数不是机密，权限过宽也不该让它失效。"""
        revoke = [ln for _i, ln in self._call_lines() if '_revoke' in ln]
        self.assertTrue(revoke, '没找到撤销计数的读取行')
        self.assertIn('_read(', revoke[0])
        self.assertNotIn('_read_secret(', revoke[0],
                         '撤销计数不该被权限校验挡住')

    def test_plain_reader_is_still_used_somewhere(self):
        """如果 `_read()` 完全没人用了，说明改过头把非机密也收紧了。"""
        uses = [ln for _i, ln in self._call_lines() if '_read(' in ln]
        self.assertTrue(uses, '`_read()` 应当仍有非机密调用点')


class TheStrictReaderChecksWhatMattersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / 'secret'

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, content, mode=0o600):
        self.path.write_text(content, encoding='utf-8')
        try:
            self.path.chmod(mode)
        except OSError:
            pass

    def test_healthy_file_is_read(self):
        self._write('k' * 48)
        if os.name != 'posix':
            self.skipTest('权限校验只在 POSIX 上成立')
        self.assertEqual(security._read_secret(str(self.path)), 'k' * 48)

    def test_missing_file_returns_empty(self):
        self.assertEqual(security._read_secret(str(self.path)), '')

    def test_oversized_is_rejected(self):
        self._write('k' * 5000)
        self.assertEqual(security._read_secret(str(self.path)), '')

    def test_custom_limit_is_respected(self):
        self._write('k' * 100)
        self.assertEqual(security._read_secret(str(self.path), limit=50), '')

    @unittest.skipUnless(os.name == 'posix', '权限位只在 POSIX 上有意义')
    def test_group_readable_is_rejected(self):
        self._write('k' * 48, mode=0o640)
        self.assertEqual(security._read_secret(str(self.path)), '',
                         '组可读的令牌不能被使用')

    @unittest.skipUnless(os.name == 'posix', '符号链接只在 POSIX 上构造')
    def test_symlink_is_rejected(self):
        target = self.dir / 'target'
        target.write_text('k' * 48, encoding='utf-8')
        try:
            target.chmod(0o600)
        except OSError:
            pass
        self.path.symlink_to(target)
        self.assertEqual(security._read_secret(str(self.path)), '',
                         '符号链接必须被拒')

    def test_directory_is_rejected(self):
        """目录不是常规文件，读它没有意义，也不能当成空令牌放行。"""
        self.assertEqual(security._read_secret(str(self.dir)), '')


class TheGapIsDocumentedTests(unittest.TestCase):
    def test_docstring_names_the_three_implementations(self):
        source = inspect.getsource(security._read_secret)
        self.assertIn('steward_config', source)
        self.assertIn('identity_signing_key', source)

    def test_docstring_says_why_it_does_not_raise(self):
        source = inspect.getsource(security._read_secret)
        self.assertIn('500', source,
                      '要说明为什么选「失败关闭」而不是抛异常')

    def test_docstring_records_the_production_check(self):
        source = inspect.getsource(security._read_secret)
        self.assertIn('0600', source,
                      '要写明生产实况，否则后人不敢收紧')


if __name__ == '__main__':
    unittest.main()
