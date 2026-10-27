"""A: 引擎夹具可控性验证 —— 通过 model 后缀 :mode 驱动 FakeProvider 的 15 种模式。

这是"引擎控制夹具"的真实替代：不需要模拟引擎内部，只要证明**同一推理入口**在不同
模式下产生可区分的行为，并记录 UI 的表现。跑在本地 VM 内，直连 Manager 端口。
"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess

LAB = Path('/home/gateway-lab/lab')


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    if socket.gethostname() != 'zhihui-local-lab':
        raise RuntimeError('Disposable local VM only')
    browser = json.loads((LAB / 'browser-current.json').read_text())
    root = Path(browser['root'])
    mgr_port = browser['manager_port']
    base = 'https://127.0.0.1:443'   # VM 内 fixture nginx 监听 443（宿主 18443 是 QEMU 转发）
    jar = root / 'engine-probe.jar'
    run = LAB / 'engine-runs' / 'probe'
    run.mkdir(parents=True, exist_ok=True)
    result = {'production_connected': False, 'manager_port': mgr_port, 'modes': {}}

    # 登录拿会话（用于建/删密钥）
    sh(['curl', '-sk', '--resolve', 'zhihui.lab:443:127.0.0.1', '-c', str(jar), '-H', f'Origin: {base}', '-H', 'X-QTG-Platform: workbuddy',
        '-H', 'Content-Type: application/json',
        '-d', json.dumps({'username': 'gateway-admin',
                          'password': 'synthetic-password'}),
        f'{base}/api/login'])
    created = sh(['curl', '-sk', '--resolve', 'zhihui.lab:443:127.0.0.1', '-b', str(jar), '-H', f'Origin: {base}', '-H', 'X-QTG-Platform: workbuddy',
                  '-H', 'Content-Type: application/json',
                  '-d', json.dumps({'name': 'qa-engine-probe',
                                    'platforms': ['workbuddy']}),
                  f'{base}/api/keys'])
    try:
        key_body = json.loads(created.stdout)
    except Exception:
        result['error'] = 'cannot create key: ' + created.stdout[:200]
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(json.dumps(result, ensure_ascii=False))
        return 2
    api_key = key_body.get('key', '')
    key_id = key_body.get('id')
    result['api_key_prefix'] = api_key[:10]

    modes = ['normal', 'error_400', 'error_429', 'error_500', 'error_503',
             'missing_usage', 'duplicate_chunk', 'broken_stream', 'slow_headers',
             'huge_response']
    url = f'http://127.0.0.1:{mgr_port}/v1/responses'
    for mode in modes:
        body = json.dumps({'model': f'fake-model:{mode}', 'input': f'probe-{mode}'})
        r = sh(['curl', '-s', '-m', '90', '-o', str(run / f'{mode}.out'),
                '-w', '%{http_code}', '-X', 'POST',
                '-H', f'Authorization: Bearer {api_key}',
                '-H', 'Content-Type: application/json', '-d', body, url])
        out = (run / f'{mode}.out').read_text(errors='replace')[:300]
        result['modes'][mode] = {'status': r.stdout.strip(), 'body_head': out}
        print(f'{mode:16s} {r.stdout.strip():4s} {out[:80]!r}')

    # 清理密钥
    if key_id:
        sh(['curl', '-sk', '--resolve', 'zhihui.lab:443:127.0.0.1', '-b', str(jar), '-X', 'DELETE',
            '-H', f'Origin: {base}', '-H', 'X-QTG-Platform: workbuddy', f'{base}/api/keys/{key_id}'])
    jar.unlink(missing_ok=True)

    # 判定：是否观察到可区分的多种行为
    statuses = {m: v['status'] for m, v in result['modes'].items()}
    distinct = set(statuses.values())
    result['distinct_statuses'] = sorted(distinct)
    result['verdict'] = ('ENGINE_CONTROLLABLE' if len(distinct) > 1
                         else 'ALL_SAME_STATUS')
    (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({'verdict': result['verdict'], 'statuses': statuses},
                     ensure_ascii=False))
    print('RESULT ' + str(run / 'result.json'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
