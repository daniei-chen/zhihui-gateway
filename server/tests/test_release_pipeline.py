"""发布流水线（deploy/release.py）的判定逻辑回归测试。

2026-11-23 连发三版时，每一版都在同样的地方出错，而**每一处错误都是可判定的**：
旧的签名清单被沿用、权限是 664、发布元数据六处不一致、manifest 版本来挂载点
目录名…… 这个测试把那些判定固定下来。

签名部分依赖 `cryptography`（只在发布镜像里），本机没有时按环境跳过 ——
跳过的是「能不能签」，不是「该不该拒」。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location('release_tool', ROOT / 'deploy/release.py')
release_tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_tool)


def _write(path: Path, text: str = 'x') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def make_release(root: Path, version: str, product: str) -> Path:
    """造一个**元数据自洽**的最小发布树。"""
    base = root / version
    base.mkdir(parents=True)
    _write(base / '.version', version + '\n')
    _write(base / 'CHANGELOG.md', f'# 更新日志\n\n## [{product}] - 2026-11-23\n\n### 新增\n\n- 测试项。\n')
    _write(base / 'server/product-release.json', json.dumps(
        {'version': product, 'release_channel': 'github-actions'}, ensure_ascii=False))
    _write(base / 'server/writer-capabilities.json', json.dumps({
        'schema': 'zhihui.writer-release', 'version': 1, 'release_id': version,
        'role_policy': 2, 'key_daily_usage': 1,
        'account_admin_routes': 'account-add-only-default-deny',
    }, ensure_ascii=False))
    _write(base / 'server/release-provenance.json', json.dumps(
        {'version': product, 'release_id': version, 'source_commit': '0' * 40}, ensure_ascii=False))
    _write(base / 'server/entry.py', 'print(1)\n')
    _write(base / 'deploy/preflight-gate.py', 'print(1)\n')
    _write(base / 'web/out/index.html', '<html></html>')
    _write(base / 'web/out/login/index.html', '<html></html>')
    return base


def make_archive(path: Path, version: str, product: str, *, mode: int = 0o664,
                 eol: str = '\n', extra: dict[str, bytes] | None = None) -> Path:
    """造一个 `git archive` 形态的归档（顶层为 server/ 与 deploy/）。

    `eol` 用来复现「在 Windows 工作区打包」：2026-11-26 的 v1.6.0 发布树
    就是这样带着 525 个 CRLF 的 `server/*.py` 上线的（见
    `release_tool.normalize_eol`）。`extra` 放二进制，验证归一不会碰它们。
    """
    files = {
        '.version': version + '\n',
        'CHANGELOG.md': f'# 更新日志\n\n## [{product}] - 2026-11-23\n\n### 新增\n\n- 新版本。\n',
        'VERSION': product + '\n',
        'server/product-release.json': json.dumps({'version': product}, ensure_ascii=False),
        'server/writer-capabilities.json': json.dumps({
            'schema': 'zhihui.writer-release', 'version': 1, 'release_id': version,
            'role_policy': 2, 'key_daily_usage': 1,
            'account_admin_routes': 'account-add-only-default-deny',
        }, ensure_ascii=False),
        'server/release-provenance.json': json.dumps(
            {'version': product, 'release_id': version}, ensure_ascii=False),
        'server/entry.py': 'print(2)\n',
        'deploy/preflight-gate.py': 'print(2)\n',
    }
    with tarfile.open(path, 'w:gz') as archive:
        for name, text in files.items():
            _add_member(archive, name, text.replace('\n', eol).encode(), mode)
        for name, raw in (extra or {}).items():
            _add_member(archive, name, raw, mode)
    return path


def _add_member(archive: tarfile.TarFile, name: str, raw: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    info.mode = mode
    archive.addfile(info, io.BytesIO(raw))


def make_web_archive(path: Path, *, markup: str = '<html>new</html>') -> Path:
    files = {'out/index.html': markup, 'out/login/index.html': markup,
             'out/_next/static/chunks/a.js': 'console.log(1)'}
    with tarfile.open(path, 'w:gz') as archive:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o666
            archive.addfile(info, io.BytesIO(raw))
    return path


class MetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base = make_release(self.root, 'v1.0.0', '1.0.0')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_consistent_release_passes(self) -> None:
        self.assertTrue(release_tool.check_metadata(self.base))

    def test_writer_version_as_product_string_is_rejected(self) -> None:
        """最常见的写错法：把描述格式版本当成产品版本。"""
        path = self.base / 'server/writer-capabilities.json'
        path.write_text(json.dumps({
            'schema': 'zhihui.writer-release', 'version': '1.0.0', 'release_id': 'v1.0.0',
            'role_policy': 2, 'key_daily_usage': 1,
            'account_admin_routes': 'account-add-only-default-deny',
        }), encoding='utf-8')
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_metadata(self.base)
        self.assertIn('必须是整数 1', str(ctx.exception))

    def test_writer_release_id_drift_rejected(self) -> None:
        path = self.base / 'server/writer-capabilities.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['release_id'] = 'v0.9.9'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_metadata(self.base)
        self.assertIn('writer-capabilities.release_id', str(ctx.exception))

    def test_product_version_drift_rejected(self) -> None:
        _write(self.base / 'server/product-release.json', json.dumps({'version': '1.0.1'}))
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_metadata(self.base)
        self.assertIn('product-release.version', str(ctx.exception))

    def test_provenance_drift_rejected(self) -> None:
        _write(self.base / 'server/release-provenance.json',
               json.dumps({'version': '1.0.0', 'release_id': 'v0.0.1'}))
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_metadata(self.base)
        self.assertIn('release-provenance.release_id', str(ctx.exception))

    def test_missing_changelog_section_rejected(self) -> None:
        _write(self.base / 'CHANGELOG.md', '# 更新日志\n\n## [0.9.0]\n\n- 旧。\n')
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_metadata(self.base)
        self.assertIn('没有 ## [1.0.0] 段落', str(ctx.exception))

    def test_bad_directory_name_rejected(self) -> None:
        bad = self.root / 'v1529'
        bad.mkdir()
        with self.assertRaises(release_tool.ReleaseError):
            release_tool.check_metadata(bad)

    def test_all_problems_reported_together(self) -> None:
        """一次全报，而不是撞到一个算一个 —— 否则要改六轮。"""
        _write(self.base / 'server/product-release.json', json.dumps({'version': '9.9.9'}))
        _write(self.base / 'server/release-provenance.json',
               json.dumps({'version': '9.9.9', 'release_id': 'v9.9.9'}))
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_metadata(self.base)
        message = str(ctx.exception)
        self.assertIn('product-release.version', message)
        self.assertIn('release-provenance.version', message)


class ExtractSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tar(self, members) -> Path:
        path = self.root / 'evil.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == 'file':
                    raw = b'x'
                    info.size = 1
                    archive.addfile(info, io.BytesIO(raw))
                elif kind == 'link':
                    info.type = tarfile.SYMTYPE
                    info.linkname = '/etc/passwd'
                    archive.addfile(info)
                else:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
        return path

    def test_parent_traversal_rejected(self) -> None:
        archive = self._tar([('../escape.py', 'file')])
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.extract(archive, self.root / 'out')
        self.assertIn('越界路径', str(ctx.exception))

    def test_leading_slash_is_never_trusted(self) -> None:
        """带前导 `/` 的成员各有各的处理，但**绝不能写到 dest 之外**。

        tarfile 在两套平台上的行为不同：POSIX 保留前导 `/`（我们直接拒绝，
        因为绝对路径没有合法用途），Windows 会把它去掉、变成树内相对路径。
        所以断言写成「拒绝，或落在树内」，两种都安全，唯一不允许的是逃出去。
        """
        archive = self._tar([('/etc/evil.py', 'file')])
        out = self.root / 'out'
        try:
            written = release_tool.extract(archive, out)
        except release_tool.ReleaseError:
            self.assertFalse((self.root / 'etc').exists())
            return
        for name in written:
            self.assertFalse(Path(name).is_absolute(), name)
            self.assertTrue((out / name).is_file(), name)
        self.assertFalse((self.root / 'etc').exists(), '绝对路径逃出了 dest')

    def test_symlink_rejected(self) -> None:
        """符号链接是把发布树指向树外的常见手法，一律不落地。"""
        archive = self._tar([('link.py', 'link')])
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.extract(archive, self.root / 'out')
        self.assertIn('非普通文件', str(ctx.exception))

    def test_roots_filter_drops_unrelated(self) -> None:
        archive = self._tar([('server/a.py', 'file'), ('web/lib/b.ts', 'file')])
        out = self.root / 'out'
        written = release_tool.extract(archive, out, roots=('server', 'deploy'))
        self.assertEqual(written, ['server/a.py'])
        self.assertFalse((out / 'web').exists())


class ClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base = make_release(self.root, 'v1.0.0', '1.0.0')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sealed_files_excluded(self) -> None:
        """自己重写的两个清单不能算进闭包，否则会自我引用。"""
        _write(self.base / 'deploy/managed-release.json', '{}')
        _write(self.base / 'deploy/managed-release.sig', 'x')
        files = release_tool.closure(self.base)
        self.assertNotIn('deploy/managed-release.json', files)
        self.assertNotIn('deploy/managed-release.sig', files)

    def test_pycache_rejected(self) -> None:
        (self.base / 'server/__pycache__').mkdir()
        _write(self.base / 'server/__pycache__/x.pyc', 'x')
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.closure(self.base)
        self.assertIn('__pycache__', str(ctx.exception))

    def test_web_closure_excludes_manifest(self) -> None:
        _write(self.base / 'web/out/manifest.sha256', 'x')
        _write(self.base / 'web/out/manifest.sig', 'x')
        names = {name for name, _, _ in release_tool.web_closure(self.base)}
        self.assertEqual(names, {'index.html', 'login/index.html'})


class StageTest(unittest.TestCase):
    """端到端：从基线复制、覆盖新产物、清掉旧清单、规范化权限。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'releases'
        self.root.mkdir()
        self.base = make_release(self.root, 'v1.0.0', '1.0.0')
        # 基线里放着上一版的清单：stage 必须删掉它们
        _write(self.base / 'deploy/managed-release.json', '{"stale": true}')
        _write(self.base / 'deploy/managed-release.sig', 'stale')
        _write(self.base / 'web/out/manifest.sha256', 'stale')
        _write(self.base / 'web/out/manifest.sig', 'stale')
        _write(self.base / 'web/out/old-chunk.js', 'stale')
        self.archive = make_archive(Path(self._tmp.name) / 'rel.tar.gz', 'v1.0.1', '1.0.1')
        self.web = make_web_archive(Path(self._tmp.name) / 'web.tar.gz')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stage(self):
        args = release_tool.argparse.Namespace(
            root=str(self.root), version='v1.0.1', archive=str(self.archive),
            web=str(self.web), base='v1.0.0', key='k', trust='t')
        return release_tool.cmd_stage(args)

    def test_stage_produces_consistent_tree(self) -> None:
        self.assertEqual(self._stage(), 0)
        target = self.root / 'v1.0.1'
        self.assertTrue((target / 'server/entry.py').is_file())
        self.assertIn('print(2)', (target / 'server/entry.py').read_text(encoding='utf-8'))
        self.assertEqual((target / '.version').read_text(encoding='utf-8').strip(), 'v1.0.1')
        self.assertTrue(release_tool.check_metadata(target))

    def test_stage_removes_stale_seals(self) -> None:
        self._stage()
        target = self.root / 'v1.0.1'
        for name in ('deploy/managed-release.json', 'deploy/managed-release.sig',
                     'web/out/manifest.sha256', 'web/out/manifest.sig'):
            self.assertFalse((target / name).exists(), name)

    def test_stage_replaces_web_tree_entirely(self) -> None:
        """混合两代前端产物会出现「引用了不存在的 chunk」，必须整目录替换。"""
        self._stage()
        self.assertFalse((self.root / 'v1.0.1/web/out/old-chunk.js').exists())
        self.assertTrue((self.root / 'v1.0.1/web/out/_next/static/chunks/a.js').is_file())

    @unittest.skipUnless(os.name == 'posix', 'Windows 上 chmod 不保留 Unix 权限位')
    def test_stage_normalises_modes(self) -> None:
        """归档来源的 664/666 会被启动门禁拒绝（unsafe release member）。"""
        self._stage()
        target = self.root / 'v1.0.1'
        for base in ('server', 'deploy', 'web/out'):
            for path in (target / base).rglob('*'):
                mode = stat.S_IMODE(path.lstat().st_mode)
                self.assertIn(mode, (0o644, 0o755), f'{path} mode={oct(mode)}')

    def test_stage_normalises_crlf_archive(self) -> None:
        """CRLF 归档落地后必须是 LF。

        2026-11-26：v1.6.0 的发布树带着 525 个 CRLF 的 `server/*.py` 和 39 个
        `deploy` 脚本上线。Python 读文件容错，所以网关照常服务、巡检全绿；但
        `server/backup_application.py` 是**内核直接执行**的入口，shebang 末尾多
        一个 `\\r` 就等于解释器不存在 —— 每日恢复快照因此连着失败了两天。
        """
        crlf = make_archive(Path(self._tmp.name) / 'crlf.tar.gz', 'v1.0.2', '1.0.2',
                            eol='\r\n')
        args = release_tool.argparse.Namespace(
            root=str(self.root), version='v1.0.2', archive=str(crlf),
            web=str(self.web), base='v1.0.0', key='k', trust='t')
        self.assertEqual(release_tool.cmd_stage(args), 0)
        target = self.root / 'v1.0.2'
        for relative in ('server/entry.py', 'deploy/preflight-gate.py'):
            data = (target / relative).read_bytes()
            self.assertNotIn(b'\r', data,
                             f'{relative} 还是 CRLF：内核执行会报解释器不存在')

    def test_stage_keeps_binary_bytes(self) -> None:
        """含 NUL 的二进制原样保留：改坏图片比留一处行尾严重得多。"""
        blob = b'\x89PNG\r\n\x1a\n\x00\x00\r\nEND'
        archive = make_archive(Path(self._tmp.name) / 'bin.tar.gz', 'v1.0.3', '1.0.3',
                               extra={'server/logo.png': blob})
        args = release_tool.argparse.Namespace(
            root=str(self.root), version='v1.0.3', archive=str(archive),
            web=str(self.web), base='v1.0.0', key='k', trust='t')
        self.assertEqual(release_tool.cmd_stage(args), 0)
        self.assertEqual((self.root / 'v1.0.3/server/logo.png').read_bytes(), blob)

    def test_stage_keeps_crlf_for_windows_batch_files(self) -> None:
        """`.cmd` 必须保持 CRLF。

        仓库的 `.gitattributes` 明确写着 `*.cmd text eol=crlf`，因为 cmd.exe 对
        LF-only 批处理在 if 块 / goto 标签边界上有已知的解析风险。它们不会被
        Linux 的 execve 执行，行尾归一只有坏处 —— 2026-11-26 第一次 stage
        v1.6.1 时就是这么把两个 `.cmd` 误转成了 LF。
        """
        script = b'@echo off\r\nstart "" workbuddy\r\n'
        archive = make_archive(Path(self._tmp.name) / 'cmd.tar.gz', 'v1.0.4', '1.0.4',
                               extra={'deploy/windows-native/start.cmd': script})
        args = release_tool.argparse.Namespace(
            root=str(self.root), version='v1.0.4', archive=str(archive),
            web=str(self.web), base='v1.0.0', key='k', trust='t')
        self.assertEqual(release_tool.cmd_stage(args), 0)
        landed = (self.root / 'v1.0.4/deploy/windows-native/start.cmd').read_bytes()
        self.assertEqual(landed, script, '.cmd 被归一成 LF 了：cmd.exe 会有解析风险')

    def test_existing_version_refused(self) -> None:
        args = release_tool.argparse.Namespace(
            root=str(self.root), version='v1.0.0', archive=str(self.archive),
            web=self.web, base='v1.0.0', key='k', trust='t')
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.cmd_stage(args)
        self.assertIn('已存在', str(ctx.exception))


@unittest.skipUnless(os.name == 'posix', '发布指针是符号链接，Windows 需开发者模式')
class SwitchTest(unittest.TestCase):
    """指针切换只在 POSIX 上测：Windows 建符号链接要特权，而发布机是 Linux。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_release(self.root, 'v1.0.0', '1.0.0')
        make_release(self.root, 'v1.0.1', '1.0.1')
        (self.root / 'current').symlink_to(self.root / 'v1.0.0', target_is_directory=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_switch_is_atomic_and_remembers_previous(self) -> None:
        previous = release_tool._switch(self.root / 'current', 'v1.0.1')
        self.assertEqual(previous, 'v1.0.0')
        self.assertEqual(os.readlink(self.root / 'current'), 'v1.0.1')
        # 切换不应留下临时链接
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.')], [])

    def test_switch_writes_relative_target(self) -> None:
        """链接目标必须是**相对名**。

        本工具在容器里跑（`--root /rel`），链接在宿主上被解析。写绝对路径会把
        `/rel/v1.5.32` 烙进去 —— 宿主上没有这个路径，服务立刻起不来。真实踩过。
        """
        release_tool._switch(self.root / 'current', 'v1.0.1')
        target = os.readlink(self.root / 'current')
        self.assertEqual(target, 'v1.0.1', f'链接目标是 {target!r}，应为相对名')
        self.assertFalse(Path(target).is_absolute())

    def test_activate_verifies_before_switching(self) -> None:
        """「先跑门禁、再切指针」是这条流程的核心不变量。

        测试里把门禁替换掉（fixture 没有真密钥，签不出合法清单），只断言
        **它被调用过** —— 顺序反了就等于回到「直接 ln -sfn 然后重启」。
        """
        from unittest import mock
        with mock.patch.object(release_tool, 'cmd_verify') as verify:
            release_tool.cmd_activate(release_tool.argparse.Namespace(
                root=str(self.root), version='v1.0.1', trust='t'))
        verify.assert_called_once()
        self.assertEqual(os.readlink(self.root / 'current'), 'v1.0.1')

    def test_rollback_returns_to_previous(self) -> None:
        previous = release_tool._switch(self.root / 'current', 'v1.0.1')
        (self.root / '.previous').write_text((previous or '') + '\n', encoding='utf-8')
        release_tool.cmd_rollback(release_tool.argparse.Namespace(root=str(self.root)))
        self.assertEqual(os.readlink(self.root / 'current'), 'v1.0.0')

    def test_rollback_without_history_refused(self) -> None:
        with self.assertRaises(release_tool.ReleaseError):
            release_tool.cmd_rollback(release_tool.argparse.Namespace(root=str(self.root)))


class NotesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base = make_release(self.root, 'v1.0.0', '1.0.0')
        _write(self.base / 'CHANGELOG.md',
               '# 更新日志\n\n## [1.0.1] - 2026-11-24\n\n### 新增\n\n- 甲的改动。\n\n'
               '### 修复\n\n- 乙的改动。\n\n## [1.0.0] - 2026-11-23\n\n### 新增\n\n- 旧。\n')
        (self.root / 'v1.0.1').mkdir()
        (self.root / 'v1.0.1/CHANGELOG.md').write_text(
            (self.base / 'CHANGELOG.md').read_text(encoding='utf-8'), encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, **kw):
        args = release_tool.argparse.Namespace(
            root=str(self.root), version='v1.0.1', out=None, changelog=None, asset=[])
        for key, value in kw.items():
            setattr(args, key, value)
        return release_tool.cmd_notes(args)

    def test_extracts_only_requested_version(self) -> None:
        out = Path(self._tmp.name) / 'notes.md'
        self.assertEqual(self._run(out=str(out)), 0)
        text = out.read_text(encoding='utf-8')
        self.assertIn('# 智汇网关 v1.0.1', text)
        self.assertIn('甲的改动', text)
        self.assertIn('乙的改动', text)
        self.assertNotIn('旧。', text)

    def test_section_level_mapped(self) -> None:
        out = Path(self._tmp.name) / 'notes.md'
        self._run(out=str(out))
        text = out.read_text(encoding='utf-8')
        self.assertIn('## 新增', text)
        self.assertNotIn('### 新增', text)

    def test_asset_digest_validated(self) -> None:
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            self._run(asset=['a.tar.gz=sha256:nothex'])
        self.assertIn('64 位十六进制', str(ctx.exception))

    def test_asset_line_rendered(self) -> None:
        out = Path(self._tmp.name) / 'notes.md'
        self._run(out=str(out), asset=['a.tar.gz=sha256:' + 'a' * 64])
        text = out.read_text(encoding='utf-8')
        self.assertIn('## 产物校验（sha256）', text)
        self.assertIn('a' * 64 + '  a.tar.gz', text)

    def test_missing_version_refused(self) -> None:
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            self._run(root=str(self.root), version='v9.9.9')
        self.assertIn('CHANGELOG', str(ctx.exception))


class NginxCheckTest(unittest.TestCase):
    """余额协议的反代放行是「被 include 进来」的，恢复备份后会静默消失。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _site(self, text: str) -> Path:
        path = self.root / 'site'
        path.write_text(text, encoding='utf-8')
        return path

    def test_both_locations_accepted(self) -> None:
        site = self._site(
            '    location = /api/usage/token {\n'
            '        include /etc/nginx/snippets/zh-usage-token.location.conf;\n'
            '    }\n'
            '    location = /api/usage/token/ {\n'
            '        include /etc/nginx/snippets/zh-usage-token.location.conf;\n'
            '    }\n')
        self.assertTrue(release_tool.check_nginx(site))

    def test_missing_block_reported_with_names(self) -> None:
        site = self._site('    location /api/ {\n        auth_request /__zh_session;\n    }\n')
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_nginx(site)
        message = str(ctx.exception)
        self.assertIn('/api/usage/token', message)
        self.assertIn('恢复备份', message)

    def test_only_trailing_slash_present_still_fails(self) -> None:
        """两种写法都要有：面板实现有带斜杠、有不带斜杠。"""
        site = self._site('    location = /api/usage/token/ {\n'
                          '        include x;\n    }\n')
        with self.assertRaises(release_tool.ReleaseError) as ctx:
            release_tool.check_nginx(site)
        self.assertIn('/api/usage/token', str(ctx.exception))

    def test_missing_file_refused(self) -> None:
        with self.assertRaises(release_tool.ReleaseError):
            release_tool.check_nginx(self.root / 'nope')


if __name__ == '__main__':
    unittest.main()
