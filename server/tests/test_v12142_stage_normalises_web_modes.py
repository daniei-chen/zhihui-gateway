"""v1.2.142：发布脚本在前端产物签名前归一化权限。

## 上一版踩了什么

`tar xzf` 把归档里的 777 一并保留，于是 `web/out` 下**每个目录**都成了
world-writable（实测 136 个目录 + 260 个文件）。

主机侧验证器会走一遍路径**及其全部父目录**，拒绝任何 group/other 可写的路径。
它只报一句「外部发布验证失败」。

**而我把签名、摘要、文件清单逐一核对，全部正常** —— 看起来像验证器坏了。
差的是权限位：**签名管的是「内容对不对」，验证器还管「路径安不安全」**。

## 为什么不只是「下次注意」

这类失败**只在解压方式变化时出现**。这次是 tar，上次是 cp -a，下次可能是
rsync 或某个 CI 的缓存。**每次都要重新查一遍同一个位置**。

**把归一化放进签名之前，这个失败形态就从流程里消失了。**

## 插在哪、为什么

放在 `create-web-manifest.py` 之前 —— 那个脚本会生成前端签名清单。

顺序很重要：**先修权限，再签名**。反过来的话，签名清单描述的是修之前的树，
而验证器比对的是修之后的树。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 目录被规范到 755 | world-writable 目录再次让部署在签名全对的情况下失败 |
| 文件被规范到 644 | 同上（文件带 group/other 写也会被拒） |
| 发生在签名之前 | 签名清单与实际树不一致 |
| 目录不存在时不报错 | 纯后端发布（没有前端产物）会挂 |
| 不吞掉真正的失败 | 权限改不动时应当能看出来 |
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'prepare-managed-stage.sh'


class TheNormalisationExistsTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding='utf-8')

    def test_directories_become_755(self):
        self.assertIn('-type d -exec chmod 755', self.text)

    def test_files_become_644(self):
        self.assertIn('-type f -exec chmod 644', self.text)

    def test_ownership_is_normalised_too(self):
        """验证器同时拒绝「不是 root 拥有」的路径。

        chmod 改得再对，只要属主不是 root，一样被拒。
        writer-guard 的 normalize_modes 一直是 chown + chmod 一起做的，
        这里补齐同一口径。
        """
        idx = self.text.find('-type d -exec chmod 755')
        window = self.text[max(0, idx - 300):idx]
        self.assertIn('chown -R 0:0', window,
                      '只改了权限没改归属 —— 解压树带着别的 uid 时仍会被拒')

    def test_it_targets_web_out(self):
        idx = self.text.find('-type d -exec chmod 755')
        window = self.text[max(0, idx - 600):idx]
        self.assertIn('web/out', window)

    def test_it_is_guarded_by_a_directory_check(self):
        """纯后端发布没有 web/out，不能因此失败。"""
        idx = self.text.find('-type d -exec chmod 755')
        window = self.text[max(0, idx - 800):idx]
        self.assertIn('-d "$root/web/out"', window)


class ItRunsBeforeSigningTests(unittest.TestCase):
    """先修权限、再签名 —— 反过来的话清单描述的是修之前的树。"""

    def test_chmod_precedes_manifest_creation(self):
        text = SCRIPT.read_text(encoding='utf-8')
        chmod_at = text.find('-type d -exec chmod 755')
        # 找**调用点**，不是前面那句存在性检查 [ -f ... ]
        call_at = text.find('"$root/deploy/create-web-manifest.py" "$root/web/out"')
        self.assertGreater(chmod_at, 0)
        self.assertGreater(call_at, 0, '没找到 create-web-manifest 的调用点')
        self.assertLess(chmod_at, call_at,
                        '权限归一化在签名之后 —— 清单会与最终树不一致')


class ItDoesNotHideRealFailuresTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding='utf-8')

    def test_script_still_fails_fast(self):
        """脚本整体仍然是 set -eu 语义，非法权限只是被规范化而不是被忽略。"""
        self.assertTrue(re.search(r'^set -[a-z]*e', self.text, re.M),
                        '脚本不再 fail-fast')

    def test_the_reason_is_written_down(self):
        idx = self.text.find('-type d -exec chmod 755')
        window = self.text[max(0, idx - 900):idx]
        self.assertIn('777', window, '没写出这个失败的成因，后来人会再查一遍')
        self.assertIn('验证失败', window)


if __name__ == '__main__':
    unittest.main()
