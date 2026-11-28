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
import re
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


_UPDATE_PS1 = _ROOT / 'windows' / 'update.ps1'
_BUILD_PS1 = _ROOT / 'windows' / 'launcher' / 'build.ps1'
_SOURCE_BEGIN = '# <manager-repo-source begin>'
_SOURCE_END = '# <manager-repo-source end>'
_PIN_HELPER = _ROOT / 'windows' / 'launcher' / 'upstream-pin.ps1'
_UPSTREAM_LOCK = _ROOT / 'dev' / 'upstream-source.lock'
_PACKER = _ROOT / 'dev' / 'pack_upstream_src.py'
_VERIFY_BEGIN = '# <upstream-src-verify begin>'
_VERIFY_END = '# <upstream-src-verify end>'


def _powershell() -> str | None:
    return shutil.which('pwsh') or shutil.which('powershell')


def _run_ps(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """把脚本写成临时 .ps1 再执行。

    不走 stdin：Windows PowerShell 按控制台代码页解码 stdin，中文注释会被拆坏成
    语法错误（实测 ParserError 缺字符串终止符）。BOM 保证按 UTF-8 读。
    """
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'probe.ps1'
        path.write_text(script, encoding='utf-8-sig')
        args = [_powershell() or '', '-NoProfile', '-NonInteractive']
        if os.name == 'nt':
            args += ['-ExecutionPolicy', 'Bypass']
        args += ['-File', str(path)]
        return subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=120,
                              env=dict(os.environ, **(env or {})))


class WindowsUpdaterSourceBoundaryTests(unittest.TestCase):
    """`windows/update.ps1` 的发布源必须和 Linux 更新器同一条边界（SWO-50）。

    DAN-24 只修了 `deploy/update.py`。Windows 一键更新器仍在从第三方仓库取
    版本号（:157/:181/:223）、`asset.digest`（:364）和安装包（:395）：那种口径下
    摘要只能证明「与第三方的元数据一致」，证明不了装的是本仓的包；而且我们的
    发布在 `daniei-chen/zhihui-gateway`，按第三方取版本号根本找不到本仓 release
    —— 于是 Windows 客户端一路装的是上游构建。

    Linux 侧的判据是 `ALLOWED_MANAGER_REPOS` + `_resolve_manager_repo`：环境变量
    只能从白名单里挑，不在白名单**不静默回退**。这里钉住 Windows 的同一件事。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _UPDATE_PS1.read_text(encoding='utf-8-sig')

    def test_official_repository_constant_matches_linux_updater(self) -> None:
        self.assertIn(f"$OfficialManagerRepo = '{OFFICIAL_REPO}'", self.source,
                      'Windows 更新器的官方发布源与 deploy/update.py 不是同一个仓库')
        self.assertEqual(OFFICIAL_REPO, product.REPOSITORY,
                         '更新器与服务端的仓库事实不一致（server/product.py）')

    def test_no_third_party_repository_slug(self) -> None:
        self.assertNotIn(THIRD_PARTY_REPO, self.source,
                         'Windows 更新器仍指向第三方仓库')

    def test_every_github_url_interpolates_the_resolved_repository(self) -> None:
        urls = [line.strip() for line in self.source.splitlines()
                if re.search(r'https://(?:api\.)?github\.com/', line)]
        self.assertTrue(urls, 'windows/update.ps1 里找不到 GitHub 地址，解析口径变了')
        for line in urls:
            with self.subTest(line=line):
                self.assertIn('$ManagerRepo', line,
                              '发布源地址写死了仓库名；应统一用 Resolve-ManagerRepo 的结果')

    def test_override_is_resolved_before_any_network_call(self) -> None:
        """白名单校验必须排在取版本号之前。

        顺序反了的话，非白名单的源会**先被用来发请求**，等拒绝时已经连出去了
        —— 「没落盘、没执行」就只剩安装阶段那一半。
        """
        resolve = self.source.find('$ManagerRepo = Resolve-ManagerRepo')
        fetch = self.source.find('$tagInfo = Get-LatestGitHubTag')
        self.assertNotEqual(resolve, -1, '找不到 Resolve-ManagerRepo 的调用点')
        self.assertNotEqual(fetch, -1, '找不到 Get-LatestGitHubTag 的调用点')
        self.assertLess(resolve, fetch,
                        '白名单校验排在取版本号之后：拒绝之前已经按非白名单的源发过请求')

    # ── 行为：白名单校验与非白名单拒绝（需要本机有 PowerShell）──

    def _source_block(self) -> str:
        begin = self.source.find(_SOURCE_BEGIN)
        end = self.source.find(_SOURCE_END)
        self.assertNotEqual(begin, -1, f'找不到 {_SOURCE_BEGIN} 标记')
        self.assertNotEqual(end, -1, f'找不到 {_SOURCE_END} 标记')
        return self.source[begin:end]

    def _resolve(self, requested: str) -> subprocess.CompletedProcess:
        if not _powershell():
            self.skipTest('没有 PowerShell，跳过行为用例（文本用例仍然生效）')
        return _run_ps(
            self._source_block() + '\nWrite-Output "RESOLVED=$ManagerRepo"\n',
            {'WB_MANAGER_REPO': requested})

    def test_unset_override_resolves_to_official_repository(self) -> None:
        done = self._resolve('')
        self.assertIn(f'RESOLVED={OFFICIAL_REPO}', done.stdout, done.stderr)

    def test_allowlisted_override_is_accepted(self) -> None:
        done = self._resolve(OFFICIAL_REPO)
        self.assertIn(f'RESOLVED={OFFICIAL_REPO}', done.stdout, done.stderr)

    def test_third_party_override_aborts_instead_of_falling_back(self) -> None:
        """非白名单不能静默换成官方仓库：静默回退会让运维以为自己配的镜像在生效。"""
        done = self._resolve(THIRD_PARTY_REPO)
        self.assertNotEqual(done.returncode, 0, '第三方发布源竟然没让更新中止')
        self.assertNotIn('RESOLVED=', done.stdout, '中止前就把仓库定下来了')


class UpstreamSourcePinTests(unittest.TestCase):
    """随包发布的 `wb2api.exe` 由哪份源码编译，只能有**一处**答案（SWO-87）。

    `windows/launcher/build.ps1` 从固定载体下载 `workbuddy2api-src.tar.gz`，解包后
    用 Go 编译成 `wb2api.exe` 打进安装包。`upstreamMeta` 只记录**编译产物**的
    `artifact_sha256` —— 那证明的是「我们编译出了什么」，**证明不了源码来自哪里**。

    2026-11-28 的 v1.6.28 发布就是败在「同一个东西两处 pin」：`release.yml` 读
    `dev/upstream-source.lock`，`build.ps1` 却自己写死了一份 sha256、而且从第三方
    仓库取源。那边同名文件被换成另一份字节之后，Linux 侧跟着 lock 走（对），
    Windows 安装器守着自己的旧值把**正确的**包也拒了，`installer` job 直接红。

    所以这里钉两件事：取源只有本仓一处、pin 只有 lock 一处，且校验在解包之前；
    再加一条「打包与改 lock 在同一次运行里完成」。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _BUILD_PS1.read_text(encoding='utf-8-sig')
        cls.packer = _PACKER.read_text(encoding='utf-8')

    # ── 文本判据：取源与 pin 各只有一处 ────────────────────────────────

    def test_source_url_is_this_repository(self) -> None:
        found = re.search(r"\$UpstreamSrcUrl = '([^']+)'", self.source)
        self.assertIsNotNone(found, '找不到 $UpstreamSrcUrl')
        url = found.group(1)
        self.assertIn(f'github.com/{OFFICIAL_REPO}/releases/download/', url,
                      f'安装器仍从别处取上游源码：{url}')
        self.assertNotIn(THIRD_PARTY_REPO, self.source,
                         'build.ps1 仍指向第三方仓库（SWO-87 的根因就是它）')

    def test_build_ps1_has_no_second_pin(self) -> None:
        """pin 只能有 lock 一处：第二份常量就是下次发版红的种子。"""
        self.assertIsNone(re.search(r'\$UpstreamSrcSha256\s*=', self.source),
                          'build.ps1 又写死了一份摘要：唯一真源必须是 dev/upstream-source.lock')
        self.assertIsNone(re.search(r"\$[A-Za-z]*Sha256\s*=\s*'[0-9a-fA-F]{64}'", self.source),
                          'build.ps1 里有硬编码的 64 位摘要常量')
        self.assertIn('dev/upstream-source.lock', self.source,
                      'build.ps1 没有引用锁文件')
        self.assertIn('Read-UpstreamSrcPin', self.source,
                      'build.ps1 没有从 lock 读 pin')
        self.assertIn('Assert-UpstreamSrcArchive', self.source,
                      'build.ps1 没有做摘要+字节双校验')

    def test_lock_is_the_single_source_and_carries_both_checks(self) -> None:
        values = {}
        for line in _UPSTREAM_LOCK.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
        self.assertEqual(values.get('release'), 'upstream-src')
        self.assertEqual(values.get('asset'), 'workbuddy2api-src.tar.gz')
        self.assertRegex(values.get('sha256', ''), r'^[0-9a-f]{64}$')
        self.assertRegex(values.get('bytes', ''), r'^[1-9][0-9]*$')

    def test_digest_is_verified_before_extraction(self) -> None:
        verify = self.source.find(_VERIFY_BEGIN)
        extract = self.source.find('& $TarExe -xzf $tarPath')
        self.assertNotEqual(verify, -1, '找不到摘要校验段')
        self.assertNotEqual(extract, -1, '找不到解包调用（解析口径变了）')
        self.assertLess(verify, extract,
                        '摘要在解包之后才校验：那时来源不明的源码已经落地了')

    # ── 行为判据：真跑 windows/launcher/upstream-pin.ps1 ──────────────

    def _probe(self, payload: bytes, lock_text: str | None,
               pin: str | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as temp:
            tmp = Path(temp)
            archive = tmp / 'src.tar.gz'
            archive.write_bytes(payload)
            lock = tmp / 'upstream-source.lock'
            if lock_text is not None:
                lock.write_text(lock_text, encoding='utf-8')
            # stderr 走 [Console]::OutputEncoding：Windows PowerShell 5.1 默认按
            # 控制台代码页（这里是 GBK）编码，中文拒绝信息会变成乱码，用例就没法
            # 断言「被哪条判据拒绝」。这两行把它按 UTF-8 输出。
            script = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
                "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
                "$ErrorActionPreference = 'Stop'\n"
                f". '{_PIN_HELPER.as_posix()}'\n"
                f"$pin = Read-UpstreamSrcPin -LockPath '{lock.as_posix()}'\n"
                f"Assert-UpstreamSrcArchive -Path '{archive.as_posix()}' -Pin $pin\n"
                "Write-Output 'VERIFIED'\n"
            )
            return _run_ps(script)

    @staticmethod
    def _lock_text(digest: str, size: int) -> str:
        return ('release=upstream-src\n'
                'asset=workbuddy2api-src.tar.gz\n'
                f'sha256={digest}\n'
                f'bytes={size}\n')

    def test_mismatched_digest_aborts_before_unpacking(self) -> None:
        if not _powershell():
            self.skipTest('没有 PowerShell，跳过行为用例（文本用例仍然生效）')
        payload = b'not the pinned upstream tarball\n'
        expected = 'f' * 64
        done = self._probe(payload, self._lock_text(expected, len(payload)))
        self.assertNotEqual(done.returncode, 0,
                            f'摘要不匹配竟然继续往下走：\n{done.stdout}{done.stderr}')
        self.assertNotIn('VERIFIED', done.stdout, '拒绝之前就放行了')
        self.assertIn(expected, done.stderr, '拒绝信息里没有期望值，排查时看不出该怎么修')
        self.assertIn(hashlib.sha256(payload).hexdigest(), done.stderr,
                      '拒绝信息里没有实际值')

    def test_mismatched_size_aborts_before_unpacking(self) -> None:
        """字节数是第二条判据：只有摘要一条时，换包后的「摘要巧合」无从证伪。"""
        if not _powershell():
            self.skipTest('没有 PowerShell，跳过行为用例（文本用例仍然生效）')
        payload = b'pinned upstream tarball\n'
        digest = hashlib.sha256(payload).hexdigest()
        done = self._probe(payload, self._lock_text(digest, len(payload) + 1))
        self.assertNotEqual(done.returncode, 0,
                            f'字节数不匹配竟然继续往下走：\n{done.stdout}{done.stderr}')
        self.assertNotIn('VERIFIED', done.stdout, '拒绝之前就放行了')
        self.assertIn('大小不匹配', done.stderr + done.stdout,
                      '拒绝原因不是字节数判据（没抓住要说清是被哪条抓住的）')

    def test_missing_lock_aborts_instead_of_skipping_the_check(self) -> None:
        """没有 lock 时不许「跳过校验继续建」——那等于把安装包交给未验证的源码。"""
        if not _powershell():
            self.skipTest('没有 PowerShell，跳过行为用例（文本用例仍然生效）')
        payload = b'pinned upstream tarball\n'
        done = self._probe(payload, None)
        self.assertNotEqual(done.returncode, 0,
                            f'lock 不存在竟然继续往下走：\n{done.stdout}{done.stderr}')
        self.assertNotIn('VERIFIED', done.stdout, '没有 pin 也放行了')
        self.assertIn('锁文件不存在', done.stderr,
                      '拒绝原因没说清是「没有可校验的 pin」，运维只会看到一句文件找不到')

    def test_matching_pin_lets_the_build_continue(self) -> None:
        """校验不能永远失败：lock 与包对上时必须放行，否则每次都红等于没校验。"""
        if not _powershell():
            self.skipTest('没有 PowerShell，跳过行为用例（文本用例仍然生效）')
        payload = b'pinned upstream tarball\n'
        done = self._probe(payload, self._lock_text(hashlib.sha256(payload).hexdigest(),
                                                    len(payload)))
        self.assertIn('VERIFIED', done.stdout, done.stdout + done.stderr)

    # ── 打包侧：上传与改 lock 必须同一次运行 ──────────────────────────

    def test_packer_writes_the_lock_in_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp = Path(temp)
            src = tmp / 'src'
            src.mkdir()
            (src / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
            (src / 'app.py').write_text('print(1)\n', encoding='utf-8')
            lock = tmp / 'upstream-source.lock'
            done = subprocess.run(
                [sys.executable, '-X', 'utf8', str(_PACKER), str(src),
                 '-o', str(tmp / 'out'), '--lock', str(lock), '--stamp', 'test'],
                capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            asset = tmp / 'out' / 'workbuddy2api-src.tar.gz'
            raw = asset.read_bytes()
            text = lock.read_text(encoding='utf-8')
            self.assertIn(f'sha256={hashlib.sha256(raw).hexdigest()}', text,
                          'lock 的 sha256 与本次产物不一致（或根本没写）')
            self.assertIn(f'bytes={len(raw)}', text,
                          'lock 的 bytes 与本次产物不一致（或根本没写）')

    def test_packer_refuses_a_third_party_upload_target(self) -> None:
        """上传目标只能本仓：把源码快照传到第三方等于交出发布权。"""
        self.assertNotIn(THIRD_PARTY_REPO, self.packer,
                         '打包助手仍教人把我们的源码快照传到第三方仓库')
        self.assertIn(f"DEFAULT_REPO = '{OFFICIAL_REPO}'", self.packer,
                      '打包助手的默认上传目标不是本仓')
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / 'src'
            src.mkdir()
            (src / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
            done = subprocess.run(
                [sys.executable, '-X', 'utf8', str(_PACKER), str(src),
                 '-o', str(Path(temp) / 'out'), '--lock', str(Path(temp) / 'l.txt'),
                 '--upload', '--repo', THIRD_PARTY_REPO],
                capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertNotEqual(done.returncode, 0, '第三方上传目标竟然被接受')


class InstallSurfacesPointAtThisRepositoryTests(unittest.TestCase):
    """安装面（脚本、运维命令、上报入口）不能把人送进别人仓库（SWO-39）。

    `deploy/README.md` 已由 SWO-34 改过。这里钉住剩下那些**会被人照抄执行**的地方：
    校验脚本让人去第三方 Releases 页核对摘要、企业补丁脚本从第三方 registry 拉镜像、
    issue 模板与漏洞上报落到别人仓库、发布流程的 ruleset 命令查的是别人的仓库。

    保留的两类**不动**：上游归属说明（`docs/enterprise-support/README.en.md`、
    `README.md:5` 的出处）与历史记录（`docs/history/*`、`docs/release-process.md:236`
    解释旧默认值）。改了它们等于篡改来源，所以断言都是逐条指向，不做整仓替换。
    """

    #: 从别人仓库取可执行内容/镜像 → 安装面，必须改
    _THIRD_PARTY = 'ithtelab'

    @staticmethod
    def _read(relative: str) -> str:
        return (_ROOT / relative).read_text(encoding='utf-8')

    def test_release_verification_sends_users_to_this_repository(self) -> None:
        script = self._read('deploy/verify-release.sh')
        self.assertNotIn(self._THIRD_PARTY, script,
                         'sha256 核对指引仍指向第三方 Releases（照它核对等于核对别人发的包）')
        self.assertIn('github.com/daniei-chen/zhihui-gateway/releases', script)

    def test_enterprise_support_patch_is_retired(self) -> None:
        """企业补丁包已停用 —— 它下面的命令是**留在仓库里的历史实现**，不执行。

        `apply.sh` 第一行之外先 `exit 2`（被 source 时 `return 2`），README 顶部也标了
        「历史补丁资料，已停用」。所以里面的镜像名要**保持历史原样**：那是「当时用的是
        什么」的记录，改成今天的样子等于篡改历史。这一条是把我 SWO-39 盘点里把它
        归成「活的安装路径」的那个判断纠正回来 —— 实测它是个死路径。
        """
        script = self._read('docs/enterprise-support/apply.sh')
        head = script.split('set -euo pipefail')[0]
        self.assertIn('RETIRED', head, 'apply.sh 的停用闸门不见了')
        self.assertIn('exit 2', head, 'apply.sh 不再在动手之前退出')
        self.assertLess(head.index('exit 2'), script.find('docker run -d'),
                        '停用闸门被挪到 docker 命令之后了')

    def test_enterprise_support_readme_names_the_live_release_source(self) -> None:
        """归属照写，但必须同时说清「本仓是自维护分支、发布源与镜像取本仓」。"""
        text = self._read('docs/enterprise-support/README.md')
        self.assertIn('ithtelab/workbuddy-manager', text,
                      '上游归属被删了 —— 那是来源事实，不能为了干净而抹掉')
        self.assertIn('已停用', text, '停用声明被删了，历史命令会被当成可用指令')
        self.assertIn('ghcr.io/daniei-chen/workbuddy-manager', text,
                      '没说清当前该用哪个镜像')
        self.assertIn('daniei-chen/zhihui-gateway', text,
                      '没说清本仓与上游的关系，读者无法判断装哪个')

    def test_issue_templates_point_at_this_repository(self) -> None:
        text = self._read('.github/ISSUE_TEMPLATE/config.yml')
        self.assertNotIn(self._THIRD_PARTY, text,
                         'issue 模板把用户送到第三方仓库的 README/CHANGELOG')
        self.assertIn('daniei-chen/zhihui-gateway', text)

    def test_release_process_commands_target_this_repository(self) -> None:
        """这两条是**会被人复制执行**的运维命令，指向第三方会改错仓库。"""
        text = self._read('docs/release-process.md')
        self.assertNotIn('repos/ithtelab/', text, 'ruleset 命令查的是别人的仓库')
        self.assertNotIn('owner:"ithtelab"', text, '分支保护查询查的是别人的仓库')
        self.assertIn('repos/daniei-chen/zhihui-gateway/rulesets', text)
        self.assertIn('owner:"daniei-chen", name:"zhihui-gateway"', text)

    def test_no_stale_updater_backup_is_committed(self) -> None:
        """`deploy/update.py.pre-srcpatch-*` 是被提交进仓库的备份，里面还写着旧默认源。"""
        leftovers = sorted(p.name for p in (_ROOT / 'deploy').glob('update.py.pre-*'))
        self.assertEqual(leftovers, [],
                         f'仓库里还躺着更新器备份文件：{leftovers}（它记着旧的第三方默认源）')


if __name__ == '__main__':
    unittest.main()
