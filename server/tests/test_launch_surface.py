"""发行版启动面守门：服务只允许从 `python -m server.serve` 起。

为什么这条测试是 D-16 的主要产出：`serve.py` 的实例租约（同一 `data/` 只允许一个
进程）与 drain 钩子（停机前进入 draining）**只有发行版真的从它启动时才生效**。
启动面一旦为了「性能」被改回 `uvicorn server.main:app --workers 2`，保护就静默失效：
没有异常、没有 5xx，代价是额度按进程放大 N 倍、账号文件的读改写重新变成多写者
交错（issue #210/#187 的丢更新）。所以这里把「启动面」写成会失败的判据，而不是
注释里的一句提醒。

扫描范围分两层：

* **发行面**（`deploy/`、`Dockerfile`、`windows/`）：出现 uvicorn 直连就是红，没有豁免；
* **文档**（`docs/`、`README*`）：给服务下启动命令时同样只允许 `server.serve`；
  例外是本地调试的热重载命令，必须同时带 `--reload`（那种进程接不了流量，
  也不该被当成部署方式）。

判据只看**生效行**：`#` 开头的注释里可以（也应该）写「不要改回 uvicorn」，那不是配置。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 直连 uvicorn 的启动命令（只针对服务本体；relay 是另一个 app，另有单测）。
DIRECT_UVICORN = re.compile(r'uvicorn\s+server\.main:app')
#: `--workers 2` 及以上：`serve.py` 只允许单进程，多进程会把内存态限额按份数放大。
MULTI_WORKER = re.compile(r'--workers[=\s]+(\d+)')
#: 热重载只属于本地调试。
RELOAD = re.compile(r'--reload\b')

RELEASE_DIRS = ('deploy', 'windows')
RELEASE_FILES = ('Dockerfile',)
DOC_SUFFIXES = ('.md',)
DOC_FILES = ('README.md', 'README.en.md')


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None


def _release_surface() -> dict[Path, str]:
    """deploy/ 与 windows/ 下的全部文本文件 + 根目录的 Dockerfile。"""
    found: dict[Path, str] = {}
    for name in RELEASE_DIRS:
        for path in sorted((ROOT / name).rglob('*')):
            if not path.is_file() or '.git' in path.parts:
                continue
            text = _read(path)
            if text is not None:
                found[path] = text
    for name in RELEASE_FILES:
        text = _read(ROOT / name)
        if text is not None:
            found[ROOT / name] = text
    return found


def _docs() -> dict[Path, str]:
    found: dict[Path, str] = {}
    for path in sorted((ROOT / 'docs').rglob('*.md')):
        text = _read(path)
        if text is not None:
            found[path] = text
    for name in DOC_FILES:
        text = _read(ROOT / name)
        if text is not None:
            found[ROOT / name] = text
    return found


def _active_lines(text: str):
    """`#` 开头的行是注释，不构成配置 —— 但同一行行内注释不豁免。"""
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith('#'):
            continue
        yield number, line


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


class LaunchSurfaceTests(unittest.TestCase):
    def test_systemd_unit_starts_the_managed_launcher(self) -> None:
        unit = ROOT / 'deploy' / 'workbuddy-web.service'
        text = _read(unit)
        self.assertIsNotNone(text, f'找不到 {_rel(unit)}')
        exec_lines = [line.strip() for line in text.splitlines()
                      if line.strip().startswith('ExecStart=')]
        self.assertEqual(len(exec_lines), 1, f'{_rel(unit)} 应当只有一条 ExecStart')
        self.assertIn('-m server.serve', exec_lines[0])
        self.assertNotIn('uvicorn', exec_lines[0])
        self.assertIn('--host 0.0.0.0', exec_lines[0])
        self.assertIn('--port 7864', exec_lines[0])

    def test_install_script_keeps_the_managed_launcher_when_rewriting_execstart(self) -> None:
        text = _read(ROOT / 'deploy' / 'install.sh')
        self.assertIsNotNone(text)
        rewrites = [line for line in text.splitlines()
                    if 's#^ExecStart=' in line]
        self.assertTrue(rewrites, 'install.sh 里应当仍有改写 ExecStart 的那一步')
        for line in rewrites:
            self.assertIn('-m server.serve', line, line)
            self.assertNotIn('uvicorn', line, line)

    def test_dockerfile_cmd_starts_the_managed_launcher(self) -> None:
        text = _read(ROOT / 'Dockerfile')
        self.assertIsNotNone(text)
        cmd = [line.strip() for line in text.splitlines()
               if line.strip().startswith('CMD ')]
        self.assertTrue(cmd, 'Dockerfile 没有 CMD')
        final = cmd[-1]
        self.assertIn('server.serve', final)
        self.assertNotIn('uvicorn', final)

    def test_windows_launcher_starts_the_managed_launcher(self) -> None:
        text = _read(ROOT / 'windows' / 'start.ps1')
        self.assertIsNotNone(text)
        self.assertIn('-m server.serve', text)
        self.assertNotIn('uvicorn', '\n'.join(line for _, line in _active_lines(text)))

    def test_no_direct_uvicorn_launcher_in_the_release_surface(self) -> None:
        offenders = []
        for path, text in _release_surface().items():
            for number, line in _active_lines(text):
                if DIRECT_UVICORN.search(line):
                    offenders.append(f'{_rel(path)}:{number}: {line.strip()}')
        self.assertEqual(offenders, [], '发行面不许直连 uvicorn：\n' + '\n'.join(offenders))

    def test_docs_only_serve_through_the_managed_launcher(self) -> None:
        offenders = []
        for path, text in _docs().items():
            for number, line in _active_lines(text):
                if DIRECT_UVICORN.search(line) and not RELOAD.search(line):
                    offenders.append(f'{_rel(path)}:{number}: {line.strip()}')
        self.assertEqual(offenders, [],
                         '文档里给服务下启动命令请用 `python -m server.serve`'
                         '（只有本地热重载才允许 uvicorn 直连，且必须带 --reload）：\n'
                         + '\n'.join(offenders))

    def test_no_multi_worker_configuration(self) -> None:
        offenders = []
        surfaces = {**_release_surface(), **_docs()}
        for path, text in surfaces.items():
            for number, line in _active_lines(text):
                match = MULTI_WORKER.search(line)
                if match and int(match.group(1)) > 1:
                    offenders.append(f'{_rel(path)}:{number}: {line.strip()}')
        self.assertEqual(offenders, [],
                         '`serve.py` 只允许单进程：限额与准入计数在进程内存里，'
                         '多进程会把额度按 N 放大、并让账号读改写重新变成多写者（#210/#187）：\n'
                         + '\n'.join(offenders))

    def test_the_scanner_really_reads_the_release_surface(self) -> None:
        """正控：路径改名/文件搬走时，上面几条不能因为「什么都没扫到」而假绿。"""
        release = _release_surface()
        docs = _docs()
        self.assertGreaterEqual(len(release), 8, f'只扫到 {len(release)} 个发行面文件')
        self.assertIn(ROOT / 'deploy' / 'workbuddy-web.service', release)
        self.assertIn(ROOT / 'deploy' / 'install.sh', release)
        self.assertIn(ROOT / 'Dockerfile', release)
        self.assertIn(ROOT / 'windows' / 'start.ps1', release)
        self.assertGreaterEqual(len(docs), 5, f'只扫到 {len(docs)} 个文档')
        joined = '\n'.join(list(release.values()) + list(docs.values()))
        self.assertIn('server.serve', joined, '扫到的文件里连启动器名字都没有，扫描大概选错了目录')


if __name__ == '__main__':
    unittest.main()
