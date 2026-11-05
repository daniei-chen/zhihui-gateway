"""Reproduce audit findings using only temporary synthetic data and test keys.

Run with the primary Python runtime (cryptography installed). No production
paths, real credentials, providers, systemd actions or network calls are used.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import ModuleType, SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

REPO = Path(__file__).resolve().parents[3]
BASE = '4c6b4cb8cb790698e0753909745dfe8a7c78dd9f'



def load(relative, baseline):
    raw = (subprocess.check_output(['git', 'show', f'{BASE}:{relative}'], cwd=REPO)
           if baseline else (REPO / relative).read_bytes())
    name = ('baseline_' if baseline else 'fixed_') + relative.replace('/', '_').replace('-', '_')
    module = ModuleType(name)
    module.__file__ = str(REPO / relative)
    sys.modules[name] = module
    exec(compile(raw, str(REPO / relative), 'exec'), module.__dict__)
    return module


def pem(private):
    return private.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)


def run_verify(module, variant, root):
    release = root / 'release'
    runtime = release / 'files'
    runtime.mkdir(parents=True)
    program = runtime / 'app.py'
    program.write_bytes(b'print("synthetic fixture")\n')
    program.chmod(0o755 if variant == 'unsafe_mode' else 0o644)
    bom = module.build_bom(runtime)
    key = ed25519.Ed25519PrivateKey.generate()
    trusted = root / 'trusted.pem'
    trusted.write_bytes(pem(key))
    if variant == 'self_signed_publisher':
        key = ed25519.Ed25519PrivateKey.generate()
    if variant == 'unsafe_mode':
        program.chmod(0o777)
    elif variant == 'unlisted_bytecode':
        (runtime / '__pycache__').mkdir()
        (runtime / '__pycache__/extra.pyc').write_bytes(b'synthetic unlisted code')
    elif variant == 'parent_path':
        program.rename(release / 'outside.py')
        bom['files'][0]['path'] = '../outside.py'
    raw = json.dumps(bom, sort_keys=True).encode()
    public = pem(key)
    key_id = hashlib.sha256(public).hexdigest()[:16]
    (release / 'trust').mkdir()
    (release / 'trust' / f'release-pub-{key_id}.pem').write_bytes(public)
    (release / 'BOM.json').write_bytes(raw)
    (release / 'BOM.json.sig.json').write_text(json.dumps({
        'schema': module.SCHEMA_SIG, 'algorithm': 'ed25519', 'key_id': key_id,
        'signed_file': 'BOM.json', 'signed_sha256': hashlib.sha256(raw).hexdigest(),
        'signature_hex': key.sign(raw).hex()}))
    stream = io.StringIO()
    try:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            rc = module.cmd_verify(SimpleNamespace(release_dir=str(release), trusted_key=str(trusted)))
        return {'accepted': rc == 0, 'exit': rc, 'receipt': json.loads(stream.getvalue())}
    except SystemExit as exc:
        return {'accepted': False, 'exit': str(exc.code)}


def run_dry_run(module, variant, root):
    (root / 'deploy').mkdir()
    (root / 'server').mkdir()
    (root / 'server/new.py').write_text('synthetic = True\n')
    manifest = root / 'deploy/managed-release.json'
    manifest.write_text('{"files":{}}')
    signature = manifest.with_suffix('.sig')
    signature.write_bytes(b'previous synthetic signature')
    before = manifest.read_bytes(), signature.read_bytes()
    args = ['--manifest', str(manifest), '--dry-run']
    if variant == 'add':
        args += ['--add', 'server/new.py']
    else:
        private = root / 'synthetic-private.pem'
        public = root / 'synthetic-public.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'Ed25519', '-out', str(private)],
                       capture_output=True, check=True)
        subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)],
                       capture_output=True, check=True)
        args += ['--sign-only', '--key', str(private), '--trust', str(public)]
    with contextlib.redirect_stdout(io.StringIO()):
        rc = module.main(args)
    return {'exit': rc, 'manifest_changed': manifest.read_bytes() != before[0],
            'signature_changed': signature.read_bytes() != before[1]}


def run_retention(module, root):
    module.ROOT = root / 'backups'
    module.SNAPSHOTS = module.ROOT / 'snapshots'
    module.LATEST = module.ROOT / 'latest.json'
    module.SNAPSHOTS.mkdir(parents=True)
    outside = root / 'outside-backup-root'
    outside.mkdir()
    payload = b'synthetic encrypted fixture'
    (outside / 'part-000.age').write_bytes(payload)
    protected = outside / 'snapshot.tar.gz'
    protected.write_bytes(b'outside sentinel, not an approved deletion target')
    (outside / 'snapshot.json').write_text(json.dumps({
        'cipher_sha256': hashlib.sha256(payload).hexdigest(), 'parts': [{'name': 'part-000.age'}]}))
    (module.SNAPSHOTS / '20260101-000000').symlink_to(outside, target_is_directory=True)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        plan = module.build_plaintext_plan()
        rc = module.apply_plaintext_plan(plan, plan['confirm_token'])
    return {'outside_plaintext_survived': protected.exists(), 'exit': rc,
            'deletion_candidates': plan['prune_count']}


def run_purge(module, root):
    target = root / 'users.json'
    old = b'{"secret":"synthetic old secret","users":[],"api_keys":[]}'
    target.write_bytes(old)
    previous_umask = os.umask(0o022)
    try:
        with mock.patch.dict(os.environ, {'WB_USERS_FILE': str(target)}), contextlib.redirect_stdout(io.StringIO()):
            rc = module.main()
    finally:
        os.umask(previous_umask)
    backup = next(root.glob('users.json.bak*'))
    return {'exit': rc, 'backup_matches_original_bytes': backup.read_bytes() == old,
            'backup_mode': oct(backup.stat().st_mode & 0o777),
            'rotated_file_mode': oct(target.stat().st_mode & 0o777)}


def run_integrity(module, root):
    release = root / 'release'
    release.mkdir()
    (release / 'app.py').write_bytes(b'modified synthetic runtime')
    baseline = root / 'baseline.json'
    baseline.write_text(json.dumps({'files': {'app.py': {
        'candidate_sha256': hashlib.sha256(b'approved synthetic runtime').hexdigest()}}}))
    out = root / 'report.json'
    with contextlib.redirect_stdout(io.StringIO()):
        rc = module.main(['--release', str(release), '--out', str(out), '--baseline', str(baseline)])
    return {'exit': rc, 'reported_changed': json.loads(out.read_text())['baseline']['changed']}


def run_restore(module, root):
    module.WORK = root / 'work'
    module.KEY = root / 'synthetic-key'
    module.KEY.write_bytes(b'fixture key, never passed to a real age subprocess')
    snapshot = root / 'snapshot'
    snapshot.mkdir()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as archive:
        raw = b'{"files":[],"sqlite_snapshots":[]}'
        entry = tarfile.TarInfo('recovery-manifest.json')
        entry.size = len(raw)
        archive.addfile(entry, io.BytesIO(raw))
    ciphertext = b'synthetic ciphertext'
    (snapshot / 'part-000.age').write_bytes(ciphertext)
    (snapshot / 'snapshot.json').write_text(json.dumps({
        'cipher_sha256': hashlib.sha256(ciphertext).hexdigest(),
        'archive_sha256': '0' * 64, 'parts': [{'name': 'part-000.age'}]}))

    def start(command, **kwargs):
        if command[0] == 'cat':
            return SimpleNamespace(stdout=io.BytesIO(), wait=lambda: 0, returncode=0)
        assert command[0] == 'age'
        kwargs['stdout'].write(payload.getvalue())
        kwargs['stdout'].flush()
        return SimpleNamespace(communicate=lambda: (None, b''), returncode=0)

    with mock.patch.object(module.subprocess, 'Popen', side_effect=start), contextlib.redirect_stdout(io.StringIO()):
        rc = module.main([str(snapshot), '--skip-sqlite'])
    report = json.loads((module.WORK / 'drill-report.json').read_text())
    return {'exit': rc, 'result': report.get('result'),
            'complete_restore_verified': report.get('complete_restore_verified', 'not stated'),
            'archive_check': next(s for s in report['steps'] if s['step'] == 'archive_sha256')['ok']}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    rows = []
    cases = []
    for variant in ('self_signed_publisher', 'unsafe_mode', 'unlisted_bytecode', 'parent_path'):
        cases.append(('experimental_verify_' + variant, 'tools/release_sign.py',
                      lambda m, r, variant=variant: run_verify(m, variant, r)))
    for variant in ('add', 'sign_only'):
        cases.append(('signer_dry_run_' + variant, 'manager/deploy/sign-release-ed25519.py',
                      lambda m, r, variant=variant: run_dry_run(m, variant, r)))
    cases.extend([
        ('retention_directory_link', 'manager/deploy/recovery_retention.py', run_retention),
        ('private_credential_backup', 'manager/deploy/purge_credentials.py', run_purge),
        ('managed_candidate_hash_drift', 'ops/integrity/verify-runtime.py', run_integrity),
        ('restore_archive_mismatch', 'manager/deploy/restore_drill.py', run_restore),
    ])
    with tempfile.TemporaryDirectory(prefix='ops-audit-synthetic-') as temporary:
        for index, (name, relative, run) in enumerate(cases):
            row = {'case': name, 'file': relative}
            for baseline in (True, False):
                root = Path(temporary) / f'{index}-{baseline}'
                root.mkdir()
                row['baseline' if baseline else 'patched'] = run(load(relative, baseline), root)
            rows.append(row)
    files = sorted({row['file'] for row in rows})
    receipt = {'baseline_commit': BASE, 'synthetic_only': True,
               'production_keys_read': False, 'network_calls': False,
               'patched_input_sha256': {file: hashlib.sha256((REPO / file).read_bytes()).hexdigest() for file in files},
               'cases': rows}
    if args.output:
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
