"""v1.2.108：nginx auth_request 那个入口读密钥时少了一整套校验。

## 发现经过

静态扫「`open()` 没有 `with`」时看到这一行：

```python
secret = open('/opt/workbuddy-manager/engine/secrets/identity_signing.key').read().strip()
```

它在 `internal_session_verify` 里 —— **nginx `location /api/` 的 `auth_request`
目标**，也就是每个 `/api/` 请求都会经过它。

而同一个密钥文件在 `edgeidentity._secret()` 里被读时，做了六项校验：

| 校验 | 裸打开 | `edgeidentity._secret()` |
| --- | --- | --- |
| 拒绝符号链接 | ✗ | ✓ `O_NOFOLLOW` + `lstat` |
| 必须是常规文件 | ✗ | ✓ `S_ISREG` |
| 大小上限 4096 | ✗ | ✓ |
| **权限位 `0o077` 必须为空** | **✗** | **✓** |
| 有界读取 | ✗ | ✓ `os.read(fd, 4097)` |
| 显式关闭 | ✗（靠 GC） | ✓ `finally` |

**「权限位必须为空」那条最要紧**：密钥文件被配成组或其他可读时，姐妹实现会**拒绝
签发**，而这个入口**照样签**。

两个入口读同一个密钥、校验却不一样 —— **本身就构成攻击面**：攻击者只需要挑宽松的那个。

## 本版

改成复用 `edgeidentity._secret()`。两份实现从此共用同一套校验。

**没有动 `edgeidentity`**：它已有 9 个测试，包括密钥轮换生效、以及
「缺失/过短/权限过宽都必须失败关闭且不泄漏路径」。
"""
from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config, edgeidentity
from server.routers import system


def _code_lines(func) -> list[str]:
    """去掉注释和空行后的源码行 —— 注释里提到 `open()` 不该被判成在用 `open()`。"""
    out = []
    for line in inspect.getsource(func).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        out.append(line)
    return out


class TheEntryPointUsesTheHardenedReaderTests(unittest.TestCase):
    def test_it_calls_the_shared_reader(self):
        source = inspect.getsource(system.internal_session_verify)
        self.assertIn('edgeidentity', source,
                      '要复用 edgeidentity 的读取逻辑，不是自己再写一遍')

    def test_no_bare_open_in_code(self):
        """只看代码行：注释里解释「原来那个裸 open」是正常的。"""
        code = '\n'.join(_code_lines(system.internal_session_verify))
        self.assertNotIn('open(', code,
                         '裸打开会丢掉 O_NOFOLLOW 与权限校验')

    def test_failure_still_fails_closed(self):
        code = '\n'.join(_code_lines(system.internal_session_verify))
        self.assertIn('503', code, '读不到密钥必须 503，不能放行')

    def test_it_no_longer_hardcodes_the_path(self):
        """路径由 config 决定；写死在路由里会让测试和部署都无法覆盖它。"""
        code = '\n'.join(_code_lines(system.internal_session_verify))
        self.assertNotIn('/opt/workbuddy-manager/engine/secrets', code)


class TheSharedReaderChecksWhatMattersTests(unittest.TestCase):
    """直接验 `_secret()` 的六项校验 —— 现在是唯一入口，破了两个端点一起破。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / 'identity.key'
        self.patch = mock.patch.object(config, 'QTG_IDENTITY_SECRET_FILE', str(self.path))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _read(self):
        return edgeidentity._secret()

    def _write(self, content, mode=0o600):
        self.path.write_text(content, encoding='utf-8')
        try:
            self.path.chmod(mode)
        except OSError:
            pass

    def test_healthy_key_is_returned(self):
        good = 'k' * 48
        self._write(good)
        if os.name == 'posix':
            self.assertEqual(self._read(), good)
        else:
            self.skipTest('权限校验只在 POSIX 上成立')

    def test_too_short_is_rejected(self):
        self._write('short')
        self.assertEqual(self._read(), '')

    def test_whitespace_inside_is_rejected(self):
        self._write('k' * 20 + ' ' + 'k' * 20)
        self.assertEqual(self._read(), '')

    def test_oversized_is_rejected(self):
        self._write('k' * 5000)
        self.assertEqual(self._read(), '')

    def test_missing_file_is_rejected(self):
        self.assertEqual(self._read(), '')

    @unittest.skipUnless(os.name == 'posix', '权限位只在 POSIX 上有意义')
    def test_group_or_world_readable_is_rejected(self):
        """这条最要紧：裸打开会照签，共享读取器必须拒。"""
        self._write('k' * 48, mode=0o644)
        self.assertEqual(self._read(), '', '权限过宽的密钥不能被使用')

    @unittest.skipUnless(os.name == 'posix', '符号链接只在 POSIX 上构造')
    def test_symlink_is_rejected(self):
        target = self.dir / 'symlink-target.key'
        target.write_text('k' * 48, encoding='utf-8')
        try:
            target.chmod(0o600)
        except OSError:
            pass
        self.path.symlink_to(target)
        self.assertEqual(self._read(), '', '符号链接必须被拒')


class TheGapIsDocumentedTests(unittest.TestCase):
    def test_source_explains_why_the_shared_reader_is_used(self):
        source = inspect.getsource(system.internal_session_verify)
        self.assertIn('O_NOFOLLOW', source)
        self.assertIn('0o077', source)

    def test_source_names_the_damage(self):
        """要写明「权限配错时照样签发」这个具体后果，否则后人看不出为什么要改。"""
        source = inspect.getsource(system.internal_session_verify)
        self.assertIn('权限', source)

    def test_source_says_why_not_to_duplicate(self):
        source = inspect.getsource(system.internal_session_verify)
        self.assertIn('攻击面', source,
                      '要说清「两套校验不一致本身就是攻击面」')


if __name__ == '__main__':
    unittest.main()
