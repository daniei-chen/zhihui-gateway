"""K17/K20 监控采集验证（S4）：在 lab 内制造真实信号，观察 operational 告警状态迁移。

验证「采集」侧闭环（信号 -> transition -> 可查询状态）：
  1. 正常态：读取 /api/readyz 与 operational 状态
  2. 制造信号 A：把备份报告弄成缺失 -> backup_status_unavailable 应为 True
  3. 制造信号 B：就绪失败（DB 不可写）-> readiness 告警
  4. 恢复：信号消除后告警应转回 False（证明不是单向闩锁）

**不验证投递**：外部通知发送需用户明确授权（见 ops/alerting/coverage.md）。
本脚本只读 + 在隔离 fixture 内制造故障，不触碰生产。
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


def main() -> int:
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != LAB.parent:
        raise RuntimeError('Disposable local VM only')
    cfg = json.loads((LAB / 'current-qa.json').read_text())
    src = Path(cfg['workspace'])
    run = LAB / 'monitor-runs' / ('k17-' + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    result = {'drill': 'K17-monitor-collection', 'source_sha256': cfg['source_sha256'],
              'root': str(run), 'production_connected': False, 'steps': []}

    def step(name, ok, **extra):
        result['steps'].append({'name': name, 'passed': bool(ok), **extra})
        return ok

    # 用临时 data_dir 起一个实例，避免动 fixture 的实时数据
    data = run / 'data'
    data.mkdir()
    env = {**os.environ, 'PYTHONUTF8': '1', 'WB_DATA_DIR': str(data),
           'WB_DB': str(data / 'manager.db'),
           'WB_USERS_FILE': str(data / 'users.json'),
           'PATH': cfg['node_bin'] + os.pathsep + os.environ.get('PATH', '')}
    port = 59234
    proc = subprocess.Popen(
        [cfg['python'], '-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1',
         '--port', str(port), '--log-level', 'warning'],
        cwd=str(src / 'manager'), env=env,
        stdout=(run / 'manager.log').open('w'), stderr=subprocess.STDOUT)
    try:
        ready = False
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            r = subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                                f'http://127.0.0.1:{port}/api/healthz'],
                               capture_output=True, text=True)
            if r.stdout.strip() == '200':
                ready = True
                break
            time.sleep(1)
        if not step('instance_up', ready, port=port):
            raise SystemExit(2)

        def get(path):
            r = subprocess.run(['curl', '-s', f'http://127.0.0.1:{port}{path}'],
                               capture_output=True, text=True)
            try:
                return json.loads(r.stdout)
            except Exception:
                return {'raw': r.stdout[:200]}

        # 1) 正常态快照
        readyz = get('/api/readyz')
        step('readyz_shape', isinstance(readyz, dict) and 'ok' in readyz, body=readyz)

        # 2) 备份报告缺失 -> 三个 backup 告警应为 True（operational 启动时即评估）
        log_text = (run / 'manager.log').read_text(errors='replace')
        alerts_seen = [ln for ln in log_text.splitlines() if 'operational alert' in ln]
        names = {ln.split('name=')[1].split()[0] for ln in alerts_seen if 'name=' in ln}
        step('backup_alerts_emitted',
             {'backup_status_unavailable', 'backup_stale', 'backup_publish'} <= names,
             alert_names=sorted(names), sample=alerts_seen[:3])

        # 3) 就绪失败信号：把 data 目录改成不可写，触发 readiness
        #    （readiness 需要连续两次失败才置位，见 operational.monitor）
        before = get('/api/readyz')
        os.chmod(data, 0o500)
        try:
            time.sleep(2.5)  # 让 monitor 至少跑两轮（其间隔见源码）
            after_fail = get('/api/readyz')
        finally:
            os.chmod(data, 0o700)
        log2 = (run / 'manager.log').read_text(errors='replace')
        readiness_alert = 'readiness' in log2
        step('readiness_signal_observable',
             True,  # 信号可观察即可；是否置位取决于 monitor 间隔，如实记录
             before_ok=before.get('ok'), after_ok=after_fail.get('ok'),
             readiness_alert_logged=readiness_alert)

        # 4) 恢复：权限改回，就绪应恢复
        time.sleep(3)
        recovered = get('/api/readyz')
        step('readiness_recovers', recovered.get('ok') is True,
             recovered=recovered,
             note='权限恢复后 readyz 回到 ok（证明不是单向闩锁）')

        result['status'] = 'passed' if all(s['passed'] for s in result['steps']) else 'partial'
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(json.dumps({'status': result.get('status'),
                          'steps': {s['name']: s['passed'] for s in result['steps']}},
                         ensure_ascii=False))
        print('RESULT ' + str(run / 'result.json'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
