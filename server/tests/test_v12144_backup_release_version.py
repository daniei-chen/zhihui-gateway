"""v1.2.144：恢复点里必须能分清「安装根版本」和「发布版本」。

## 发现过程

查恢复体系能不能真的恢复：15 个密文分片逐个校验全过、拼接后解密成功、
归档 19376 个成员、tar 完整性通过、两个 `manager.db` 都在。
**链路是通的。**

然后看到归档里 `manager/.version` 写的是 `v1.2.2+audit.20261112.1`，
而线上跑的是 `v1.2.143`。

## 这不是数据错误

`backup_application.py` 把 `/opt/workbuddy-manager/.version` 打进了 `manager/.version`。
那一份记录的是**安装根**被铺开的时间，不是正在服务的发布版本。

两个都是真实信息，`/opt/gateway-ops/scripts/backup-gateway-config.sh`
也一直分开存（`version-install-root` 与 `version-release`）。

**问题在于主备份只带了前者，还没标注它是哪一个。**
恢复时看到 `manager/.version`，很自然会以为那就是应用版本 ——
而它比实际运行的版本低了 141 个。

## 改法

发布版本单独落一份 `manager/release-version.txt`，名字直接说清是什么。

不改 `manager/.version` 的含义 —— 它是安装根的记录，本身没错。
**只补上缺失的那一半。**

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 备份里带发布版本 | 恢复时只能看到安装根版本，误判 |
| 它与 `.version` 是两个文件 | 覆盖 `.version` 会丢掉安装根的记录 |
| 文件不存在时不报错 | 某些部署形态没有 `releases/current` |
| 读的是 `releases/current` | 读别处会拿到不是「正在服务」的那个 |
"""
from __future__ import annotations

import unittest
from pathlib import Path

# `backup_application` 顶层 import fcntl，那是 Unix-only，Windows 上导不进来。
# 这里只读源码文本做断言 —— 被测的那段逻辑本身与平台无关。
SOURCE = (Path(__file__).resolve().parents[1] / 'backup_application.py').read_text(
    encoding='utf-8')


class TheReleaseVersionIsRecordedTests(unittest.TestCase):
    def setUp(self):
        self.src = SOURCE

    def test_it_writes_a_release_version_entry(self):
        self.assertIn('manager/release-version.txt', self.src,
                      '备份里没有发布版本 —— 恢复时只能看到安装根版本')

    def test_it_reads_from_releases_current(self):
        idx = self.src.find('manager/release-version.txt')
        window = self.src[max(0, idx - 500):idx]
        self.assertIn("'releases'", window)
        self.assertIn("'current'", window)

    def test_it_does_not_overwrite_the_install_root_version(self):
        """`manager/.version` 是安装根的记录，不能被顶掉。"""
        self.assertNotIn("arcname='manager/.version'", self.src)
        idx = self.src.find('manager/release-version.txt')
        window = self.src[max(0, idx - 200):idx]
        self.assertNotIn("'manager/.version'", window)


class ItHandlesMissingFilesTests(unittest.TestCase):
    def setUp(self):
        self.src = SOURCE

    def test_it_checks_the_file_exists(self):
        idx = self.src.find('manager/release-version.txt')
        window = self.src[max(0, idx - 600):idx]
        self.assertIn('is_file()', window)

    def test_it_rejects_symlinks(self):
        idx = self.src.find('manager/release-version.txt')
        window = self.src[max(0, idx - 600):idx]
        self.assertIn('is_symlink()', window)


class TheReasonIsWrittenDownTests(unittest.TestCase):
    def test_the_comment_explains_the_two_versions(self):
        src = SOURCE
        idx = src.find('manager/release-version.txt')
        window = src[max(0, idx - 1400):idx]
        self.assertIn('v1.2.2', window, '没写出实测到的那个差异，后来人会再查一遍')
        self.assertIn('v1.2.143', window)


if __name__ == '__main__':
    unittest.main()
