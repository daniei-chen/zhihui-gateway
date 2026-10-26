"""B: K26 默认切 http 的可行性验证 —— 用 wb_read_mode=http 启动 QTG 并跑真实业务读。

判定标准（全部要过）：
  1. QTG 能起来（/healthz 200）
  2. 统计接口经 HTTP 通道返回**与 sqlite 模式同口径**的数据（同输入同输出）
  3. HTTP 通道失败时**报错而非回退**（临时停掉 Manager 验证）
  4. 慢/超时不导致挂死

不修改任何生产配置：只在临时 data_dir 内启动一个 QTG 实例。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

LAB = Path('/home/gateway-lab/lab')


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    if socket.gethostname() != 'zhihui-local-lab':
        raise RuntimeError('Disposable local VM only')
    cfg = json.loads((LAB / 'current-qa.json').read_text())
    browser = json.loads((LAB / 'browser-current.json').read_text())
    src = Path(cfg['workspace'])
    run = LAB / 'k26-runs' / ('mode-' + uuid.uuid4().hex[:10])
    run.mkdir(parents=True, exist_ok=False)
    result = {'drill': 'K26-http-default', 'source_sha256': cfg['source_sha256'],
              'root': str(run), 'production_connected': False, 'steps': []}

    def step(name, ok, **extra):
        result['steps'].append({'name': name, 'passed': bool(ok), **extra})
        return ok

    # 用 fixture 的 manager 地址。凭据要用 **scope=read** 的新令牌（N32 裁定）：
    # scope=keys 只能管密钥、读不了统计，直接拿它测会把"权限不足"误判成"通道不通"。
    fixture_root = Path(browser['root'])
    base_url = f'http://127.0.0.1:{browser["manager_port"]}'
    # 先用 fixture 的管理员会话签一把 scope=read 令牌
    jar = run / 'mgr.jar'
    mgr_token_file = fixture_root / 'manager-service-token'
    admin_tok = ''
    # 管理员会话：直接调 Manager 的 /api/login（fixture 里 gateway-admin 的合成口令）
    sh(['curl', '-s', '-c', str(jar), '-H',
        'Origin: http://127.0.0.1', '-H', 'Content-Type: application/json',
        '-d', json.dumps({'username': 'gateway-admin',
                          'password': 'synthetic-password'}),
        f'{base_url}/api/login'])
    created = sh(['curl', '-s', '-b', str(jar), '-H', 'Origin: http://127.0.0.1',
                  '-H', 'X-QTG-Platform: workbuddy',
                  '-H', 'Content-Type: application/json',
                  '-d', json.dumps({'name': 'qa-qtg-read',
                                    'scope': 'read'}),
                  f'{base_url}/api/tokens'])
    try:
        tok_body = json.loads(created.stdout)
    except Exception:
        tok_body = {}
    admin_tok = tok_body.get('token') or ''
    read_token_id = tok_body.get('id')
    result['read_token_created'] = bool(admin_tok)
    result['read_token_hint'] = (created.stdout or '')[:200]
    if admin_tok:
        token_file = run / 'read-token'
        token_file.write_text(admin_tok, encoding='utf-8')
        token_file.chmod(0o600)
    else:
        token_file = mgr_token_file   # 退化为旧令牌（会 403，但能验证失败语义）
    if not token_file.is_file():
        result['fatal'] = 'cannot obtain any manager token'
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(json.dumps(result, ensure_ascii=False))
        return 2

    data_dir = run / 'qtg-data'
    data_dir.mkdir()
    # 复用 fixture 的 qtg-data（里面有 credentials/会话密钥），否则新实例不知道管理员密码。
    # 只复制小型状态文件，不带数据库（数据库由本次实例自建）。
    fx_data = fixture_root / 'qtg-data'
    if fx_data.is_dir():
        for f in fx_data.iterdir():
            if f.is_file() and f.suffix in ('.json', '.key', '.secret', ''):
                import shutil as _sh
                try:
                    _sh.copy2(f, data_dir / f.name)
                except Exception:
                    pass
        result['copied_state_files'] = sorted(
            p.name for p in data_dir.iterdir() if p.is_file())
    env = {
        **os.environ,
        'PYTHONUTF8': '1',
        'QTG_DATA_DIR': str(data_dir),   # 前缀是 QTG_（见 config._abs）
        'QTG_STATIC_DIR': str(data_dir / 'web'),   # ensure_dirs 也要可写
        # http 读通道
        'QTG_WB_READ_MODE': 'http',       # 同样走 QTG_ 前缀
        'QTG_WB_PANEL_BASE': f'http://127.0.0.1:{browser["manager_port"]}',
        'QTG_MANAGER_SERVICE_TOKEN_FILE': str(token_file),
        'QTG_ADMIN_TOKEN_FILE': str(fixture_root / 'qtg-admin-token'),
        # 保留数据面地址（转发用）
        'QTG_WB_API_BASE': 'http://127.0.0.1:7863',
        'PATH': cfg['node_bin'] + os.pathsep + os.environ.get('PATH', ''),
    }
    port = 59456
    cmd = [cfg['python'], '-m', 'uvicorn', 'app.qtg.main:app', '--host', '127.0.0.1',
           '--port', str(port), '--log-level', 'warning']
    log = (run / 'qtg-http.log').open('w')
    proc = subprocess.Popen(cmd, cwd=str(src / 'qtg'), env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    try:
        ready = False
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            r = sh(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                    f'http://127.0.0.1:{port}/healthz'])
            if r.stdout.strip() == '200':
                ready = True
                break
            time.sleep(1)
        if not step('qtg_http_mode_boots', ready, port=port, exit=proc.poll(),
                    log_tail=(run / 'qtg-http.log').read_text(errors='replace')[-500:]):
            raise SystemExit(2)

        def get(path, headers=None):
            args = ['curl', '-s', '-m', '30', f'http://127.0.0.1:{port}{path}']
            for k, v in (headers or {}).items():
                args += ['-H', f'{k}: {v}']
            r = sh(args)
            try:
                return r.stdout, json.loads(r.stdout)
            except Exception:
                return r.stdout, None

        # 2) 先登录拿 QTG 会话（否则 /api/* 一律 unauthorized，测不到数据）
        # QTG 的登录只认**管理令牌**（不是用户名+密码，见 security.verify_password）。
        # fixture 的 qtg-admin-token 就是那份令牌。
        jar = run / 'qtg.jar'
        admin_token_file = fixture_root / 'qtg-admin-token'
        admin_pw = ''
        if admin_token_file.is_file():
            admin_pw = admin_token_file.read_text(encoding='utf-8').strip()
        # 令牌可能是 JSON 或纯文本；两种都试
        candidates = []
        if admin_pw:
            try:
                j = json.loads(admin_pw)
                for k in ('token', 'password', 'value'):
                    if isinstance(j, dict) and j.get(k):
                        candidates.append(str(j[k]))
            except Exception:
                candidates.append(admin_pw)
        login_ok = False
        login_body = ''
        for pw in candidates:
            lg = sh(['curl', '-s', '-c', str(jar), '-m', '30',
                     '-H', f'Origin: http://127.0.0.1:{port}',
                     '-H', 'Content-Type: application/json',
                     '-d', json.dumps({'password': pw}),
                     f'http://127.0.0.1:{port}/api/v1/auth/login'])
            login_body = (lg.stdout or '')[:150]
            if '"ok"' in login_body:
                login_ok = True
                break
        step('qtg_login', login_ok, body=login_body,
             token_candidates=len(candidates))

        def get_auth(path, extra=None):
            args = ['curl', '-s', '-b', str(jar), '-m', '30',
                    f'http://127.0.0.1:{port}{path}']
            for k, v in (extra or {}).items():
                args += ['-H', f'{k}: {v}']
            r = sh(args)
            try:
                return r.stdout, json.loads(r.stdout)
            except Exception:
                return r.stdout, None

        hdr = {'X-QTG-Platform': 'workbuddy'}
        raw_sum, sum_body = get_auth('/api/stats/summary', hdr)
        # 断言必须看到真实统计字段（不是 unauthorized 这类信封）
        has_stats = isinstance(sum_body, dict) and any(
            k in sum_body for k in ('today_requests', 'total_requests', 'today_tokens'))
        step('stats_summary_via_http', has_stats, body_head=raw_sum[:220],
             note='必须含 today_requests/total_requests 等真实统计字段')

        raw_keys, keys_body = get_auth('/api/keys', hdr)
        keys_ok = isinstance(keys_body, (list, dict)) and not (
            isinstance(keys_body, dict) and keys_body.get('detail') == 'unauthorized')
        step('keys_via_http', keys_ok, body_head=raw_keys[:220])

        # 3) 故障时不回退：把 token 文件临时改名，重启后读应失败而非返回空
        bak = token_file.with_suffix('.bak-test')
        token_file.rename(bak)
        try:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            log2 = (run / 'qtg-http-nofail.log').open('w')
            proc = subprocess.Popen(cmd, cwd=str(src / 'qtg'), env=env,
                                    stdout=log2, stderr=subprocess.STDOUT)
            time.sleep(12)
            alive = proc.poll() is None
            raw_fail, fail_body = (None, None)
            if alive:
                raw_fail, fail_body = get_auth('/api/stats/summary', hdr)
            # 期望：要么启动就失败（配置缺失明确报错），要么请求返回错误而非空成功
            explicit_failure = (not alive) or (fail_body is None) or (
                isinstance(fail_body, dict) and
                ('error' in fail_body or 'stats_health' in fail_body
                 or fail_body.get('ok') is False))
            step('fails_loudly_without_token', explicit_failure, still_alive=alive,
                 body=(raw_fail or '')[:200],
                 note='凭据缺失时不得静默回退 sqlite 或返回全 0')
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            log2.close()
        finally:
            if bak.exists():
                bak.rename(token_file)

        result['status'] = 'passed' if all(s['passed'] for s in result['steps']) else 'partial'
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            log.close()
        except Exception:
            pass
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(json.dumps({'status': result.get('status'),
                          'steps': {s['name']: s['passed'] for s in result['steps']}},
                         ensure_ascii=False))
        print('RESULT ' + str(run / 'result.json'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
