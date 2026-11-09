"""继承下载器的字节校验与当前受控发布文档的回归测试。

verify_download 只核对下载摘要/大小，不是生产安装授权。缺摘要的历史兼容
分支仍标注未比对，但当前受控实例另有安装拒绝、Ed25519、完整测试回执和
外部可信发布器门禁。旧 OpenSSH helper 不复活，也不能要求文档取消现用签名。

校验使用临时真实文件，不替换共享标准库、不连接生产。
"""
from __future__ import annotations

import hashlib
import importlib.util
import tarfile
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_update_mod():
    """加载 deploy/update.py（每次返回全新模块对象，测试之间无共享状态）。"""
    spec = importlib.util.spec_from_file_location('upd', str(_ROOT / 'deploy' / 'update.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Rep:
    """收集日志，便于断言「是否告警 / 报错文案」。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.integrity: dict | None = None

    def log(self, msg: str, level: str = 'info') -> None:
        self.lines.append((level, msg))

    def set_integrity(self, status: str, digest: str = '') -> None:
        self.integrity = {'status': status, 'digest': digest}

    def warned(self) -> bool:
        return any(lvl == 'warn' for lvl, _ in self.lines)

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)


class DownloadIntegrityTest(unittest.TestCase):
    """真实调用 verify_download：不改标准库、不联网。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.mod = _load_update_mod()
        self.pkg = self.dir / 'pkg.tar.gz'
        # 用真实 tar.gz（而非随机字节）：万一将来校验被挪到解压之后，这条会红
        with tarfile.open(self.pkg, 'w:gz') as tf:
            payload = self.dir / 'payload.txt'
            payload.write_text('hello\n', encoding='utf-8')
            tf.add(payload, arcname='pkg/payload.txt')
        self.digest = 'sha256:' + hashlib.sha256(self.pkg.read_bytes()).hexdigest()
        self.info = {'pkg_size': self.pkg.stat().st_size, 'pkg_digest': self.digest}

    def test_matching_digest_passes_and_is_recorded(self) -> None:
        rep = _Rep()
        self.mod.verify_download(self.pkg, self.info, rep)
        self.assertEqual(rep.integrity['status'], 'digest')
        self.assertEqual(rep.integrity['digest'],
                         hashlib.sha256(self.pkg.read_bytes()).hexdigest())
        self.assertFalse(rep.warned(), '一切正常时不该告警')

    def test_tampered_package_is_refused(self) -> None:
        """包在传输途中被换掉：摘要对不上就必须中止（这是这一关的全部意义）。"""
        data = bytearray(self.pkg.read_bytes())
        data[len(data) // 2] ^= 0xFF
        self.pkg.write_bytes(bytes(data))
        rep = _Rep()
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.verify_download(self.pkg, self.info, rep)
        self.assertIn('sha256 与发布页不一致', str(ctx.exception))
        self.assertIn('发布页：', str(ctx.exception))
        self.assertIsNone(rep.integrity, '拒绝时不该记成「已校验」')

    def test_truncated_download_is_refused_with_retry_hint(self) -> None:
        """下载被截断（实测最常见的事故）：说清「再点一次更新」，别让人去查发布流程。"""
        rep = _Rep()
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.verify_download(self.pkg, {**self.info, 'pkg_size': 999_999_999}, rep)
        msg = str(ctx.exception)
        self.assertIn('大小不符', msg)
        self.assertIn('再点一次更新', msg)

    def test_missing_digest_installs_but_says_not_compared(self) -> None:
        """回退路径拿不到摘要：照装（否则 API 被限流就没法更新），但如实标注。"""
        rep = _Rep()
        self.mod.verify_download(self.pkg, {'pkg_size': None, 'pkg_digest': ''}, rep)
        self.assertEqual(rep.integrity['status'], 'none')
        self.assertTrue(rep.warned(), '没比对摘要却不当回事 —— 应该告警')
        self.assertIn('未比对发布页摘要', rep.text())
        self.assertIn(hashlib.sha256(self.pkg.read_bytes()).hexdigest(), rep.text(),
                      '要打出本地摘要，用户才能自己跟发布页比对')

    def test_digest_is_checked_before_unpacking(self) -> None:
        """顺序：先校验（下载 + 摘要），再解压 —— 不能先解压再看摘要。"""
        src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        body = src.split('def _release_root(', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('verify_download(', body, '下载后没有校验')
        self.assertLess(body.index('verify_download('), body.index('tarfile.open'),
                        '解压排在了校验之前 —— 半截/被换过的包会先进文件系统')


class LegacyUpdaterBoundaryTest(unittest.TestCase):
    """区分已移除的 OpenSSH 下载 helper 与当前受信 Ed25519 发布合同。"""

    def test_key_and_helpers_are_removed(self) -> None:
        src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        # 注释里允许提到历史（「历史上这里还有一层…签名」），代码符号不允许留
        for token in ('RELEASE_PUBKEY', 'SKIP_SIGNATURE', 'RELEASE_SIGNER_ID',
                      'def check_signature', 'def download_signature',
                      'def verify_release_signature', 'ssh-keygen'):
            self.assertNotIn(token, src, f'{token} 仍在 deploy/update.py 里')
        self.assertFalse((_ROOT / 'deploy' / 'release-signing-key.pub').exists(),
                         '公钥文件还在仓库里')

    def test_verify_script_checks_sha256(self) -> None:
        src = (_ROOT / 'deploy' / 'verify-release.sh').read_text(encoding='utf-8')
        self.assertIn('sha256sum', src)
        # 注释里可以讲历史（「原来的 ssh-keygen 验签已取消」），但不能真去调它
        self.assertNotIn('command -v ssh-keygen', src)
        self.assertNotIn('ssh-keygen -Y', src)

    def test_release_notes_publish_the_digest(self) -> None:
        """发布说明里要印出摘要 —— 用户想手动核对时得有地方对。"""
        src = (_ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
        self.assertIn('sha256sum', src)
        self.assertIn('产物校验（sha256）', src)
        self.assertNotIn('tar.gz.sig', src, '工作流里还留着签名资产的处理逻辑')

    def test_docs_require_current_ed25519_release_contract(self) -> None:
        doc = (_ROOT / 'docs' / 'release-signing.md').read_text(encoding='utf-8')
        for requirement in ('sha256', 'Ed25519', '完整隔离测试',
                            '签名或回执时阻断', 'root-owned', '业务数据库'):
            self.assertIn(requirement, doc, '文档缺少当前受控发布要求: ' + requirement)


if __name__ == '__main__':
    unittest.main()
