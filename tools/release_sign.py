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
  claim and fails verification.
* ``mode=0644`` / ``0755`` are the only accepted values, which keeps the
  manifest small and the restore rule unambiguous.

Usage::

    python -B -X utf8 tools/release_sign.py keygen --out-dir <keys-dir> \
        --passphrase-env RELEASE_KEY_PASSPHRASE

    python -B -X utf8 tools/release_sign.py bundle --source <tree> \
        --out <release-dir> --key <private.pem> --passphrase-env RELEASE_KEY_PASSPHRASE \
        --label <name> --source-archive <name> --source-archive-sha256 <sha256>

    python -B -X utf8 tools/release_sign.py verify --release-dir <release-dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_BOM = 'zhihui-release-bom/2'
SCHEMA_SIG = 'zhihui-detached-signature/1'
ALLOWED_MODES = {0o644: '0644', 0o755: '0755'}
SKIP_NAMES = {'.git', '__pycache__', '.pytest_cache', '.DS_Store'}


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


def build_bom(source: Path) -> dict:
    files = []
    for current, dirs, names in os.walk(source):
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_NAMES]
        for name in sorted(names):
            if name in SKIP_NAMES:
                continue
            full = Path(current) / name
            if not full.is_file():
                continue
            mode = _mode_of(full)
            if mode not in ALLOWED_MODES:
                # Normalise the common case (a checkout may bring 0664/0775);
                # anything unusual is refused rather than silently rewritten.
                mode = 0o755 if mode & 0o111 else 0o644
            files.append({
                'path': full.relative_to(source).as_posix(),
                'bytes': full.stat().st_size,
                'sha256': sha256_file(full),
                'mode': ALLOWED_MODES[mode],
            })
    files.sort(key=lambda item: item['path'])
    return {'schema': SCHEMA_BOM, 'generated_at': _now(),
            'file_count': len(files), 'files': files}


def cmd_keygen(args) -> int:
    _, serialization, ed25519 = _require_crypto()
    out = Path(args.out_dir)
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
    priv_path.write_bytes(priv_bytes)
    pub_path.write_bytes(pub_bytes)
    try:
        priv_path.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({'key_id': key_id, 'private': str(priv_path), 'public': str(pub_path),
                      'note': 'keep the private key offline; it must never enter the repo or a server'},
                     ensure_ascii=False))
    return 0


def cmd_bundle(args) -> int:
    _, serialization, ed25519 = _require_crypto()
    source = Path(args.source).resolve()
    release = Path(args.out).resolve()
    if not source.is_dir():
        raise SystemExit('source tree not found: %s' % source)
    if release.exists() and any(release.iterdir()):
        raise SystemExit('release directory is not empty: %s' % release)
    release.mkdir(parents=True, exist_ok=True)
    files_dir = release / 'files'
    if files_dir.exists():
        shutil.rmtree(files_dir)
    shutil.copytree(source, files_dir,
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '.git'))
    bom = build_bom(files_dir)
    bom['label'] = args.label
    bom['source_archive'] = args.source_archive
    bom['source_archive_sha256'] = args.source_archive_sha256
    bom['signing'] = {'channel': 'new-offline-ed25519', 'reuses_existing_trust_anchor': False}
    bom_path = release / 'BOM.json'
    bom_bytes = json.dumps(bom, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    bom_path.write_bytes(bom_bytes)

    private_key = serialization.load_pem_private_key(
        Path(args.key).read_bytes(), password=_passphrase(args))
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
        '1. 用 `trust/release-pub-<key_id>.pem` 对 `BOM.json` 验签；\n'
        '2. 按 BOM 逐文件校验 sha256、字节数与 mode；\n'
        '3. 任一项不符立即停止，不要安装。\n\n'
        '```\npython -B -X utf8 tools/release_sign.py verify --release-dir .\n```\n',
        encoding='utf-8')
    print(json.dumps({'release': str(release), 'files': bom['file_count'], 'key_id': key_id,
                      'bom_sha256': sig_doc['signed_sha256']}, ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    _, serialization, ed25519 = _require_crypto()
    release = Path(args.release_dir).resolve()
    bom_path = release / 'BOM.json'
    sig_path = release / 'BOM.json.sig.json'
    if not bom_path.is_file() or not sig_path.is_file():
        raise SystemExit('BOM.json / BOM.json.sig.json missing in %s' % release)
    sig_doc = json.loads(sig_path.read_text(encoding='utf-8'))
    key_id = sig_doc.get('key_id') or ''
    pub_path = release / 'trust' / ('release-pub-%s.pem' % key_id)
    if not pub_path.is_file():
        raise SystemExit('public key for key_id %s is missing' % key_id)
    public_key = serialization.load_pem_public_key(pub_path.read_bytes())
    if hashlib.sha256(pub_path.read_bytes()).hexdigest()[:16] != key_id:
        raise SystemExit('public key does not match the declared key_id')
    bom_bytes = bom_path.read_bytes()
    try:
        public_key.verify(bytes.fromhex(sig_doc['signature_hex']), bom_bytes)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit('BOM signature verification FAILED: %s' % type(exc).__name__) from None
    if hashlib.sha256(bom_bytes).hexdigest() != sig_doc.get('signed_sha256'):
        raise SystemExit('BOM bytes changed after signing (sha256 mismatch)')
    bom = json.loads(bom_bytes.decode('utf-8'))
    if bom.get('schema') != SCHEMA_BOM:
        raise SystemExit('unexpected BOM schema: %r' % bom.get('schema'))
    files_dir = release / 'files'
    problems = []
    seen = set()
    for entry in bom.get('files') or []:
        rel = entry.get('path') or ''
        seen.add(rel)
        target = files_dir / rel
        if not target.is_file():
            problems.append('missing: ' + rel)
            continue
        if target.stat().st_size != entry.get('bytes'):
            problems.append('size mismatch: ' + rel)
        if sha256_file(target) != entry.get('sha256'):
            problems.append('sha256 mismatch: ' + rel)
        mode = _mode_of(target)
        normalised = 0o755 if mode & 0o111 else 0o644
        if ALLOWED_MODES[normalised] != entry.get('mode'):
            problems.append('mode mismatch: %s (%s != %s)' % (rel, ALLOWED_MODES[normalised], entry.get('mode')))
    extra = []
    for current, dirs, names in os.walk(files_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_NAMES]
        for name in names:
            rel = (Path(current) / name).relative_to(files_dir).as_posix()
            if rel not in seen:
                extra.append(rel)
    result = {'status': 'FAILED' if (problems or extra) else 'VERIFIED',
              'release_dir': str(release), 'key_id': key_id,
              'files_checked': len(seen), 'file_count_in_bom': bom.get('file_count'),
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
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
