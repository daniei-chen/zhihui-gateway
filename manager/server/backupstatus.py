"""Publish credential-free backup evidence for the unprivileged health monitor."""
import json
import os
from pathlib import Path
import tempfile
import time

DEFAULT_STATUS = Path('/var/lib/zhihui-gateway/backup-status.json')

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
    remote = result.get('offsite')
    public = {'schema': 1, 'created_at': int(time.time()), 'bytes': size,
              'restore_check': 'ok',
              'offsite': remote is True or isinstance(remote, dict) and remote.get('verified_remote') is True}
    fd, temporary = tempfile.mkstemp(prefix='.backup-status-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(public, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return public
