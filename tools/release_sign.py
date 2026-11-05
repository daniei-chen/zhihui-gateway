"""Offline Ed25519 signing / verification for release bundles (new trust channel).

This is a **new** signing channel built for this project. It does not reuse,
replace or wrap any existing production trust anchor: a fresh key pair is
generated offline, the public half ships with the bundle, and the private half
never enters the repository, the bundle or a server.

Design (why it is shaped this way):

* The signature is detached and covers the exact bytes of ``BOM.json``. The BOM
  in turn lists ``sha256`` + ``bytes`` + ``mode`` for every runtime file, so
  signing one file transitively protects the whole tree without a second
  manifest format to keep in sync.
* Verification never needs the private key, and it re-hashes every file, so a
  signature that is valid but whose payload was edited still fails.
* File mode is part of the BOM: a bundle restored with 0777 is a different
  claim and fails verification. Links and unlisted runtime files are refused.
* ``mode=0644`` / ``0755`` are the only accepted values, which keeps the
  manifest small and the restore rule unambiguous.
* Verification requires an independently provisioned public key outside the
  bundle. A public key supplied by the bundle cannot establish publisher trust.
  This experimental channel does not approve a production deployment.

Usage::

    python -B -X utf8 tools/release_sign.py keygen --out-dir <keys-dir> \
        --passphrase-env RELEASE_KEY_PASSPHRASE

    python -B -X utf8 tools/release_sign.py bundle --source <tree> \
        --out <release-dir> --key <private.pem> --passphrase-env RELEASE_KEY_PASSPHRASE \
        --label <name> --source-archive <name> --source-archive-sha256 <sha256>

    python -B -X utf8 tools/release_sign.py verify --release-dir <release-dir> \
        --trusted-key <independently-provisioned-public.pem>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCHEMA_BOM = 'zhihui-release-bom/2'
SCHEMA_SIG = 'zhihui-detached-signature/1'
ALLOWED_MODES = {0o644: '0644', 0o755: '0755'}
SKIP_NAMES = {'.git', '__pycache__', '.pytest_cache', '.DS_Store'}
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 100000


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _require_crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit('cryptography is required for signing: %s' % exc) from None
    return hashes, serialization, ed25519


def _passphrase(args) -> bytes:
    env_name = getattr(args, 'passphrase_env', None)
    if not env_name:
        raise SystemExit('--passphrase-env is required (never pass secrets on the command line)')
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit('environment variable %s is empty or unset' % env_name)
    return value.encode('utf-8')


def _mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _plain_path(value, description: str) -> Path:
    """Do not resolve away a link before checking the supplied path."""
    path = Path(value).absolute()
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SystemExit('%s contains a symbolic link or reparse point' % description)
    # Normalize lexical parent components only after inspecting the original
    # ancestors, so ``elsewhere/../release/trust/key.pem`` cannot evade the
    # requirement that the trust anchor lives outside the release tree.
    return Path(os.path.abspath(path))


def _relative_path(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise SystemExit('invalid BOM file path')
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or '..' in path.parts
            or '\\' in value or ':' in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or any(part.rstrip(' .') != part for part in path.parts)):
        raise SystemExit('unsafe or noncanonical BOM file path')
    return value


def _regular(path: Path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or info.st_nlink != 1):
        raise SystemExit('release files must be regular files without links: %s' % path)
    return info


def _tree_files(root: Path, *, skip_source_metadata: bool = False) -> list[Path]:
    root = _plain_path(root, 'source tree')
    if not root.is_dir():
        raise SystemExit('runtime tree is missing: %s' % root)
    files = []
    seen = set()
    for current, dirs, names in os.walk(root, followlinks=False):
        if skip_source_metadata:
            dirs[:] = [name for name in dirs if name not in SKIP_NAMES]
            names = [name for name in names if name not in SKIP_NAMES]
        dirs.sort()
        for name in sorted(dirs + names):
            path = Path(current) / name
            relative = _relative_path(path.relative_to(root).as_posix())
            if relative.casefold() in seen:
                raise SystemExit('case-colliding runtime paths: %s' % relative)
            seen.add(relative.casefold())
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise SystemExit('runtime tree contains a symbolic link or reparse point')
            if stat.S_ISDIR(info.st_mode):
                continue
            _regular(path)
            files.append(path)
            if len(files) > MAX_FILES:
                raise SystemExit('runtime tree has too many files')
    return files


def _read_regular(path: Path, limit: int) -> bytes:
    if _regular(path).st_size > limit:
        raise SystemExit('release metadata exceeds size limit: %s' % path.name)
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise SystemExit('release metadata exceeds size limit: %s' % path.name)
    return raw


def _json_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise SystemExit('invalid release JSON: %s' % type(exc).__name__) from None
    if not isinstance(value, dict):
        raise SystemExit('release metadata must be an object')
    return value


def build_bom(source: Path) -> dict:
    source = _plain_path(source, 'staged source')
    files = []
    for full in _tree_files(source):
        info = _regular(full)
        mode = stat.S_IMODE(info.st_mode)
        if mode not in ALLOWED_MODES:
            raise SystemExit('staged runtime mode is not 0644/0755: %s' % full)
        files.append({
            'path': full.relative_to(source).as_posix(),
            'bytes': info.st_size,
            'sha256': sha256_file(full),
            'mode': ALLOWED_MODES[mode],
        })
    if not files:
        raise SystemExit('cannot sign an empty runtime tree')
    files.sort(key=lambda item: item['path'])
    return {'schema': SCHEMA_BOM, 'generated_at': _now(),
            'file_count': len(files), 'files': files}


def cmd_keygen(args) -> int:
    _, serialization, ed25519 = _require_crypto()
    out = _plain_path(args.out_dir, 'key directory')
    out.mkdir(parents=True, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    key_id = hashlib.sha256(pub_bytes).hexdigest()[:16]
    priv_path = out / ('release-priv-%s.pem' % key_id)
    pub_path = out / ('release-pub-%s.pem' % key_id)
    if priv_path.exists() or pub_path.exists():
        raise SystemExit('refusing to overwrite an existing key pair: %s' % key_id)
    passphrase = _passphrase(args)
    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase))
    # Create the private key with restrictive permissions from the first byte.
    for path, content, mode in ((priv_path, priv_bytes, 0o600), (pub_path, pub_bytes, 0o644)):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    print(json.dumps({'key_id': key_id, 'private': str(priv_path), 'public': str(pub_path),
                      'note': 'keep the private key offline; it must never enter the repo or a server'},
                     ensure_ascii=False))
    return 0


def cmd_bundle(args) -> int:
    _, serialization, ed25519 = _require_crypto()
    source = _plain_path(args.source, 'source')
    release = _plain_path(args.out, 'release destination')
    key_path = _plain_path(args.key, 'signing key')
    if not source.is_dir():
        raise SystemExit('source tree not found: %s' % source)
    if source == release or source.is_relative_to(release) or release.is_relative_to(source):
        raise SystemExit('source and release destination must not overlap')
    if key_path.is_relative_to(source) or key_path.is_relative_to(release):
        raise SystemExit('the private signing key must be outside source and release trees')
    private_key = serialization.load_pem_private_key(
        _read_regular(key_path, 64 * 1024), password=_passphrase(args))
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise SystemExit('the signing key must be Ed25519')
    _tree_files(source, skip_source_metadata=True)
    if release.exists() and any(release.iterdir()):
        raise SystemExit('release directory is not empty: %s' % release)
    release.mkdir(parents=True, exist_ok=True)
    files_dir = release / 'files'
    if files_dir.exists():
        shutil.rmtree(files_dir)
    # Preserve links while copying so a concurrent added link is refused below,
    # rather than following it and copying files outside the source tree.
    shutil.copytree(source, files_dir, symlinks=True,
                    ignore=shutil.ignore_patterns(*SKIP_NAMES))
    for path in _tree_files(files_dir):
        mode = _mode_of(path)
        if mode not in (0o444, 0o644, 0o664, 0o555, 0o755, 0o775):
            raise SystemExit('unsafe source file mode: %s' % path)
        path.chmod(0o755 if mode & 0o111 else 0o644)
    bom = build_bom(files_dir)
    bom['label'] = args.label
    bom['source_archive'] = args.source_archive
    bom['source_archive_sha256'] = args.source_archive_sha256
    bom['signing'] = {'channel': 'new-offline-ed25519', 'reuses_existing_trust_anchor': False}
    bom_path = release / 'BOM.json'
    bom_bytes = json.dumps(bom, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    bom_path.write_bytes(bom_bytes)

    signature = private_key.sign(bom_bytes)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    key_id = hashlib.sha256(public_pem).hexdigest()[:16]
    trust_dir = release / 'trust'
    trust_dir.mkdir(exist_ok=True)
    (trust_dir / ('release-pub-%s.pem' % key_id)).write_bytes(public_pem)
    sig_doc = {
        'schema': SCHEMA_SIG,
        'algorithm': 'ed25519',
        'key_id': key_id,
        'signed_file': 'BOM.json',
        'signed_sha256': hashlib.sha256(bom_bytes).hexdigest(),
        'signature_hex': signature.hex(),
        'signed_at': _now(),
        'signer': args.signer or '',
    }
    (release / 'BOM.json.sig.json').write_text(
        json.dumps(sig_doc, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
    (release / 'README-INSTALL.md').write_text(
        '# 安装前必须验签\n\n'
        '1. 从独立可信渠道取得包外 Ed25519 公钥；包内 `trust/` 仅作对照，不能建立信任；\n'
        '2. 按 BOM 逐文件校验 sha256、字节数与 mode；\n'
        '3. 任一项不符立即停止；本实验验签不替代生产受信发布器的批准。\n\n'
        '```\npython -B -X utf8 tools/release_sign.py verify --release-dir . '
        '--trusted-key /independently-provisioned/release-public.pem\n```\n',
        encoding='utf-8')
    print(json.dumps({'release': str(release), 'files': bom['file_count'], 'key_id': key_id,
                      'bom_sha256': sig_doc['signed_sha256']}, ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    _, serialization, ed25519 = _require_crypto()
    release = _plain_path(args.release_dir, 'release directory')
    trusted = getattr(args, 'trusted_key', None)
    if not trusted:
        raise SystemExit('--trusted-key is required; a bundled public key cannot establish publisher trust')
    trusted_path = _plain_path(trusted, 'trusted key')
    if trusted_path.is_relative_to(release):
        raise SystemExit('the trusted public key must be provisioned outside the release directory')
    trusted_pem = _read_regular(trusted_path, 64 * 1024)
    public_key = serialization.load_pem_public_key(trusted_pem)
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise SystemExit('the trusted public key must be Ed25519')
    bom_path = release / 'BOM.json'
    sig_path = release / 'BOM.json.sig.json'
    if not bom_path.is_file() or not sig_path.is_file():
        raise SystemExit('BOM.json / BOM.json.sig.json missing in %s' % release)
    sig_doc = _json_object(_read_regular(sig_path, 64 * 1024))
    key_id = sig_doc.get('key_id') or ''
    if (sig_doc.get('schema') != SCHEMA_SIG or sig_doc.get('algorithm') != 'ed25519'
            or sig_doc.get('signed_file') != 'BOM.json'
            or not isinstance(key_id, str) or not re.fullmatch('[a-f0-9]{16}', key_id)
            or hashlib.sha256(trusted_pem).hexdigest()[:16] != key_id):
        raise SystemExit('signature metadata does not match the independently trusted publisher')
    pub_path = release / 'trust' / ('release-pub-%s.pem' % key_id)
    _plain_path(pub_path, 'bundled public key')
    if _read_regular(pub_path, 64 * 1024) != trusted_pem:
        raise SystemExit('bundled public key does not match the independently trusted publisher')
    bom_bytes = _read_regular(bom_path, MAX_MANIFEST_BYTES)
    try:
        public_key.verify(bytes.fromhex(sig_doc['signature_hex']), bom_bytes)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit('BOM signature verification FAILED: %s' % type(exc).__name__) from None
    if hashlib.sha256(bom_bytes).hexdigest() != sig_doc.get('signed_sha256'):
        raise SystemExit('BOM bytes changed after signing (sha256 mismatch)')
    bom = _json_object(bom_bytes)
    if bom.get('schema') != SCHEMA_BOM:
        raise SystemExit('unexpected BOM schema: %r' % bom.get('schema'))
    files_dir = release / 'files'
    actual_files = {path.relative_to(files_dir).as_posix(): path for path in _tree_files(files_dir)}
    entries = bom.get('files')
    count = bom.get('file_count')
    if (not isinstance(entries, list) or not entries or len(entries) > MAX_FILES
            or isinstance(count, bool) or not isinstance(count, int) or count != len(entries)):
        raise SystemExit('invalid BOM file count or file list')
    problems = []
    seen = set()
    folded = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit('invalid BOM file record')
        rel = _relative_path(entry.get('path'))
        if rel.casefold() in folded:
            raise SystemExit('duplicate or case-colliding BOM file path')
        folded.add(rel.casefold())
        seen.add(rel)
        size = entry.get('bytes')
        digest = entry.get('sha256')
        if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
                or entry.get('mode') not in ALLOWED_MODES.values()):
            raise SystemExit('invalid BOM file size, digest or mode')
        target = actual_files.get(rel)
        if target is None:
            problems.append('missing: ' + rel)
            continue
        if target.stat().st_size != entry.get('bytes'):
            problems.append('size mismatch: ' + rel)
        if sha256_file(target) != entry.get('sha256'):
            problems.append('sha256 mismatch: ' + rel)
        mode = _mode_of(target)
        if ALLOWED_MODES.get(mode) != entry.get('mode'):
            problems.append('mode mismatch: %s (%04o != %s)' % (rel, mode, entry.get('mode')))
    extra = sorted(set(actual_files) - seen)
    result = {'status': 'FAILED' if (problems or extra) else 'VERIFIED',
              'release_dir': str(release), 'key_id': key_id,
              'files_checked': len(seen), 'file_count_in_bom': bom.get('file_count'),
              'publisher_verified': not (problems or extra), 'production_approval': False,
              'problems': problems[:50], 'unlisted_files': extra[:50]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'VERIFIED' else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    keygen = sub.add_parser('keygen', help='generate a fresh offline Ed25519 key pair')
    keygen.add_argument('--out-dir', required=True)
    keygen.add_argument('--passphrase-env', required=True)
    keygen.set_defaults(func=cmd_keygen)

    bundle = sub.add_parser('bundle', help='copy a tree, build BOM.json and sign it')
    bundle.add_argument('--source', required=True)
    bundle.add_argument('--out', required=True)
    bundle.add_argument('--key', required=True)
    bundle.add_argument('--passphrase-env', required=True)
    bundle.add_argument('--label', required=True)
    bundle.add_argument('--source-archive', default='')
    bundle.add_argument('--source-archive-sha256', default='')
    bundle.add_argument('--signer', default='')
    bundle.set_defaults(func=cmd_bundle)

    verify = sub.add_parser('verify', help='verify signature and every file in a release')
    verify.add_argument('--release-dir', required=True)
    verify.add_argument('--trusted-key', required=True,
                        help='independently provisioned Ed25519 public key outside the release')
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
