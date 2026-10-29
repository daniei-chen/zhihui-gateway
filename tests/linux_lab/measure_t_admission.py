"""S3 T_admission drill（R3-L1 口径修正版）：真实 Nginx + 两代构建 + 固定节拍采样。

## R3-L1 修掉的三处口径缺陷（旧版，见 evidence/r3-inventory/gaps.json G2-1）

1. **sleep-after-request**：旧采样器 `fetch()` 完成后才 `sleep(interval)`，每拍实际
   起点间隔 = 请求耗时 + interval（实测 0.25–0.40s，而报告写的是 20ms）。
   现在用**下一拍绝对时刻**推进：第 N 拍目标起点 = `anchor + N*interval`，请求完成后
   睡到下一目标时刻；请求耗时不进入节拍误差。每一拍记录 `gap_ms`（真实起点间隔）
   与 `duration_ms`（请求耗时），漂移可复算。
2. **单点失败算 0 秒**：旧版「最长连续非 200 跨度」对只命中一个失败样本的窗口返回
   0s —— 失败被当成「没有中断」。现在给出**区间**：
   `T_admission = 首个成功响应完成 - 最后成功响应完成`（跨过失败窗口），
   并同时给保守下界/上界、样本数、采样分辨率；单点失败时明确标注
   「单样本，真实中断上界 = 失败样本 t_end - 前一成功样本 t_end，分辨率 ±interval」。
3. **只测静态页**：本脚本仍只测 web 发布切换（`GET /`），这是它的边界；
   **推理准入与已准入流截断由 `measure_admission_matrix.py` 用真实 SSE 长流观测**。
   因此本脚本的失败计数改名为 `admission_failures`（单一明确口径），不再叫 N_cut,
   避免与「已准入流被截断」混用。

## 判定口径（与 measure_admission_matrix.py 共用）

* 样本数 n < 20 或 意外失败样本 / n > 5% → `verdict = 不足以判定`，不给毫秒结论；
* `T_admission_ms` 只在该窗口有「失败样本」且两侧都有成功样本时给出；
  全程无失败时给 `observed_no_outage`（附 n 与分辨率），不写 0 秒当成精确值；
* 所有结论都能由 `timeline.ndjson` 复算（每行一个采样或一个发布事件）。

仅限一次性本地 VM；不触碰生产。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import uuid

#: 与 measure_admission_matrix.py 一致的判定门槛
MIN_SAMPLES = 20
MAX_UNEXPECTED_FAILURE_RATIO = 0.05


class FixedCadenceSampler(threading.Thread):
    """固定节拍采样器：单调时钟记录每请求 t_start/t_end，不 sleep-after-request。"""

    def __init__(self, interval_s: float, fetch, timeline, origin: float) -> None:
        super().__init__(daemon=True)
        self.interval = interval_s
        self.fetch = fetch
        self.timeline = timeline
        self.origin = origin
        self.stop_event = threading.Event()
        self.samples: list[dict] = []
        self._lock = threading.Lock()

    def run(self) -> None:
        anchor = time.monotonic()
        index = 0
        previous_start = None
        while not self.stop_event.wait(0):
            target = anchor + index * self.interval
            now = time.monotonic()
            if target > now:
                time.sleep(target - now)
            elif previous_start is not None and (now - previous_start) < self.interval * 0.5:
                # 落后不足半拍：重新对齐，避免连发造成虚假样本密度
                anchor = now - index * self.interval
            t_start = time.monotonic()
            code, duration = self.fetch('/')
            t_end = time.monotonic()
            gap = None if previous_start is None else round((t_start - previous_start) * 1000, 3)
            sample = {'seq': index, 't_start': t_start, 't_end': t_end,
                      'status': code, 'duration_ms': round((t_end - t_start) * 1000, 3),
                      'gap_ms': gap}
            with self._lock:
                self.samples.append(sample)
            self.timeline['emit']({
                'event': 'admission_probe', 'seq': index,
                't_start_ms': round((t_start - self.origin) * 1000, 3),
                't_end_ms': round((t_end - self.origin) * 1000, 3),
                'status': code, 'gap_ms': gap, 'duration_ms': sample['duration_ms'],
            })
            previous_start = t_start
            index += 1

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.samples)


def analyse_window(samples: list[dict], origin: float, interval_s: float) -> dict:
    """按 R3-L1 口径算 T_admission（区间 + 样本 + 分辨率 + 不足判定）。"""
    def rel(moment: float) -> float:
        return round((moment - origin) * 1000, 3)

    gaps = [s['gap_ms'] for s in samples if s.get('gap_ms') is not None]
    failures = [s for s in samples if s['status'] != '200']

    def expected_failure(status: str) -> bool:
        """预期内的「不可用」：5xx，或 curl 连接失败留下的 '000'/空串。"""
        text = str(status).strip()
        return text == '000' or text == '' or text.startswith('5')

    # 意外失败 = 既不是 200，也不是 5xx/连接失败（例如 401/403/404）。
    # **200 必须排除在外**：第一版写成 `not startswith('5') and != '000'`，
    # 把全部 200 样本算成「意外失败」，于是「切换全程可用」被判定成
    # 「不足以判定」（VM 复跑实测：357/357 unexpected、failure_samples=0）。
    unexpected = [s for s in samples
                  if s['status'] != '200' and not expected_failure(s['status'])]
    # 失败窗口：首个失败之前的最后一个成功 → 最后一个失败之后的第一个成功
    first_failure_index = next((i for i, s in enumerate(samples) if s['status'] != '200'), None)
    last_failure_index = next((i for i in range(len(samples) - 1, -1, -1)
                               if samples[i]['status'] != '200'), None)
    window: dict = {
        'samples': len(samples),
        'failure_samples': len(failures),
        'unexpected_failure_samples': len(unexpected),
        'resolution_ms': round(interval_s * 1000, 3),
        'sample_gap_median_ms': sorted(gaps)[len(gaps) // 2] if gaps else None,
        'sample_gap_max_ms': max(gaps) if gaps else None,
        'first_failure_ms': (rel(failures[0]['t_start']) if failures else None),
        'last_failure_ms': (rel(failures[-1]['t_end']) if failures else None),
    }
    reasons: list[str] = []
    if len(samples) < MIN_SAMPLES:
        reasons.append(f'样本数 {len(samples)} < {MIN_SAMPLES}')
    if samples and (len(unexpected) / len(samples)) > MAX_UNEXPECTED_FAILURE_RATIO:
        reasons.append(f'意外失败样本 {len(unexpected)}/{len(samples)} 超过 '
                       f'{int(MAX_UNEXPECTED_FAILURE_RATIO * 100)}%')
    if failures and first_failure_index == 0:
        reasons.append('失败窗口之前没有成功样本（无法定义「最后成功」）')
    if failures and last_failure_index == len(samples) - 1:
        reasons.append('失败窗口之后没有成功样本（未见恢复）')

    if not failures:
        window['T_admission_ms'] = None
        window['observed_outage'] = False
        if reasons:
            window['verdict'] = '不足以判定'
            window['insufficient_reasons'] = reasons
        else:
            window['verdict'] = 'observed_no_outage'
            window['note'] = (f'采样 {len(samples)} 点、分辨率 {window["resolution_ms"]}ms，'
                              '未观测到非 200；这不是「0 秒精确值」，而是「该分辨率下无中断」')
        return window

    if reasons:
        window['T_admission_ms'] = None
        window['observed_outage'] = True
        window['verdict'] = '不足以判定'
        window['insufficient_reasons'] = reasons
        return window

    last_ok_before = samples[first_failure_index - 1]
    first_ok_after = samples[last_failure_index + 1]
    t_last_ok = rel(last_ok_before['t_end'])
    t_first_ok = rel(first_ok_after['t_end'])
    window.update({
        'observed_outage': True,
        't_last_success_before_ms': t_last_ok,
        't_first_success_after_ms': t_first_ok,
        'T_admission_ms': round(t_first_ok - t_last_ok, 3),
        'verdict': 'measured',
    })
    if len(failures) == 1:
        single = failures[0]
        window['single_failure_note'] = (
            '仅 1 个失败样本：T_admission 是「最后一次成功 → 第一次成功」的区间，'
            f'真实中断上界落在 [{rel(single["t_start"])}, {rel(single["t_end"])}] 内，'
            f'分辨率 ±{window["resolution_ms"]}ms（旧版对此给出 0s，是错的）')
        window['real_outage_upper_bound_ms'] = round(
            rel(single['t_end']) - rel(last_ok_before['t_end']), 3)
    return window


def main() -> int:
    lab = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent:
        raise RuntimeError('Disposable local VM only')
    ap = argparse.ArgumentParser()
    ap.add_argument('--script', required=True)
    ap.add_argument('--old-export', required=True)
    ap.add_argument('--probe-interval-ms', type=int, default=20)
    ap.add_argument('--out', default='', help='输出目录（默认在 run root 下）')
    args = ap.parse_args()
    script = Path(args.script).resolve()
    old_export = Path(args.old_export).resolve()
    if not script.is_file():
        raise ValueError('publisher missing')
    if not old_export.is_relative_to(lab / 'snapshots') or not (old_export / 'index.html').is_file():
        raise ValueError('old export must be a real frozen export')
    cfg = json.loads((lab / 'current-qa.json').read_text())
    source = Path(cfg['workspace'])
    new_export = source / 'src/three-platform/web/out'
    assert (new_export / 'index.html').is_file()

    root = lab / 'http-release-runs' / ('adm-' + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o755)
    out_dir = Path(args.out).resolve() if args.out else root
    out_dir.mkdir(parents=True, exist_ok=True)
    timeline_path = out_dir / 'timeline.ndjson'
    origin = time.monotonic()
    timeline_handle = timeline_path.open('w', encoding='utf-8')
    timeline_lock = threading.Lock()

    def emit(event: dict) -> None:
        event = {'event': event.pop('event'), 't_mono_s': round(time.monotonic(), 6),
                 **event}
        with timeline_lock:
            timeline_handle.write(json.dumps(event, ensure_ascii=False) + '\n')
            timeline_handle.flush()

    site = 'zhihui.lab'
    cert, key = root / 'tls.crt', root / 'tls.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                    'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=' + site,
                    '-addext', 'subjectAltName=DNS:' + site, '-keyout', str(key), '-out', str(cert)],
                   check=True, capture_output=True)
    PORT = 18443 + 1000  # 高位端口，避开 fixture 占用的 443/18443
    (root / 'nginx.conf').write_text(f'''user gateway-lab;
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 256; }}
http {{
    include /etc/nginx/mime.types;
    access_log {root}/access.log;
    server {{
        listen 127.0.0.1:{PORT} ssl;
        server_name {site};
        ssl_certificate {cert};
        ssl_certificate_key {key};
        root {root}/current;
        location / {{ try_files $uri $uri/ =404; }}
    }}
}}
''')
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', PORT)) == 0:
            raise RuntimeError(f'Port {PORT} busy; leave the existing listener alone')
    nginx = ['sudo', '-n', '/usr/sbin/nginx', '-p', str(root), '-c', str(root / 'nginx.conf')]
    result = {'drill': 'T_admission', 'host': socket.gethostname(),
              'source_sha256': cfg['source_sha256'], 'root': str(root),
              'out_dir': str(out_dir), 'production_connected': False,
              'probe_interval_ms': args.probe_interval_ms,
              'metric_definition': (
                  'T_admission = 首个成功响应完成时刻 - 跨过失败窗口的最后一次成功响应完成时刻'
                  '（单调时钟；分辨率 = 采样间隔）'),
              'thresholds': {'min_samples': MIN_SAMPLES,
                             'max_unexpected_failure_ratio': MAX_UNEXPECTED_FAILURE_RATIO},
              'started_at': dt.datetime.now(dt.timezone.utc).isoformat()}

    def pages_of(out: Path):
        return sorted({'/' if p.parent == out else '/' + p.parent.relative_to(out).as_posix() + '/'
                       for p in out.rglob('index.html') if '404' not in p.relative_to(out).parts})

    def env_path():
        return os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')

    def publish(out: Path, name: str):
        env = {**os.environ, 'OUT_DIR': str(out), 'REL_ROOT': str(root / 'releases'),
               'LINK': str(root / 'current'), 'SITE': site,
               'LOG': str(root / f'release-{name}.log'), 'CURL_CA_BUNDLE': str(cert),
               'KEEP_VERSIONS': '2', 'HTTP_CHECK': '1', 'LINK_MODE': 'switch',
               'SITE_PORT': str(PORT),
               'ZH_RELEASE_LOCK_FILE': str(root / 'release.lock'),
               'ZH_RELEASE_STATE_FILE': str(root / 'release-state.json'),
               'PAGES': ' '.join(pages_of(out)),
               'PATH': cfg['node_bin'] + os.pathsep + env_path()}
        for k in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            env[k] = ''
        env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost,' + site
        wrapper = source / 'ops/release-lock/zh-release-lock.sh'
        t0 = time.monotonic()
        proc = subprocess.run(['bash', str(wrapper), 'run', 'adm-' + root.name + '-' + name, 'web',
                               '--', 'bash', str(script)], env=env, cwd=source,
                              capture_output=True, timeout=900)
        record = {'rc': proc.returncode, 'elapsed_s': round(time.monotonic() - t0, 3),
                  'stdout_tail': proc.stdout.decode('utf-8', 'replace')[-1500:],
                  'stderr_tail': proc.stderr.decode('utf-8', 'replace')[-800:]}
        emit({'event': 'publish', 'name': name, 'rc': record['rc'],
              'elapsed_s': record['elapsed_s']})
        return record

    def fetch(path='/'):
        r = subprocess.run(['curl', '-s', '-o', '/dev/null', '-m', '5',
                            '-w', '%{http_code}|%{time_total}',
                            '--cacert', str(cert), '--resolve', f'{site}:{PORT}:127.0.0.1',
                            f'https://{site}:{PORT}{path}'], capture_output=True, text=True)
        code, _, t = r.stdout.partition('|')
        return code.strip(), float(t or 0)

    try:
        subprocess.run([*nginx, '-t'], check=True, capture_output=True)
        subprocess.run(nginx, check=True, capture_output=True)
        time.sleep(0.6)

        res_old = publish(old_export, 'old')
        result['publish_old'] = res_old
        if res_old['rc'] != 0:
            raise AssertionError('seed publish failed')
        before_code, _ = fetch('/')
        before_txt = subprocess.run(['curl', '-s', '--cacert', str(cert), '--resolve',
                                     f'{site}:{PORT}:127.0.0.1', f'https://{site}:{PORT}/'],
                                    capture_output=True, text=True).stdout
        result['before'] = {'code': before_code, 'html_len': len(before_txt)}

        sampler = FixedCadenceSampler(args.probe_interval_ms / 1000.0, fetch,
                                      {'emit': emit}, origin)
        sampler.start()
        time.sleep(0.5)
        pre_samples = len(sampler.snapshot())
        emit({'event': 'switch_begin', 't_rel_ms': round((time.monotonic() - origin) * 1000, 3)})
        res_new = publish(new_export, 'new')
        result['publish_new'] = res_new
        time.sleep(1.2)
        sampler.stop_event.set()
        sampler.join(timeout=5)
        samples = sampler.snapshot()
        tail = samples[pre_samples:]
        result['window'] = analyse_window(tail, origin, args.probe_interval_ms / 1000.0)
        after_code, _ = fetch('/')
        after_txt = subprocess.run(['curl', '-s', '--cacert', str(cert), '--resolve',
                                    f'{site}:{PORT}:127.0.0.1', f'https://{site}:{PORT}/'],
                                   capture_output=True, text=True).stdout
        changed = after_txt != before_txt
        result.update({
            'samples_total': len(samples),
            'samples_in_switch_window': len(tail),
            'after': {'code': after_code, 'content_changed': changed,
                      'html_len': len(after_txt)},
            'admission_failures': result['window']['failure_samples'],
            'timeline': str(timeline_path),
        })
        verdict = result['window']['verdict']
        if verdict == 'measured':
            verdict = 'ADMISSION_CUT'
        elif verdict == 'observed_no_outage':
            verdict = 'PASS' if changed and after_code == '200' else 'CONTENT_NOT_SWITCHED'
        else:
            verdict = 'INSUFFICIENT_EVIDENCE'
        result['verdict'] = verdict
        result['status'] = verdict
        emit({'event': 'verdict', 'verdict': verdict,
              'T_admission_ms': result['window'].get('T_admission_ms')})
        (out_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1) + '\n',
                                             encoding='utf-8')
        print(json.dumps({k: result[k] for k in
                          ('verdict', 'samples_total', 'samples_in_switch_window')}, ensure_ascii=False))
        print(json.dumps(result['window'], ensure_ascii=False, indent=2))
        print('RESULT ' + str(out_dir / 'result.json'))
        return 0 if verdict in ('PASS', 'ADMISSION_CUT') else 1
    finally:
        try:
            subprocess.run([*nginx, '-s', 'stop'], capture_output=True, timeout=20)
        except Exception:
            pass
        result.setdefault('finished_at', dt.datetime.now(dt.timezone.utc).isoformat())
        (out_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1) + '\n',
                                             encoding='utf-8')
        timeline_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
