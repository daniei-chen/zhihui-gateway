"""Fixed, coordinated install/rollback of the isolated experiment, not Manager."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/opt/zhihui-release/experiments/trae-callback-20261030-r2')
NGINX = Path('/etc/nginx/sites-enabled/zhihui.kaogong.art')
RATE = Path('/etc/nginx/conf.d/zh-trae-callback-probe-rate.conf')
UNIT = Path('/etc/systemd/system/zh-trae-callback-probe.service')
NAME = UNIT.name
EXPECTED = '7d6f23879a32e439fad8664b516cbbd0ea094fd02b1db2e03032bd0cae79a6d9'
HOME = '85a3b85ed38bd27eb83af455c0e3c68db2a10e2305f660d364e601dcb35dfa99'
LOGIN = 'd18c17efce22e61cef894c819ab4c61eb7f9e17e248efe22ccf13b70fe1a7170'
FILES = ('server.py', 'test_server.py', 'README.md', 'zh-trae-callback-probe.service',
         'nginx-rate.conf', 'nginx-location.conf', 'deploy_remote.py', 'rollback_remote.py')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run(args):
    return subprocess.run(args, check=True, capture_output=True, timeout=60)


def durable(path, data, mode=0o644, replace=False):
    target = path.with_name('.' + path.name + '.probe-new') if replace else path
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(target, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def receipt(value):
    durable(ROOT / 'deployment.json', (json.dumps(value, indent=2) + '\n').encode(), replace=True)


def request(path, cookie=''):
    conn = http.client.HTTPConnection('127.0.0.1', 17388, timeout=15)
    try:
        conn.request('GET', path, headers={'Host': 'zhihui.kaogong.art', 'Cookie': cookie})
        response = conn.getresponse()
        return response.status, response.read(65536)
    finally:
        conn.close()


def main():
    assert os.getuid() == 0
    sys.path.insert(0, '/opt/zhihui-release/writer-guard')
    from writer_entry import verify_host_closure
    from coordination import Coordination
    from state_guard import Paths
    verify_host_closure()
    assert sha(Path('/opt/zhihui-release/writer-guard/host-files.json').read_bytes()) == (
        '0e4706853fa5cfa4fb30eb955605f904d92f7c2bba548fd02f6cfad46997042f')
    supplied = json.loads(sys.stdin.buffer.read(262145))
    assert set(supplied) == {'files', 'sha256'} and set(supplied['files']) == set(FILES)
    data = {name: base64.b64decode(supplied['files'][name], validate=True) for name in FILES}
    assert {name: sha(value) for name, value in data.items()} == supplied['sha256']
    assert sum(map(len, data.values())) < 131072
    with Coordination(Paths.production()).acquire():
        assert not ROOT.exists() and not RATE.exists()
        old_unit = UNIT.read_bytes() if UNIT.exists() else None
        if old_unit is not None:
            assert not UNIT.is_symlink() and sha(old_unit) == '74ea55e87a6a63f2a739a3d83770dc607d3d427cdbd66d834a12910f84aa0e67'
            assert run(['systemctl', 'show', NAME, '-p', 'ActiveState', '--value']).stdout.strip() == b'inactive'
        assert not NGINX.is_symlink() and sha(NGINX.read_bytes()) == EXPECTED
        assert sha(Path('/var/www/zhihui-home/index.html').read_bytes()) == HOME
        assert sha(Path('/var/www/zhihui-login/index.html').read_bytes()) == LOGIN
        assert not any(':17388 ' in line for line in run(['ss', '-H', '-ltn']).stdout.decode().splitlines())
        manager_pid = run(['systemctl', 'show', 'workbuddy-web.service', '-p', 'MainPID', '--value']).stdout.strip()
        assert int(manager_pid) > 0
        original = NGINX.read_bytes()
        anchor = b'    server_name zhihui.kaogong.art;\n'
        assert original.count(anchor) == 2
        candidate = original.replace(anchor, anchor + data['nginx-location.conf'] + b'\n', 1)
        after = sha(candidate)
        ROOT.mkdir(mode=0o755, parents=True)
        state = {'phase': 'prepared', 'before_sha256': EXPECTED, 'after_sha256': after,
                 'files': supplied['sha256'], 'installed_at': int(time.time()),
                 'manager_pid_before': int(manager_pid), 'provider_exchange': False,
                 'real_callback_verified': False}
        durable(ROOT / 'nginx-before.conf', original, 0o600)
        durable(ROOT / 'nginx-after.conf', candidate, 0o600)
        if old_unit is not None:
            durable(ROOT / 'predecessor-unit.service', old_unit)
        for name in FILES:
            durable(ROOT / name, data[name])
        receipt(state)
        changed = False
        try:
            tests = run(['runuser', '-u', 'nobody', '--', '/usr/bin/python3', '-I', '-B', '-m',
                         'unittest', 'discover', '-s', str(ROOT), '-p', 'test_server.py', '-v'])
            durable(ROOT / 'synthetic-tests.txt', tests.stdout + tests.stderr)
            durable(UNIT, data['zh-trae-callback-probe.service'], replace=old_unit is not None)
            run(['systemd-analyze', 'verify', str(UNIT)])
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'start', NAME])
            assert run(['systemctl', 'is-active', NAME]).stdout.strip() == b'active'
            for attempt in range(40):
                try:
                    code, _ = request('/trae-callback-test/')
                    break
                except ConnectionRefusedError:
                    if attempt == 39:
                        raise
                    time.sleep(0.25)
            assert code == 401
            code, _ = request('/trae-callback-test/return/' + 'a' * 64 + '?scope=synthetic')
            assert code == 303
            assert sha(NGINX.read_bytes()) == EXPECTED
            durable(RATE, data['nginx-rate.conf'])
            state['phase'] = 'ingress-intent'
            receipt(state)
            durable(NGINX, candidate, replace=True)
            changed = True
            run(['nginx', '-t'])
            run(['systemctl', 'reload', 'nginx'])
            state['phase'] = 'installed'
            receipt(state)
            assert manager_pid == run(['systemctl', 'show', 'workbuddy-web.service', '-p', 'MainPID', '--value']).stdout.strip()
            assert sha(Path('/var/www/zhihui-home/index.html').read_bytes()) == HOME
            assert sha(Path('/var/www/zhihui-login/index.html').read_bytes()) == LOGIN
            print(json.dumps(state))
        except Exception:
            # No predecessor DB, service, release, login or homepage restoration.
            # Only undo this exact config generation; never overwrite other drift.
            if changed:
                if sha(NGINX.read_bytes()) != after:
                    state['phase'] = 'rollback-blocked-ingress-drift'
                    receipt(state)
                    raise RuntimeError('ingress changed externally; retained for manual inspection') from None
                durable(NGINX, original, replace=True)
            if RATE.exists():
                assert sha(RATE.read_bytes()) == sha(data['nginx-rate.conf'])
                RATE.unlink()
            run(['nginx', '-t'])
            run(['systemctl', 'reload', 'nginx'])
            subprocess.run(['systemctl', 'stop', NAME], capture_output=True, timeout=20)
            state['phase'] = 'rolled-back-retained-evidence'
            receipt(state)
            raise


if __name__ == '__main__':
    main()
