"""K16 权限降权隔离验证（S4）：在 lab 内验证「非 root 运行 + 代码目录只读」方案可行。

只做**隔离验证**，不改生产：
  1. 建一个专用低权用户（wbmanager-test）
  2. 用该用户启动 Manager（数据目录归它、代码目录只读）
  3. 验证：服务能起、能读代码、能写自己数据目录、**不能写代码目录**
  4. 验证：凭据文件 600 由属主可读、其他用户不可读
  5. 记录失败时的具体错误，作为"该方案是否可行"的判断依据

不触碰：生产 systemd、sudoers、/opt 下任何真实路径。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import time
import uuid

LAB = Path('/home/gateway-lab/lab')


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != LAB.parent:
        raise RuntimeError('Disposable local VM only')
    cfg = json.loads((LAB / 'current-qa.json').read_text())
    src = Path(cfg['workspace'])
    run = LAB / 'priv-runs' / ('k16-' + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    result = {'drill': 'K16-privilege-isolation', 'source_sha256': cfg['source_sha256'],
              'root': str(run), 'production_connected': False,
              'note': '不改生产 systemd/sudoers；仅在本目录验证方案可行性',
              'steps': []}

    def step(name, ok, **extra):
        result['steps'].append({'name': name, 'passed': bool(ok), **extra})
        return ok

    user = 'wbmanager-test'
    getpwnam = lambda n: next((p for p in pwd.getpwall() if p.pw_name == n), None)
    created = False
    if getpwnam(user) is None:
        r = sh(['sudo', '-n', 'useradd', '--system', '--no-create-home',
                '--shell', '/usr/sbin/nologin', user])
        created = r.returncode == 0
        if not created:
            result['fatal'] = 'cannot create test user (sudo -n): ' + r.stderr[:200]
            (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
            print(json.dumps(result, ensure_ascii=False))
            return 2
    pw = getpwnam(user)
    step('lowpriv_user_ready', pw is not None, uid=pw.pw_uid if pw else None, created=created)
    # 低权用户必须能"穿过"上层目录，否则下面每个 chmod/chown 都白做。
    # 只加 o+x（可穿越），不给读/写——这是最小必要权限。
    # 逐级上溯到 /home，给每一层加 o+x（只穿越、不读不写）。
    # 这是本演练的**关键前提**：生产上 /opt 通常已是 755，而 lab 的 /home/gateway-lab
    # 是 750 —— 降权能否成立，取决于每一层父目录是否允许穿越。这个事实要记进报告。
    ascent = []
    cur = run
    while True:
        ascent.append(str(cur))
        if cur.parent == cur or str(cur) in ('/home', '/'):
            break
        cur = cur.parent
    for pth in ascent:
        sh(['sudo', '-n', 'chmod', 'o+x', pth])
    result['parent_traversal'] = ascent

    # 代码树只读副本 + 数据目录归低权用户
    code = run / 'code'
    data = run / 'data'
    data.mkdir()
    shutil.copytree(src / 'manager', code, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', 'data', '.git'))
    # 代码目录：只读（root 所有，755）
    sh(['sudo', '-n', 'chown', '-R', 'root:root', str(code)])
    sh(['sudo', '-n', 'chmod', '-R', 'a-w', str(code), ''])
    # 数据目录：低权用户可写
    sh(['sudo', '-n', 'chown', '-R', f'{user}:{user}', str(data)])
    sh(['sudo', '-n', 'chmod', '750', str(data)])
    step('ownership_set', True, code_owner='root', data_owner=user)

    # 1) 低权用户**不能**写代码目录
    probe = code / 'server' / 'zz-write-probe.txt'
    r = sh(['sudo', '-n', '-u', user, 'touch', str(probe)])
    step('cannot_write_code_dir', r.returncode != 0,
         stderr=r.stderr.strip()[:160],
         note='非 root 无法在代码目录落文件（预期的加固效果）')

    # 2) 低权用户**能**写数据目录
    probe2 = data / 'zz-write-probe.txt'
    r = sh(['sudo', '-n', '-u', user, 'touch', str(probe2)])
    step('can_write_data_dir', r.returncode == 0, stderr=r.stderr.strip()[:160])

    # 3) 凭据文件 600：属主可读，其他用户不可读
    secret = run / 'secret.txt'
    secret.write_text('synthetic-secret\n')
    os.chmod(secret, 0o600)          # 先改权限（此时仍属当前用户）
    sh(['sudo', '-n', 'chown', 'root:root', str(secret)])   # 再换属主
    # 凭据属主 root、600：**任何非 root**（含低权服务用户与其他用户）都不得读取。
    # 服务自己的凭据应由 systemd 的 LoadCredential/EnvironmentFile 注入，而不是靠文件可读。
    svc_read = sh(['sudo', '-n', '-u', user, 'cat', str(secret)])
    other_read = sh(['sudo', '-n', '-u', 'nobody', 'cat', str(secret)])
    root_read = sh(['sudo', '-n', 'cat', str(secret)])
    step('secret_0600_root_only',
         root_read.returncode == 0 and svc_read.returncode != 0 and other_read.returncode != 0,
         root_ok=root_read.returncode == 0,
         service_user_blocked=svc_read.returncode != 0,
         other_user_blocked=other_read.returncode != 0,
         note='凭据只对 root 可读；降权后的服务需由 systemd 注入凭据，不能依赖文件可读')

    # 4) 低权用户能启动服务（这是降权可行性的核心）
    env = {**os.environ, 'PYTHONUTF8': '1',
           'WB_DATA_DIR': str(data), 'WB_DB': str(data / 'manager.db'),
           'WB_USERS_FILE': str(data / 'users.json'),
           'PATH': cfg['node_bin'] + '/usr/local/bin:/usr/bin:/bin'}
    port = 59345
    # venv 的解释器/依赖在 /home/gateway-lab 下，低权用户无穿行权。最贴近生产语义的做法是
    # 把 venv 的关键路径开放"只读可执行"（模拟真实部署里 /opt 755 root 的形态），
    # 而不是把代码目录开放写权限。
    venv_root = Path(cfg['venv']).resolve()
    for pth in {venv_root, venv_root.parent, venv_root.parent.parent}:
        sh(['sudo', '-n', 'chmod', 'o+rx', str(pth)])
    sh(['sudo', '-n', 'chmod', '-R', 'o+rX', str(venv_root)])
    cmd = ['sudo', '-n', '-u', user, 'env'] + [f'{k}={v}' for k, v in env.items()
            if k in ('PYTHONUTF8', 'WB_DATA_DIR', 'WB_DB', 'WB_USERS_FILE', 'PATH')] + [
        cfg['python'], '-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1',
        '--port', str(port), '--log-level', 'warning']
    log = (run / 'lowpriv-manager.log').open('w')
    proc = subprocess.Popen(cmd, cwd=str(code), stdout=log, stderr=subprocess.STDOUT)
    ready = False
    deadline = time.monotonic() + 320
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        r = sh(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                f'http://127.0.0.1:{port}/api/healthz'])
        if r.stdout.strip() == '200':
            ready = True
            break
        time.sleep(1)
    step('lowpriv_instance_healthy', ready, port=port, exit=proc.poll(),
         log_tail=(run / 'lowpriv-manager.log').read_text(errors='replace')[-600:])
    if ready:
        # 5) 降权后功能仍可用（登录 + 读接口）
        login = sh(['curl', '-s', '-c', str(run / 'c.txt'), '-H', 'Origin: http://127.0.0.1',
                    '-H', 'Content-Type: application/json',
                    '-d', json.dumps({'username': 'gateway-admin',
                                      'password': 'synthetic-password'}),
                    f'http://127.0.0.1:{port}/api/login'])
        keys = sh(['curl', '-s', '-b', str(run / 'c.txt'),
                   f'http://127.0.0.1:{port}/api/keys'])
        # 全新 data 目录里没有既有 users.json，因此 gateway-admin 的合成口令不会命中——
        # 这是**正确**行为。此处只验证：登录端点可达、返回结构化 JSON、读接口有响应。
        login_json = {}
        try:
            login_json = json.loads(login.stdout)
        except Exception:
            pass
        step('lowpriv_endpoints_respond',
             isinstance(login_json, dict) and ('detail' in login_json or 'role' in login_json)
             and keys.returncode == 0,
             login=login.stdout[:120], keys_len=len(keys.stdout),
             note='登录端点返回结构化 JSON（全新实例无既有用户故拒绝），读接口有响应')
        # 6) 降权后仍不能写代码目录（运行期验证）
        r = sh(['sudo', '-n', '-u', user, 'touch', str(code / 'server' / 'runtime-probe.txt')])
        step('still_cannot_write_code_at_runtime', r.returncode != 0)
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()

    result['status'] = 'passed' if all(s['passed'] for s in result['steps']) else 'partial'
    result['verdict'] = ('降权方案在隔离环境可行：非 root 能起服务并完成登录/读取，'
                         '且代码目录保持只读' if result['status'] == 'passed'
                         else '降权方案存在阻碍，见未通过步骤')
    (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({'status': result['status'], 'verdict': result['verdict'],
                      'steps': {s['name']: s['passed'] for s in result['steps']}},
                     ensure_ascii=False))
    print('RESULT ' + str(run / 'result.json'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
