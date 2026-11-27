"""Publish credential-free backup evidence for the unprivileged health monitor."""
import json
import os
from pathlib import Path
import re
import tempfile
import time

DEFAULT_STATUS = Path('/var/lib/zhihui-gateway/backup-status.json')
MAX_EVIDENCE_AGE = 26 * 3600
_SHA = re.compile(r'[0-9a-f]{64}\Z')
if __package__:
    from .offsitebackup import READBACK_METHOD
else:  # Standalone backup_application.py uses isolated Python and sibling imports.
    from offsitebackup import READBACK_METHOD


def _readback(result, now):
    remote = result.get('offsite')
    if not isinstance(remote, dict) or remote.get('verified_remote') is not True:
        return None
    evidence = remote.get('readback')
    if not isinstance(evidence, dict):
        return None
    stamp = evidence.get('verified_at')
    cipher_bytes = evidence.get('cipher_bytes')
    source_hash = result.get('sha256')
    if (evidence.get('method') != READBACK_METHOD or evidence.get('verified') is not True
            or evidence.get('approved_offsite_target') is not True
            or type(stamp) is not int or not -300 <= now - stamp <= MAX_EVIDENCE_AGE
            or type(cipher_bytes) is not int or cipher_bytes <= 0
            or not _SHA.fullmatch(str(source_hash or ''))
            or evidence.get('source_archive_sha256') != source_hash
            or remote.get('source_archive_sha256') != source_hash
            or not _SHA.fullmatch(str(evidence.get('cipher_sha256', '')))
            or evidence.get('cipher_sha256') != remote.get('cipher_sha256')
            or cipher_bytes != remote.get('cipher_bytes')
            or not re.fullmatch(r'[0-9a-f]{40}', str(evidence.get('commit', '')))
            or evidence.get('commit') != remote.get('commit')):
        return None
    return {name: evidence[name] for name in ('method', 'verified_at', 'commit',
            'cipher_sha256', 'cipher_bytes', 'source_archive_sha256')}


def verified_public_readback(data, now):
    """A report rewrite/mtime change cannot renew old download evidence."""
    evidence = data.get('offsite_readback')
    if data.get('schema') != 2 or data.get('offsite') is not True or not isinstance(evidence, dict):
        return False
    stamp = evidence.get('verified_at')
    return (evidence.get('method') == READBACK_METHOD and type(stamp) is int
            and -300 <= now - stamp <= MAX_EVIDENCE_AGE
            and _SHA.fullmatch(str(evidence.get('source_archive_sha256', ''))) is not None
            and evidence.get('source_archive_sha256') == data.get('archive_sha256')
            and _SHA.fullmatch(str(evidence.get('cipher_sha256', ''))) is not None
            and re.fullmatch(r'[0-9a-f]{40}', str(evidence.get('commit', ''))) is not None
            and type(evidence.get('cipher_bytes')) is int and evidence['cipher_bytes'] > 0)


def report_age(data, mtime, now):
    """Legacy local evidence remains readable; new evidence has immutable time."""
    age = now - mtime
    if age < -300:
        raise ValueError('future backup report timestamp')
    if data.get('schema') == 2:
        stamp = data.get('created_at')
        if type(stamp) is not int or now - stamp < -300:
            raise ValueError('invalid backup evidence timestamp')
        age = max(age, now - stamp)
    return age

def publish_verified_status(result, destination=DEFAULT_STATUS):
    size = result.get('bytes')
    if result.get('restore_check') != 'ok' or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError('unverified backup cannot publish healthy status')
    destination = Path(destination)
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError('backup status path must not be a symlink')
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    # Root backup jobs use umask 077. The dedicated directory contains only
    # credential-free health evidence and must remain traversable by uid 10001.
    if destination.parent == DEFAULT_STATUS.parent:
        destination.parent.chmod(0o755)
    now = int(time.time())
    readback = _readback(result, now)
    stamp = result.get('verified_at', now)
    if type(stamp) is not int or not -300 <= now - stamp <= MAX_EVIDENCE_AGE:
        raise ValueError('backup validation evidence is stale or invalid')
    public = {'schema': 2, 'created_at': stamp, 'bytes': size,
              'restore_check': 'ok',
              'offsite': readback is not None,
              'archive_sha256': result.get('sha256') if _SHA.fullmatch(str(result.get('sha256', ''))) else None,
              'offsite_readback': readback, 'offsite_restore_verified': False}
    fd, temporary = tempfile.mkstemp(prefix='.backup-status-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), 0o644)
            else:  # pragma: no cover - exercised on Windows
                os.chmod(temporary, 0o644)
            json.dump(public, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return public
