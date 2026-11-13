#!/opt/workbuddy-manager/venv/bin/python
"""Read-only production source and permission gate (Ed25519 manifest channel).

Differences from the previous RSA-only gate:
  * The manifest MUST declare "signature_algorithm": "ed25519". The legacy
    rsa-sha256 path is intentionally NOT implemented here, so this gate can
    never be downgraded to the old channel by editing the manifest.
  * Trust anchor: /opt/workbuddy-manager/release-trust-ed25519.pem
    (Ed25519 public key, key_id 2abc6b75f0ecfd5d).
  * Signature: openssl pkeyutl -verify (detached, raw over the manifest bytes).
Everything else (per-file mode whitelist, per-file sha256, executable
entrypoints, compile check over server/*.py) is unchanged from the previous
gate, so the security posture is identical apart from the algorithm.
"""
from pathlib import Path, PurePosixPath
import hashlib
import json
import stat
import subprocess

SAFE_MODES = {0o400, 0o440, 0o444, 0o600, 0o640, 0o644, 0o700, 0o750, 0o755}
ENTRYPOINTS = {'deploy/source_preflight.py', 'server/backup_application.py'}
TRUST = Path('/opt/workbuddy-manager/release-trust-ed25519.pem')
SOURCE_DIRS = ('server', 'deploy')
MANIFEST_FILES = frozenset({'deploy/managed-release.json', 'deploy/managed-release.sig'})


def verify_signature(root: Path, release: dict, marker: Path, signature: Path) -> None:
    if release.get('signature_algorithm') != 'ed25519':
        raise RuntimeError('manifest must declare signature_algorithm=ed25519')
    if not TRUST.is_file() or TRUST.is_symlink():
        raise RuntimeError('ed25519 trust anchor missing: %s' % TRUST)
    if not signature.is_file():
        raise RuntimeError('managed release signature missing')
    checked = subprocess.run(
        ['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(TRUST),
         '-rawin', '-in', str(marker), '-sigfile', str(signature)],
        capture_output=True, timeout=10)
    if checked.returncode:
        raise RuntimeError('managed release signature verification failed (ed25519)')


def source_files(root: Path) -> set[str]:
    """Allow the managed top-level links, never links inside a source tree."""
    resolved_root = root.resolve()
    found = set()
    for name in SOURCE_DIRS:
        base = root / name
        if not base.is_dir() or not base.resolve().is_relative_to(resolved_root):
            raise RuntimeError('unsafe managed source directory: ' + name)
        for file in base.rglob('*'):
            relative = file.relative_to(root).as_posix()
            if file.is_symlink():
                raise RuntimeError('unsafe source symlink: ' + relative)
            if '__pycache__' in file.relative_to(base).parts or file.suffix == '.pyc':
                continue
            if file.is_dir():
                continue
            if not file.is_file() or not file.resolve().is_relative_to(resolved_root):
                raise RuntimeError('unsafe source entry: ' + relative)
            if relative not in MANIFEST_FILES:
                found.add(relative)
    return found


def check(root: Path):
    marker = root / 'deploy/managed-release.json'
    signature = root / 'deploy/managed-release.sig'
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError('managed release manifest missing or unsafe')
    if signature.is_symlink() or not signature.is_file():
        raise RuntimeError('managed release signature missing or unsafe')
    release = json.loads(marker.read_text(encoding='utf-8'))
    if not isinstance(release, dict) or release.get('signature_required') is not True:
        raise RuntimeError('managed release must require a signature')
    verify_signature(root, release, marker, signature)
    files = release.get('files')
    if not isinstance(files, dict) or not files:
        raise RuntimeError('managed release file list missing or empty')
    if MANIFEST_FILES.intersection(files):
        raise RuntimeError('release marker/signature must not hash themselves')
    verified = 0
    for relative, item in files.items():
        parts = PurePosixPath(relative)
        if (parts.is_absolute() or '..' in parts.parts or '\\' in relative
                or parts.as_posix() != relative or not parts.parts):
            raise RuntimeError('unsafe managed release path')
        file = root / relative
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(root.resolve()):
            raise RuntimeError('unsafe managed release file: ' + relative)
        if not isinstance(item, dict):
            raise RuntimeError('invalid managed release item: ' + relative)
        expected = item.get('mode')
        actual = stat.S_IMODE(file.stat().st_mode)
        if isinstance(expected, bool) or expected not in SAFE_MODES or expected != actual:
            raise RuntimeError('managed release permissions mismatch: ' + relative)
        if relative in ENTRYPOINTS and not actual & 0o111:
            raise RuntimeError('managed entrypoint is not executable: ' + relative)
        if hashlib.sha256(file.read_bytes()).hexdigest() != item.get('candidate_sha256'):
            raise RuntimeError('managed source hash mismatch: ' + relative)
        verified += 1
    expected_sources = {name for name in files if name.startswith(('server/', 'deploy/'))}
    actual_sources = source_files(root)
    if actual_sources != expected_sources:
        extra = sorted(actual_sources - expected_sources)
        missing = sorted(expected_sources - actual_sources)
        raise RuntimeError('managed source closure mismatch: extra=%r missing=%r' % (extra, missing))
    for file in (root / 'server').rglob('*.py'):
        if '__pycache__' not in file.parts:
            compile(file.read_text(encoding='utf-8'), str(file), 'exec')
    return verified


if __name__ == '__main__':
    verified = check(Path('/opt/workbuddy-manager'))
    print(f'managed source preflight passed: {verified} files; ed25519 channel; startup patches disabled')
