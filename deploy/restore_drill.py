#!/usr/bin/env python3
"""Bounded offline archive/decryption and SQLite recovery evidence.

All payload work is in a private temporary directory. No archived code is run,
no application is started and no existing business database is opened. A data
check is explicitly different from an application/offsite recovery proof.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


WORK = None
KEY = Path('/opt/workbuddy-manager/keys/recovery.agekey')
CHUNK = 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_CIPHER_PARTS = 512
MAX_CIPHER_BYTES = 2 * 1024**3
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_MEMBER_BYTES = 2 * 1024**3
MAX_UNPACKED_BYTES = 4 * 1024**3
MAX_MEMBERS = 100000
MIN_FREE_BYTES = 256 * 1024**2
DECRYPT_TIMEOUT = 900
SQLITE_TIMEOUT = 60
APPLICATION_TIMEOUT = 90
SHA = re.compile(r'[0-9a-f]{64}\Z')


class ApplicationProbeError(ValueError):
    def __init__(self, evidence):
        super().__init__('trusted application failed isolated startup/readiness checks')
        self.evidence = evidence


def _step(name: str, ok, detail: str = '') -> dict:
    return {'step': name, 'ok': ok, 'detail': detail}


def _path(value) -> Path:
    path = Path(value).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink or parent traversal in recovery path')
    return path


def _regular(path: Path, limit: int):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError('recovery input is linked, non-regular or too large')
    return info


def _read_json(path: Path):
    info = _regular(path, MAX_METADATA_BYTES)
    with path.open('rb') as stream:
        raw = stream.read(info.st_size + 1)
    if len(raw) > info.st_size:
        raise ValueError('recovery metadata too large')
    return json.loads(raw)


def _free(path: Path, required: int):
    if shutil.disk_usage(path).free < required + MIN_FREE_BYTES:
        raise ValueError('insufficient free space for recovery bounds')


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def _write_report(value: dict, work: Path) -> None:
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = work / 'drill-report.json'
    if target.is_symlink():
        raise ValueError('report must not be a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.drill-report-', dir=work)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _safe_sidecar(snapshot: Path, data: bytes) -> None:
    target = snapshot / 'recovery-manifest.json'
    if target.is_symlink():
        target.unlink()
    temporary = snapshot / f'.recovery-manifest.{os.getpid()}.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _assemble_parts(snapshot: Path, meta: dict, output: Path):
    parts = meta.get('parts')
    if not isinstance(parts, list) or not 0 < len(parts) <= MAX_CIPHER_PARTS:
        raise ValueError('cipher part inventory missing or too large')
    seen, inventory, total = set(), [], 0
    for item in parts:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            raise ValueError('invalid cipher part')
        name = item['name']
        if not re.fullmatch(r'snapshot\.part-[0-9]{3,8}\.age', name) or name in seen:
            raise ValueError('unsafe or repeated cipher part')
        seen.add(name)
        source = snapshot / name
        info = _regular(source, MAX_CIPHER_BYTES)
        total += info.st_size
        if total > MAX_CIPHER_BYTES:
            raise ValueError('ciphertext exceeds total byte limit')
        if 'bytes' in item and (type(item['bytes']) is not int or item['bytes'] != info.st_size):
            raise ValueError('cipher part size mismatch')
        inventory.append((source, item, info))
    if 'cipher_bytes' in meta and (type(meta['cipher_bytes']) is not int or meta['cipher_bytes'] != total):
        raise ValueError('ciphertext total size mismatch')
    _free(output.parent, total)
    digest, evidence = hashlib.sha256(), []
    with output.open('xb') as destination:
        for source, item, before in inventory:
            local, count = hashlib.sha256(), 0
            fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_nlink) != (
                        before.st_ino, before.st_dev, before.st_size, 1):
                    raise ValueError('cipher part changed before read')
                for block in iter(lambda: stream.read(CHUNK), b''):
                    count += len(block)
                    if count > before.st_size:
                        raise ValueError('cipher part grew during read')
                    destination.write(block)
                    local.update(block)
                    digest.update(block)
                after = os.fstat(stream.fileno())
            if count != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                raise ValueError('cipher part changed during read')
            if 'sha256' in item and item['sha256'] != local.hexdigest():
                raise ValueError('cipher part digest mismatch')
            evidence.append({'name': source.name, 'bytes': count, 'sha256': local.hexdigest()})
    return digest.hexdigest(), evidence


def _decrypt(ciphertext: Path, key: Path, output: Path):
    _regular(key, MAX_METADATA_BYTES)
    _free(output.parent, MAX_ARCHIVE_BYTES)
    options = {}
    if os.name == 'posix':
        import resource

        def limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARCHIVE_BYTES, MAX_ARCHIVE_BYTES))
        options['preexec_fn'] = limit
    with ciphertext.open('rb') as source, output.open('xb') as destination:
        process = subprocess.Popen(['age', '--decrypt', '-i', str(key), '-o', '-'],
            stdin=source, stdout=destination, stderr=subprocess.DEVNULL, **options)
        try:
            process.communicate(timeout=DECRYPT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ValueError('age decrypt timed out') from None
        if process.returncode:
            raise ValueError('age decrypt failed')
    _regular(output, MAX_ARCHIVE_BYTES)


class _BoundedTarInput:
    """Reject giant extended-header reads before tarfile allocates them."""
    def __init__(self, stream):
        self.stream = stream
        self.count = 0

    def read(self, size=-1):
        if size < 0 or size > CHUNK:
            raise ValueError('archive requested an oversized read')
        data = self.stream.read(size)
        self.count += len(data)
        if self.count > MAX_UNPACKED_BYTES + MAX_MEMBERS * 1024:
            raise ValueError('archive expanded stream exceeds limit')
        return data


class _SafeTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        archive._recovery_headers = getattr(archive, '_recovery_headers', 0) + 1
        if archive._recovery_headers > MAX_MEMBERS or self.size < 0 or self.size > MAX_MEMBER_BYTES:
            raise ValueError('archive header count or member byte limit exceeded')
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise ValueError('sparse recovery members are refused')
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                         tarfile.GNUTYPE_LONGLINK):
            total = getattr(archive, '_recovery_metadata_bytes', 0) + self.size
            if self.size > CHUNK or total > MAX_METADATA_BYTES:
                raise ValueError('archive extended headers exceed metadata limit')
            archive._recovery_metadata_bytes = total
        return super()._proc_member(archive)


def _name(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or len(name.encode()) > 1024 or path.is_absolute() or '..' in path.parts
            or path.as_posix() != name.rstrip('/') or '\\' in name or '\x00' in name):
        raise ValueError('unsafe archive member path')
    return path.as_posix()


def _members(archive: Path):
    with archive.open('rb') as source, gzip.GzipFile(fileobj=source) as unpacked:
        bounded = _BoundedTarInput(unpacked)
        with tarfile.open(fileobj=bounded, mode='r|', tarinfo=_SafeTarInfo) as bundle:
            for member in bundle:
                yield bundle, member
                # Streaming extraction does not need tarfile's growing index.
                bundle.members.clear()


def _scan_archive(archive: Path):
    seen, regular, total, sidecar, path_bytes = set(), {}, 0, None, 0
    for bundle, member in _members(archive):
        name = _name(member.name)
        if name in seen or len(seen) >= MAX_MEMBERS:
            raise ValueError('duplicate members or archive member limit exceeded')
        seen.add(name)
        path_bytes += len(name.encode())
        if path_bytes > MAX_METADATA_BYTES:
            raise ValueError('archive paths exceed metadata limit')
        if not (member.isfile() or member.isdir()) or member.issparse():
            raise ValueError('archive links, special files and sparse files are refused')
        if member.size < 0 or member.size > MAX_MEMBER_BYTES:
            raise ValueError('archive member exceeds byte limit')
        total += member.size
        if total > MAX_UNPACKED_BYTES:
            raise ValueError('archive unpacked total exceeds byte limit')
        if member.isfile():
            regular[name] = member.size
        if name == 'recovery-manifest.json':
            if not member.isfile() or member.size > MAX_METADATA_BYTES:
                raise ValueError('invalid recovery sidecar')
            with bundle.extractfile(member) as stream:
                sidecar = stream.read(member.size + 1)
            if len(sidecar) != member.size:
                raise ValueError('truncated recovery sidecar')
    if sidecar is None:
        raise ValueError('recovery sidecar missing')
    metadata = json.loads(sidecar)
    if not isinstance(metadata, dict):
        raise ValueError('recovery sidecar is not an object')
    return sidecar, metadata, regular, total


def _verify_data(archive: Path, metadata: dict, regular: dict, scratch: Path, application=None):
    records = metadata.get('files')
    if isinstance(records, list):
        names = [item.get('name') for item in records if isinstance(item, dict)]
        if len(names) != len(records) or len(set(names)) != len(names):
            raise ValueError('invalid recovery file inventory')
        records = dict(zip(names, records))
    if not isinstance(records, dict) or set(records) != set(regular) - {'recovery-manifest.json'}:
        raise ValueError('recovery inventory does not cover archive files')
    databases = metadata.get('sqlite_snapshots')
    if not isinstance(databases, list) or not databases:
        raise ValueError('sqlite restore inventory missing')
    database_names = set()
    for entry in databases:
        name = entry.get('name') if isinstance(entry, dict) else None
        if not isinstance(name, str) or _name(name) != name or name not in records or name in database_names:
            raise ValueError('invalid sqlite restore inventory')
        database_names.add(name)
    for name, item in records.items():
        if (not isinstance(name, str) or _name(name) != name or not isinstance(item, dict)
                or not SHA.fullmatch(str(item.get('sha256', '')))):
            raise ValueError('invalid recovery file digest')
        size = item.get('bytes', item.get('size'))
        if type(size) is not int or size != regular[name]:
            raise ValueError('recovery file size mismatch')
    app_names = {name for name in regular if name.startswith(('manager/data/', 'gateway/'))} if application else set()
    _free(scratch, sum(regular[name] for name in database_names | app_names))
    restored = {}
    for bundle, member in _members(archive):
        name = _name(member.name)
        if not member.isfile() or name == 'recovery-manifest.json':
            continue
        expected = records[name]
        digest = hashlib.sha256()
        count = 0
        # Flatten database names into generated private files, never extract paths.
        target = scratch / f'database-{len(restored)}.sqlite3' if name in database_names else None
        if name in app_names:
            target = application / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = target.open('xb') if target is not None else None
        try:
            with bundle.extractfile(member) as source:
                for block in iter(lambda: source.read(CHUNK), b''):
                    count += len(block)
                    if count > regular[name]:
                        raise ValueError('archive member size changed')
                    digest.update(block)
                    if destination is not None:
                        destination.write(block)
        finally:
            if destination is not None:
                destination.close()
        if count != regular[name] or digest.hexdigest() != expected['sha256']:
            raise ValueError('recovery file digest mismatch')
        if name in database_names:
            restored[name] = target
    for path in restored.values():
        deadline = time.monotonic() + SQLITE_TIMEOUT
        with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=1) as connection:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024 * 1024)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA cache_size=-2048')
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('sqlite integrity check failed')
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('sqlite foreign key check failed')
    return {'files_verified': len(records), 'sqlite_verified': len(restored),
            'isolation': 'private-temporary-files-read-only-sqlite'}


def _trusted_checkout(path: Path, commit: str):
    if not re.fullmatch(r'[0-9a-f]{40}', commit or ''):
        raise ValueError('an exact trusted source commit is required')
    command = ['git', '-C', str(path), '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null']
    env = {'PATH': os.environ.get('PATH', ''), 'GIT_TERMINAL_PROMPT': '0',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
    def git(*args):
        import resource
        def limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_METADATA_BYTES, MAX_METADATA_BYTES))
        with tempfile.TemporaryFile() as output:
            subprocess.run(command + list(args), stdout=output, stderr=subprocess.DEVNULL,
                           check=True, timeout=15, env=env, preexec_fn=limit)
            size = output.tell()
            if size > MAX_METADATA_BYTES:
                raise ValueError('trusted checkout inventory exceeds metadata limit')
            output.seek(0)
            return output.read(size + 1)
    if git('rev-parse', 'HEAD').decode().strip() != commit:
        raise ValueError('trusted checkout commit mismatch')
    if git('status', '--porcelain', '--untracked-files=all').strip():
        raise ValueError('trusted checkout must be clean')
    entries = git('ls-tree', '-rz', '--full-tree', 'HEAD').decode().strip('\0').split('\0')
    files, total = [], 0
    for entry in entries:
        record, name = entry.split('\t', 1)
        mode, kind, expected = record.split()
        if kind != 'blob' or mode not in ('100644', '100755') or _name(name) != name:
            raise ValueError('trusted checkout contains unsupported entries')
        source = path / name
        info = _regular(source, MAX_MEMBER_BYTES)
        total += info.st_size
        if total > 512 * 1024**2 or len(files) >= MAX_MEMBERS:
            raise ValueError('trusted checkout exceeds source byte budget')
        digest = hashlib.sha1(('blob %d\0' % info.st_size).encode())
        with source.open('rb') as stream:
            for block in iter(lambda: stream.read(CHUNK), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            # Git's tracked .gitattributes deliberately checks Windows .cmd
            # files out as CRLF. Permit only this UTF-8 newline conversion;
            # arbitrary clean filters/assume-unchanged bytes remain refused.
            if info.st_size > MAX_METADATA_BYTES:
                raise ValueError('trusted source bytes do not match exact commit')
            expected_bytes = git('cat-file', 'blob', expected)
            with source.open('rb') as stream:
                actual_bytes = stream.read(info.st_size + 1)
            try:
                same = actual_bytes.decode('utf-8').replace('\r\n', '\n') == expected_bytes.decode('utf-8').replace('\r\n', '\n')
            except UnicodeDecodeError:
                same = False
            if not same:
                raise ValueError('trusted source bytes do not match exact commit')
        files.append(name)
    if not files or 'server/main.py' not in files:
        raise ValueError('trusted application checkout inventory invalid')
    return files


def _verify_application(checkout: Path, commit: str, application: Path, scratch: Path):
    if os.name != 'posix':
        raise ValueError('bounded offline application probe requires POSIX resource limits')
    files = _trusted_checkout(checkout, commit)
    if not (application / 'manager/data/manager.db').is_file():
        raise ValueError('manager database missing from restored application state')
    inventory = scratch / 'trusted-files.json'
    inventory.write_text(json.dumps(files))
    env = {'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    with tempfile.TemporaryFile() as logs:
        process = subprocess.run([sys.executable, '-I', '-B',
            str(Path(__file__).with_name('recovery_probe.py')), str(checkout), str(application), str(inventory)],
            cwd=scratch, env=env, stdin=subprocess.DEVNULL, stdout=logs, stderr=logs,
            timeout=APPLICATION_TIMEOUT)
    result_file = application / '.application-probe.json'
    result = _read_json(result_file) if result_file.is_file() else {}
    if process.returncode or not isinstance(result, dict) or result.get('ok') is not True:
        evidence = {name: result[name] for name in ('scope', 'health_status', 'ready_status', 'error', 'blocked_operations')
                    if isinstance(result, dict) and name in result}
        raise ApplicationProbeError(evidence)
    return {**result, 'source_commit': commit}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', nargs='?')
    parser.add_argument('--archive', help='explicit already decrypted tar.gz for an offline check')
    parser.add_argument('--archive-sha256', help='required trusted receipt for --archive')
    parser.add_argument('--key', help='explicit offline age identity file')
    parser.add_argument('--work-dir', help='private report directory; default is a new temporary directory')
    parser.add_argument('--trusted-checkout', help='optional clean reviewed Git checkout for bounded offline ASGI startup')
    parser.add_argument('--source-commit', help='exact 40-character commit required with --trusted-checkout')
    parser.add_argument('--restore-sidecar', action='store_true')
    parser.add_argument('--skip-sqlite', action='store_true')
    args = parser.parse_args(argv)
    if bool(args.snapshot) == bool(args.archive):
        parser.error('provide exactly one snapshot directory or --archive')
    if args.archive and (not SHA.fullmatch(args.archive_sha256 or '') or args.restore_sidecar):
        parser.error('--archive requires --archive-sha256 and cannot restore a source sidecar')
    if bool(args.trusted_checkout) != bool(args.source_commit) or args.trusted_checkout and args.skip_sqlite:
        parser.error('application verification requires --trusted-checkout, --source-commit and SQLite verification')
    work = _path(args.work_dir or WORK or tempfile.mkdtemp(prefix='zhihui-restore-report-'))
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = work.lstat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
        parser.error('work directory must be private and owned by the current user')
    report = {'schema': 2, 'created_at': int(time.time()), 'result': 'failed',
              'complete_restore_verified': False, 'application_start_verified': False,
              'offsite_restore_verified': False, 'data_restore_verified': False,
              'sqlite_skipped': bool(args.skip_sqlite), 'read_only_source': True, 'steps': []}
    try:
        with tempfile.TemporaryDirectory(prefix='.payload-', dir=work) as temporary:
            scratch = Path(temporary)
            if args.archive:
                archive = _path(args.archive)
                _regular(archive, MAX_ARCHIVE_BYTES)
                recorded_archive = args.archive_sha256
                snapshot = None
            else:
                snapshot = _path(args.snapshot)
                meta = _read_json(snapshot / 'snapshot.json')
                if not isinstance(meta, dict):
                    raise ValueError('snapshot receipt invalid')
                cipher = scratch / 'ciphertext.age'
                digest, parts = _assemble_parts(snapshot, meta, cipher)
                valid = SHA.fullmatch(str(meta.get('cipher_sha256', ''))) and digest == meta['cipher_sha256']
                report['steps'].append(_step('cipher_sha256', bool(valid)))
                report['steps'][-1]['evidence'] = parts
                if not valid:
                    raise ValueError('cipher digest mismatch')
                archive = scratch / 'archive.tar.gz'
                _decrypt(cipher, _path(args.key or KEY), archive)
                recorded_archive = meta.get('archive_sha256')
            if not isinstance(recorded_archive, str) or not SHA.fullmatch(recorded_archive):
                report['steps'].append(_step('archive_sha256', None, 'archive receipt missing'))
                raise ValueError('archive receipt missing')
            archive_ok = _hash(archive) == recorded_archive
            report['steps'].append(_step('archive_sha256', archive_ok))
            if not archive_ok:
                raise ValueError('archive digest mismatch')
            sidecar, metadata, regular, total = _scan_archive(archive)
            report['archive_sha256'] = recorded_archive
            report['steps'].append(dict(_step('archive_bounds', True),
                                        files=len(regular), unpacked_bytes=total))
            if args.restore_sidecar:
                _safe_sidecar(snapshot, sidecar)
                report['read_only_source'] = False
                report['steps'].append(_step('restore_sidecar', True))
            if args.skip_sqlite:
                report['result'] = 'sampled'
            else:
                application = scratch / 'application' if args.trusted_checkout else None
                evidence = _verify_data(archive, metadata, regular, scratch, application=application)
                report['steps'].append(dict(_step('sqlite_restore', True), **evidence))
                report['data_restore_verified'] = True
                report['result'] = 'data_verified'
                if args.trusted_checkout:
                    proof = _verify_application(_path(args.trusted_checkout), args.source_commit, application, scratch)
                    report['steps'].append(dict(_step('application_start', True), **proof))
                    report['application_start_verified'] = True
                    report['result'] = 'application_verified'
    except Exception as exc:  # noqa: BLE001 - evidence must record a failed drill
        if isinstance(exc, ApplicationProbeError):
            report['steps'].append(dict(_step('application_start', False), **exc.evidence))
        report['error'] = type(exc).__name__
        # Error diagnostics may contain private archive filenames. Publish only type.
        report['result'] = 'failed'
        _write_report(report, work)
        return 5
    _write_report(report, work)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
