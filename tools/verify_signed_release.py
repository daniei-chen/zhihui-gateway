"""Independent local verification of the signed release produced on the server.

Proves the signature and the per-file manifest can be checked WITHOUT the server
and without the private key: only the downloaded public key, the signed payload
and the candidate archive are used.

Usage: python -B -X utf8 tools/verify_signed_release.py --release <dir> --archive <tar.gz>
"""
from __future__ import annotations

import argparse
import hashlib
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True, help='dir with MANIFEST.sha256 / SIGNED-PAYLOAD.* / pubkey')
    parser.add_argument('--archive', required=True, help='candidate tar.gz to verify against the manifest')
    args = parser.parse_args()

    release = Path(args.release).resolve()
    archive = Path(args.archive).resolve()
    report: dict = {'release': str(release), 'archive': str(archive)}

    pub_bytes = (release / 'trust-release-pub.pem').read_bytes()
    public_key = serialization.load_pem_public_key(pub_bytes)
    payload = (release / 'SIGNED-PAYLOAD.txt').read_bytes()
    signature = (release / 'SIGNED-PAYLOAD.sig').read_bytes()
    try:
        public_key.verify(signature, payload)
        report['signature'] = 'OK'
    except Exception as exc:  # noqa: BLE001
        report['signature'] = 'FAILED: ' + type(exc).__name__
        print(report)
        return 1

    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    report['key_id'] = hashlib.sha256(der).hexdigest()[:16]
    report['payload_sha256'] = sha256_bytes(payload)
    report['signature_sha256'] = sha256_bytes(signature)

    with tempfile.TemporaryDirectory(prefix='zh-verify-') as tmp:
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter='data')
        root = Path(tmp) / 'workspace'
        if not root.is_dir():
            report['archive_layout'] = 'unexpected: no workspace/ dir'
            print(report)
            return 1
        manifest = (release / 'MANIFEST.sha256').read_text(encoding='utf-8').splitlines()
        missing, mismatch, checked = [], [], 0
        for line in manifest:
            if not line.strip():
                continue
            digest, rel = line.split('  ', 1)
            target = root / rel
            if not target.is_file():
                missing.append(rel)
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            checked += 1
            if actual != digest:
                mismatch.append(rel)
        report['files_in_manifest'] = len(manifest)
        report['files_checked'] = checked
        report['missing'] = missing[:20]
        report['mismatch'] = mismatch[:20]
        report['status'] = 'VERIFIED' if not missing and not mismatch else 'FAILED'

    print('\n'.join('%s: %s' % (k, v) for k, v in report.items()))
    return 0 if report['status'] == 'VERIFIED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
