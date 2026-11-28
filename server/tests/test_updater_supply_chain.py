"""更新器的供应链边界（DAN-24）：发布源、签名、以及 pip 的输入。

病灶是三件事凑齐才成立的：更新器默认从第三方仓库取 latest 发布、拿到包就以
装机权限跑 `pip install -r`、唯一的屏障是 `deploy/managed-release.json` marker。
marker 在仓库里是被跟踪的，所以受控安装树里自助升级本来就会退出；真正暴露的是
「marker 被删」或「不是从受控发布装出来的树」。这三条各自都要有自己的用例，
而且都必须断言**行为**：安装目录有没有被动过、pip 有没有被调用。

  · 发布源：默认必须是本项目仓库，且环境变量不能静默把它指向第三方；
  · 签名：包没有 Ed25519 detached 签名、或签名验不过（哪怕发布页把摘要一并
    改成被篡改字节的摘要），都必须中止且一个字节不落盘、pip 一次都不运行；
  · marker：受控安装树里必须继续拒绝自助升级（防止修复过程中退化）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from server import product  # noqa: E402

OFFICIAL_REPO = 'daniei-chen/zhihui-gateway'
THIRD_PARTY_REPO = 'ithtelab/workbuddy-manager'


class _Rep:
    """与 `deploy/update.py` 的 Reporter 同接口的最小替身。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.state: dict = {}
        self.integrity: dict = {}

    def log(self, message: str, level: str = 'info') -> None:
        self.lines.append((level, message))

    def step(self, name: str) -> None:
        self.lines.append(('info', f'== {name} =='))

    def set_target_version(self, tag: str) -> None:
        self.state['target_version'] = str(tag or '').strip().lstrip('vV')

    def set_integrity(self, status: str, digest: str = '') -> None:
        self.integrity = {'status': status, 'digest': digest}

    def finish(self, ok: bool) -> None:
        self.state['ok'] = ok

    def text(self) -> str:
        return '\n'.join(message for _, message in self.lines)


def _openssl(*args: str) -> None:
    proc = subprocess.run(['openssl', *args], capture_output=True, timeout=60)
    if proc.returncode:
        raise AssertionError(
            f'openssl {" ".join(args)} 失败：{proc.stderr.decode("utf-8", "replace")[:300]}')


class UpdaterSupplyChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which('openssl'):
            raise unittest.SkipTest('需要 openssl（Ed25519 签名/验签）')
        cls._keys = tempfile.TemporaryDirectory(prefix='updater-supply-keys-')
        keys = Path(cls._keys.name)
        for name in ('release', 'rogue'):
            _openssl('genpkey', '-algorithm', 'ed25519', '-out', str(keys / f'{name}.pem'))
            _openssl('pkey', '-in', str(keys / f'{name}.pem'),
                     '-pubout', '-out', str(keys / f'{name}.pub.pem'))
        cls.release_key = keys / 'release.pem'
        cls.release_anchor = keys / 'release.pub.pem'
        cls.rogue_key = keys / 'rogue.pem'

    @classmethod
    def tearDownClass(cls) -> None:
        cls._keys.cleanup()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='updater-supply-')
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    # ── 夹具 ──

    def _make_install(self, name: str) -> Path:
        """造一个"已部署"的安装目录（旧版本，没有 managed-release marker）。"""
        inst = self.dir / name / 'install'
        (inst / 'server').mkdir(parents=True)
        (inst / 'server' / 'main.py').write_text("version='1.0.0'\n# OLD\n", encoding='utf-8')
        (inst / 'server' / 'requirements.txt').write_text('', encoding='utf-8')
        (inst / 'web' / 'out').mkdir(parents=True)
        (inst / 'web' / 'out' / 'index.html').write_text('<html>OLD</html>', encoding='utf-8')
        (inst / 'deploy').mkdir()
        (inst / '.version').write_text('v1.0.0\n', encoding='utf-8')
        return inst

    def _make_release(self, name: str, *, version: str = '9.9.9') -> tuple[Path, str]:
        """打一个真实发布包，返回 (tar.gz, 'sha256:…')。"""
        stage = self.dir / name / f'workbuddy-manager-v{version}'
        (stage / 'server').mkdir(parents=True)
        (stage / 'server' / 'main.py').write_text(f"version='{version}'\n# NEW\n", encoding='utf-8')
        (stage / 'server' / 'requirements.txt').write_text('', encoding='utf-8')
        (stage / 'server' / 'big.bin').write_bytes(os.urandom(200_000))
        (stage / 'web' / 'out').mkdir(parents=True)
        (stage / 'web' / 'out' / 'index.html').write_text('<html>NEW</html>', encoding='utf-8')
        (stage / '.version').write_text(f'v{version}\n', encoding='utf-8')
        pkg = self.dir / name / f'workbuddy-manager-v{version}.tar.gz'
        subprocess.run(['tar', 'czf', str(pkg), '-C', str(stage.parent), stage.name],
                       check=True, capture_output=True)
        return pkg, 'sha256:' + hashlib.sha256(pkg.read_bytes()).hexdigest()

    def _sign(self, pkg: Path, key: Path | None = None) -> Path:
        signature = pkg.with_name(pkg.name + '.sig')
        _openssl('pkeyutl', '-sign', '-inkey', str(key or self.release_key),
                 '-rawin', '-in', str(pkg), '-out', str(signature))
        return signature

    def _load(self, install: Path, **env):
        """按环境装载更新器（它读环境变量定常量）。"""
        keys = ['WB_INSTALL_DIR', 'WB_DATA_DIR', 'WB_UPDATE_STATUS', 'WB_SERVICE_NAME',
                'WB_MANAGER_REPO', 'WB_RELEASE_TRUST', *env.keys()]
        old = {k: os.environ.get(k) for k in keys}
        settings = {
            'WB_INSTALL_DIR': str(install),
            'WB_DATA_DIR': str(install / 'data'),
            'WB_UPDATE_STATUS': str(install / 'data' / 'update-status.json'),
            'WB_SERVICE_NAME': 'workbuddy-web-does-not-exist',
            'WB_RELEASE_TRUST': str(self.release_anchor),
            **env,
        }
        try:
            os.environ.update({k: str(v) for k, v in settings.items()})
            spec = importlib.util.spec_from_file_location(
                f'upd_supply_{abs(hash(str(install)))}', _ROOT / 'deploy' / 'update.py')
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            for k, value in old.items():
                if value is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = value
        return mod

    def _wire_release(self, mod, pkg: Path, digest: str, *, signature: Path | None,
                      version: str = '9.9.9') -> None:
        """接上假 Release：资产用 file:// URL，下载与校验全程真跑。"""
        assets = [{'name': pkg.name, 'browser_download_url': pkg.as_uri(),
                   'size': pkg.stat().st_size, 'digest': digest}]
        if signature is not None:
            assets.append({'name': signature.name,
                           'browser_download_url': signature.as_uri(),
                           'size': signature.stat().st_size})
        rel = {'tag_name': f'v{version}', 'assets': assets}
        mod.http_json = lambda url, timeout=20, use_mirror=True: rel  # type: ignore[assignment]

    def _run(self, mod, pkg: Path, digest: str, *, signature: Path | None,
             version: str = '9.9.9') -> tuple[bool, _Rep, list[list[str]]]:
        """驱动真实 update_manager；`run()` 被接管，用来记录 pip 调用。"""
        calls: list[list[str]] = []

        def fake_run(cmd, *a, **kw):
            calls.append(list(cmd))
            return 0, ''

        mod.run = fake_run  # type: ignore[assignment]
        self._wire_release(mod, pkg, digest, signature=signature, version=version)
        rep = _Rep()
        try:
            mod.update_manager(rep)
            return True, rep, calls
        except Exception as exc:  # noqa: BLE001
            rep.log(f'更新失败：{exc}', 'error')
            return False, rep, calls

    def _assert_untouched(self, install: Path, rep: _Rep) -> None:
        self.assertIn('# OLD', (install / 'server' / 'main.py').read_text(encoding='utf-8'),
                      f'未验签的发布包改动了 server/：\n{rep.text()}')
        self.assertIn('OLD', (install / 'web' / 'out' / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual((install / '.version').read_text(encoding='utf-8').strip(), 'v1.0.0')
        self.assertFalse(list(install.glob('backup-*')), '中止前不该产生备份目录')

    # ── 发布源 ──

    def test_updater_defaults_to_first_party_repo(self) -> None:
        """默认发布源必须是本项目仓库，而且请求确实发到它那里。

        更新器以装机权限执行包内依赖清单，发布源本身就是信任边界：默认指向第三方
        （`ithtelab/workbuddy-manager`）等于把「装什么」的决定权交给别人的仓库。
        """
        install = self._make_install('repo')
        mod = self._load(install)

        self.assertEqual(mod.MANAGER_REPO, OFFICIAL_REPO,
                         '更新器默认发布源不是本项目仓库')
        self.assertEqual(mod.MANAGER_REPO, product.REPOSITORY,
                         '更新器与服务端的仓库事实不一致（server/product.py）')

        urls: list[str] = []
        release = {'tag_name': 'v1.0.1', 'assets': [
            {'name': 'workbuddy-manager-v1.0.1.tar.gz', 'size': 1024,
             'browser_download_url': 'https://example.invalid/workbuddy-manager-v1.0.1.tar.gz'},
            {'name': 'workbuddy-manager-v1.0.1.tar.gz.sig', 'size': 64,
             'browser_download_url': 'https://example.invalid/workbuddy-manager-v1.0.1.tar.gz.sig'},
        ]}
        mod.http_json = lambda url, timeout=20, use_mirror=True: (  # type: ignore[assignment]
            urls.append(url), release)[1]
        info = mod._fetch_release_info(_Rep())

        self.assertTrue(urls, '压根没去查发布')
        self.assertIn(OFFICIAL_REPO, urls[0], f'发布源不是本项目仓库：{urls[0]}')
        self.assertTrue(info['sig_url'].endswith('.tar.gz.sig'),
                        '签名资产没被带上 —— 没有它发布包无法通过验签')

    def test_third_party_repo_override_is_refused(self) -> None:
        """环境变量不能静默把发布源指向第三方：拒绝更新，且不发出任何请求。"""
        install = self._make_install('thirdparty')
        mod = self._load(install, WB_MANAGER_REPO=THIRD_PARTY_REPO)

        self.assertEqual(mod.MANAGER_REPO, OFFICIAL_REPO,
                         'WB_MANAGER_REPO 把发布源静默改成了第三方仓库')

        requests: list[str] = []
        mod.http_json = lambda url, timeout=20, use_mirror=True: (  # type: ignore[assignment]
            requests.append(url), {})[1]
        with mock.patch.object(sys, 'argv', ['update.py', '--target', 'manager']):
            code = mod.main()

        self.assertEqual(code, 1, '指向第三方仓库的更新器竟然继续跑了')
        self.assertFalse(requests, f'拒绝之前还是发出了请求：{requests}')

    # ── 签名 ──

    def test_update_refuses_release_without_verified_signature(self) -> None:
        """没有签名的发布包必须被拒绝，且不能走到 pip。

        这是「发布源被接管」唯一能挡住的一层：摘要印在发布页上，与包来自同一处。
        """
        install = self._make_install('nosig')
        pkg, digest = self._make_release('nosig')
        mod = self._load(install)

        ok, rep, calls = self._run(mod, pkg, digest, signature=None)

        self.assertFalse(ok, '没有签名的发布包竟然装上了')
        self.assertIn('签名', rep.text(), f'拒绝原因没说清是签名：\n{rep.text()}')
        self.assertEqual(calls, [], f'未验签的包已经走到了 pip（装机权限）：{calls}')
        self._assert_untouched(install, rep)

    def test_update_refuses_signature_from_another_key(self) -> None:
        """签名必须来自信任锚对应的私钥：别人的密钥签的包一样拒绝。"""
        install = self._make_install('rogue')
        pkg, digest = self._make_release('rogue')
        signature = self._sign(pkg, key=self.rogue_key)   # 攻击者自己的密钥
        mod = self._load(install)

        ok, rep, calls = self._run(mod, pkg, digest, signature=signature)

        self.assertFalse(ok, '别人签的包竟然装上了')
        self.assertIn('签名校验失败', rep.text(), rep.text())
        self.assertEqual(calls, [], f'验签失败的包走到了 pip：{calls}')
        self._assert_untouched(install, rep)

    def test_update_refuses_when_release_page_rewrites_the_digest(self) -> None:
        """摘要被一起改掉（发布源被接管）时，只有签名还能挡住。

        造法：签的是原包，然后把包换掉，并把发布页的摘要**同步改成被篡改字节的
        摘要** —— 这正是「只比摘要」失效的场景。
        """
        install = self._make_install('rewrite')
        pkg, _ = self._make_release('rewrite')
        signature = self._sign(pkg)
        data = bytearray(pkg.read_bytes())
        data[len(data) // 2] ^= 0xFF
        pkg.write_bytes(bytes(data))
        forged_digest = 'sha256:' + hashlib.sha256(pkg.read_bytes()).hexdigest()
        mod = self._load(install)

        ok, rep, calls = self._run(mod, pkg, forged_digest, signature=signature)

        self.assertFalse(ok, '摘要与签名对不上的包竟然装上了')
        self.assertIn('签名校验失败', rep.text(), rep.text())
        self.assertEqual(calls, [], f'验签失败的包走到了 pip：{calls}')
        self._assert_untouched(install, rep)

    def test_update_installs_release_with_verified_signature(self) -> None:
        """正常路径不能被修坏：签名验过的包必须真的装上去。

        没有这条，「一律拒绝」也能让上面三条通过。
        """
        install = self._make_install('signed')
        pkg, digest = self._make_release('signed')
        signature = self._sign(pkg)
        mod = self._load(install)

        ok, rep, calls = self._run(mod, pkg, digest, signature=signature)

        self.assertTrue(ok, rep.text())
        main_py = (install / 'server' / 'main.py').read_text(encoding='utf-8')
        self.assertIn("version='9.9.9'", main_py)
        self.assertIn('# NEW', main_py)
        self.assertEqual((install / '.version').read_text(encoding='utf-8').strip(), 'v9.9.9')
        self.assertTrue(any('pip' in call for call in calls), '依赖安装根本没跑')

    def test_update_refuses_untrusted_anchor(self) -> None:
        """信任锚本身不合格（可被组/其他用户写）时拒绝 —— 否则换把公钥就绕过了签名。"""
        install = self._make_install('anchor')
        pkg, digest = self._make_release('anchor')
        signature = self._sign(pkg)
        anchor = self.dir / 'trust' / 'release.pub.pem'
        anchor.parent.mkdir()
        anchor.write_bytes(self.release_anchor.read_bytes())
        anchor.chmod(0o666)
        mod = self._load(install, WB_RELEASE_TRUST=str(anchor))

        ok, rep, calls = self._run(mod, pkg, digest, signature=signature)

        self.assertFalse(ok, '可被任何用户改写的信任锚竟然被采信')
        self.assertIn('信任锚', rep.text(), rep.text())
        self.assertEqual(calls, [], f'不信任的锚却走到了 pip：{calls}')
        self._assert_untouched(install, rep)

    def test_update_refuses_missing_anchor(self) -> None:
        """没有信任锚就没有验签能力：fail closed，不回退到"只比摘要"。"""
        install = self._make_install('noanchor')
        pkg, digest = self._make_release('noanchor')
        signature = self._sign(pkg)
        mod = self._load(install, WB_RELEASE_TRUST=str(self.dir / 'nope.pem'))

        ok, rep, calls = self._run(mod, pkg, digest, signature=signature)

        self.assertFalse(ok, '没有信任锚竟然还是装上了（等于只比摘要）')
        self.assertIn('信任锚', rep.text(), rep.text())
        self.assertEqual(calls, [])
        self._assert_untouched(install, rep)

    # ── 受控安装树：marker 在就必须继续拒绝（防退化） ──

    def test_update_refuses_without_managed_marker(self) -> None:
        """marker 存在 = 受控发布树，自助升级必须拒绝。

        这条在修复前就是绿的（记 `_require_verified_deployment_controller`），
        留在这里只是防退化：修复签名与发布源的时候不能把这道屏障弄丢。
        """
        install = self._make_install('managed')
        (install / 'deploy' / 'managed-release.json').write_text('{}', encoding='utf-8')
        pkg, digest = self._make_release('managed')
        signature = self._sign(pkg)
        mod = self._load(install)

        ok, rep, calls = self._run(mod, pkg, digest, signature=signature)

        self.assertFalse(ok, '受控安装树竟然允许自助升级')
        self.assertIn('verified deployment controller', rep.text(), rep.text())
        self.assertEqual(calls, [])
        self._assert_untouched(install, rep)



if __name__ == '__main__':
    unittest.main()
