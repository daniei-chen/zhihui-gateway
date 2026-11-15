"""重签清单必须同时同步 `release_id`，否则服务起不来。

## 这条测试挡的是什么

启动门禁（`writer-guard/state_guard.py` 的 `inspect_release`）同时要求：

    manifest['version']    == 发布目录名
    manifest['release_id'] == 发布目录名

而 `deploy/sign-release-ed25519.py --rebuild` 原先**只同步 `version`**，
`release_id` 停在旧值。于是「把 v1.2.198 复制成 v1.3.2 再重签」这个最自然的
发布动作，产出的清单会被门禁判为 `verified release identity mismatch`，
服务直接 `WRITER-LAUNCH-BLOCKED` 退出。

2026-11-16 实测代价：网关连续 15 分钟不可用，期间所有模型调用失败。

## 为什么断言源码而不是跑一遍

跑通需要私钥、信任锚、一个完整的 716 文件发布目录 —— 那是发布主机的活，
不该进单元测试。这条断言只守**一件事**：签名脚本里存在同步 `release_id`
的语句，且它写的是 `root.name`。删掉它就会红。
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SIGNER = ROOT / 'deploy' / 'sign-release-ed25519.py'


def _load_signer():
    """按路径加载签名脚本。

    不能 `from deploy import ...`：`deploy/` 不是 Python 包（没有 `__init__.py`），
    而文件名里的连字符也不是合法模块名。发布工具本来就是脚本，不是库。
    """
    spec = importlib.util.spec_from_file_location('sign_release_ed25519', SIGNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManifestIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not SIGNER.is_file():
            raise unittest.SkipTest('签名脚本只存在于完整发布树')
        cls.signer = _load_signer()

    def test_rebuild_syncs_release_id_to_directory_name(self) -> None:
        source = inspect.getsource(self.signer.rebuild_json_manifest)

        self.assertIn(
            "release['release_id'] = root.name", source,
            'rebuild 必须把 release_id 同步成发布目录名；只改 version 会让门禁'
            '判为 verified release identity mismatch，服务起不来',
        )
        self.assertIn(
            "release['version'] = actual_version", source,
            'version 的同步不能被这条修复挤掉',
        )

    def test_release_id_is_compared_against_directory_name(self) -> None:
        """门禁侧也确认一下：它比的确实是目录名。

        如果哪天门禁改成不再校验 `release_id`，这条会失败——那时本修复可以
        连同它的注释一起去掉，而不是留一条没人需要的行为。
        """
        from pathlib import Path
        guard = Path('/opt/zhihui-release/writer-guard/state_guard.py')
        if not guard.is_file():
            self.skipTest('writer-guard 只存在于发布主机')
        text = guard.read_text(encoding='utf-8')
        self.assertIn("value['release_id'] != root.name", text)


if __name__ == '__main__':
    unittest.main()
