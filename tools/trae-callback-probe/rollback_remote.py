"""Remove only the exact experiment ingress generation, preserving business data."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path('/opt/zhihui-release/experiments/trae-callback-20261030-r2')
NGINX = Path('/etc/nginx/sites-enabled/zhihui.kaogong.art')
RATE = Path('/etc/nginx/conf.d/zh-trae-callback-probe-rate.conf')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    assert os.getuid() == 0
    sys.path.insert(0, '/opt/zhihui-release/writer-guard')
    from writer_entry import verify_host_closure
    from coordination import Coordination
    from state_guard import Paths
    verify_host_closure()
    with Coordination(Paths.production()).acquire():
        state = json.loads((ROOT / 'deployment.json').read_bytes())
        original, after = (ROOT / 'nginx-before.conf').read_bytes(), (ROOT / 'nginx-after.conf').read_bytes()
        assert sha(original) == state['before_sha256'] and sha(after) == state['after_sha256']
        assert not NGINX.is_symlink() and NGINX.read_bytes() == after
        assert not RATE.is_symlink() and sha(RATE.read_bytes()) == state['files']['nginx-rate.conf']
        # Block new callbacks, but never cancel/exchange an engine grant or restore
        # the Manager DB/users. Active user authorization will expire normally.
        temp = NGINX.with_name('.' + NGINX.name + '.probe-rollback')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, NGINX)
        RATE.unlink()
        subprocess.run(['nginx', '-t'], check=True, capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'stop', 'zh-trae-callback-probe.service'], check=True, timeout=20)
        state['phase'] = 'operator-rolled-back-retained-evidence'
        temp = ROOT / '.deployment.rollback'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, ROOT / 'deployment.json')
        for folder in (NGINX.parent, RATE.parent, ROOT):
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(fd)
            os.close(fd)
        print(json.dumps({'phase': state['phase'], 'business_data_restored': False,
                          'nginx_sha256': sha(NGINX.read_bytes())}))


if __name__ == '__main__':
    main()
