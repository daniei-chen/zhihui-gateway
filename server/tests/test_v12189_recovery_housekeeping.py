"""v1.2.189：修一个让「快照清理」永久失效的 bug。

## 事实

2026-11-15：`zhihui-recovery-housekeeping.service` 连续失败。

```
FileNotFoundError: '/root/backups/zhihui-v2/snapshots/20261115-061320/snapshot.json'
```

遍历快照目录时遇到一个**半成品目录**（生成中途被中断留下的，缺 `snapshot.json`），
而 `read_json` 抛的是 `FileNotFoundError` —— 那里只预期 `ValueError`。

## 后果不是「少删几个文件」

**是清理从此永久失效**：每天都会再遇到同一个半成品，每天在同一个地方崩，
旧快照一天天堆积，磁盘慢慢涨。**而它只是每天凌晨失败一次，没有人被通知到。**

## 修的界限

- **不完整**（缺元数据）→ 跳过并记录
- **不安全**（权限错、符号链接、非预期类型）→ **仍然中止**

**把这两者混为一谈，等于为了「不崩溃」而放过安全问题。**
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / 'deploy' / 'recovery_housekeeping.py'
SRC = PATH.read_text(encoding='utf-8')


class ItSkipsIncompleteSnapshotsTests(unittest.TestCase):
    def test_incomplete_list_exists(self):
        self.assertIn('incomplete = []', SRC)

    def test_it_checks_before_reading(self):
        """先看文件在不在，再去读 —— 顺序反了就没有防护。"""
        idx_check = SRC.find("(path / 'snapshot.json').is_file()")
        idx_read = SRC.find("meta = read_json(path / 'snapshot.json')")
        self.assertGreater(idx_check, 0)
        self.assertGreater(idx_read, 0)
        self.assertLess(idx_check, idx_read)

    def test_it_catches_filenotfound_too(self):
        """检查与读取之间被删掉，同样是不完整。"""
        idx = SRC.find('except FileNotFoundError:')
        self.assertGreater(idx, 0)
        window = SRC[idx:idx + 200]
        self.assertIn('incomplete.append', window)

    def test_incomplete_is_reported(self):
        self.assertIn("'incomplete_snapshots': incomplete", SRC)


class ItStillFailsClosedOnUnsafeTests(unittest.TestCase):
    """安全性问题必须继续中止 —— 这是这次修改的边界。"""

    def test_unsafe_descendant_still_raises(self):
        self.assertIn("raise ValueError('unsafe snapshot descendant')", SRC)

    def test_lacks_restore_validation_still_raises(self):
        self.assertIn("raise ValueError('snapshot lacks restore validation')", SRC)

    def test_the_contract_is_documented(self):
        self.assertIn("'invalid_or_unsafe_snapshot': 'fail_closed'", SRC)


class ItExplainsWhyThisMattersTests(unittest.TestCase):
    def test_it_says_the_failure_is_permanent(self):
        self.assertIn('永久失效', SRC)

    def test_it_says_nobody_was_notified(self):
        self.assertIn('没有人被通知到', SRC)


class ItStillParsesTests(unittest.TestCase):
    def test_syntax_is_valid(self):
        ast.parse(SRC)


if __name__ == '__main__':
    unittest.main()
