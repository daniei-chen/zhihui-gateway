"""Explicit native Windows gate; Linux Manager gate checks the source contract."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile


def main():
    if sys.platform != 'win32':
        raise SystemExit('This gate requires native Windows, not a skipped Linux result.')
    root = Path(__file__).resolve().parents[1] / 'manager'
    scripts = ('windows/update.ps1', 'windows/prepare.ps1', 'windows/start.ps1', 'windows/launcher/build.ps1')
    with tempfile.TemporaryDirectory(prefix='zhihui-retired-windows-') as name:
        directory = Path(name)
        sentinel = directory / 'sentinel'
        sentinel.write_bytes(b'unchanged')
        for entry in scripts:
            run = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(root / entry)], cwd=directory, capture_output=True, timeout=15)
            if (run.returncode != 2 or sentinel.read_bytes() != b'unchanged'
                    or {p.name for p in directory.iterdir()} != {'sentinel'}):
                raise AssertionError('retired entry changed files or failed to refuse: ' + entry)
    print(json.dumps({'os': sys.platform, 'executed': len(scripts), 'exit_codes': [2] * len(scripts),
                      'synthetic_working_directory_unchanged': True}))


if __name__ == '__main__':
    main()
