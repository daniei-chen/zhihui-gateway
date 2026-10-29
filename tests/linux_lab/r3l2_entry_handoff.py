"""R3-L2 入口层版：经 Nginx 双 upstream 的准入切换实验（E2-3/E2-4）。

阶段 2（`r3l2_handoff.py`）证明的是**后端**交接（直连两个 Manager）。本脚本补
Lead 要求的**入口层**：客户端只经 Nginx 前端（`https://127.0.0.1:<host_port>`），
由 `tests/linux_lab/r3l2_env.py` 起「双 Manager + 双 upstream」，由
`tests/linux_lab/switch_upstream.py` 执行两种切换顺序。

## 观测口径（与 R3-L1 同一套，只是把「停止准入」换成入口层）

* `t_switch_start`：`switch_upstream.py` 发出 `t_switch_start` 事件的时刻
  （**入口切走起点**；Lead 要求用它替代 drain begin 应答时刻）；
* **入口层 T_admission** = `t_first_front_200 − t_switch_start`：
  `t_first_front_200` 是**经 nginx** 观测到的第一个 200（前端采样器打 `/api/readyz`）；
* 分段：`switch_start → drain_signal / nginx_reload / first_front_200 / 首条长流完成`
  —— 把「入口中断」「nginx reload」「新实例首个成功」「客户端流本身」分开；
* `admission_rejected`：切换窗口内**经前端**被拒（503/5xx/连接失败）的采样数；
* `N_cut`：**已准入**的长流（HTTP 200 且已收到帧）在切换期间是否收到终止信号
  （`[DONE]` / finish_reason / `message_stop` / `response.completed`）；
* `billing`：同一 DB 的四表对账（request_logs / request_outcomes /
  budget_reservations / usage_daily）。

## 两种顺序各跑一遍（分别报数）

* `drain-then-switch`：先 SIGUSR1 旧实例（readyz 503）→ 等真的 503 → nginx 收窄到新实例
  → reload → 前端恢复；
* `switch-then-drain`：先 reload 切到新实例 → 确认前端走新实例 → 再 SIGUSR1 旧实例。

## 运行（VM 内，`r3l2_env.py` 的 `up` 由本脚本管理）

    python3 tests/linux_lab/r3l2_entry_handoff.py --host-port 443 \
        --env-script /tmp/r3l2_env.py --switch-script /tmp/switch_upstream.py \
        --out evidence/r3-l2/entry

两模式跑完会 `--restore` 并 `down` 掉环境。产物：`entry-report.json`、
`entry-timeline.ndjson`、`entry-ledger-reconcile.json`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

MODES = ('drain-then-switch', 'switch-then-drain')


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def summarize_front_window(samples: list[dict], started: float, ended: float) -> dict:
    """Conservative probe envelopes on one monotonic clock, never a zero-outage claim."""
    samples = sorted(samples, key=lambda row: row['t_start'])
    rejected = [row for row in samples if row['status'] is not None and row['status'] != 200]
    errors = [row for row in samples if row.get('error')]
    abnormal = sorted(rejected + errors, key=lambda row: row['t_start'])
    invalid = []
    if len(samples) < 20:
        invalid.append('fewer_than_20_probes')
    if samples and len(abnormal) / len(samples) > .05:
        invalid.append('failure_fraction_above_5_percent')
    gaps = [(b['t_start'] - a['t_start']) * 1000 for a, b in zip(samples, samples[1:])]
    upper = None
    recovery = None
    measured = None
    if abnormal:
        last_bad = abnormal[-1]
        recovery = next((row for row in samples if row['status'] == 200
                         and row['t_start'] >= last_bad['t_end']), None)
        if recovery is None:
            invalid.append('no_recovery_after_last_failure')
        else:
            measured = round((recovery['t_end'] - abnormal[0]['t_start']) * 1000, 3)
    elif samples:
        coverage = [samples[0]['t_end'] - started, ended - samples[-1]['t_start']]
        coverage += [b['t_end'] - a['t_start'] for a, b in zip(samples, samples[1:])]
        upper = round(max(coverage) * 1000, 3)
    return {'rejected': rejected, 'errors': errors, 'abnormal': abnormal,
            'gaps': gaps, 'recovery': recovery, 'measured_ms': measured,
            'upper_bound_ms': upper, 'invalid_reasons': invalid,
            'definition': 'first observed failure to recovery after the last failure; not summed downtime'}


class Timeline:
    def __init__(self, path: Path, origin: float) -> None:
        self._fh = path.open('w', encoding='utf-8')
        self.origin = origin
        self._lock = threading.Lock()

    def rel_ms(self, moment: float | None = None) -> float:
        return round(((moment or time.monotonic()) - self.origin) * 1000, 3)

    def emit(self, event: str, **fields) -> None:
        with self._lock:
            self._fh.write(json.dumps({'event': event, 't_rel_ms': self.rel_ms(),
                                       't_mono_s': round(time.monotonic(), 6),
                                       **fields}, ensure_ascii=False) + '\n')
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class EntryExperiment:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.origin = time.monotonic()
        self.timeline = Timeline(self.out / 'entry-timeline.ndjson', self.origin)
        self.env_proc: subprocess.Popen | None = None
        self.state: dict = {}
        self.front_url = ''
        self.db_path = ''
        self.api_key = ''
        self.report: dict = {
            'schema': 'zhihui-r3-l2-entry/v1',
            'production_connected': False,
            'started_at': now_iso(),
            'metrics_definition': {
                'T_admission': 't_first_front_200 - t_switch_start（入口切走起点，单调时钟）',
                'admission_rejected': '切换窗口内经前端被拒（503/5xx/连接失败）的采样数',
                'N_cut': '已准入（200 且收到帧）的长流未收到终止信号的数量',
                'segments': 'switch_start→drain_signal→nginx_reload→first_front_200→首条流完成',
            },
            'modes': {},
        }

    # ── 环境（r3l2_env.py up / down）────────────────────────────────────
    def env_up(self) -> None:
        script = Path(self.args.env_script).resolve()
        command = [sys.executable, '-u', str(script), 'up', '--host-port', str(self.args.host_port)]
        if getattr(self.args, 'qa_file', None):
            command += ['--qa-file', str(self.args.qa_file)]
        self.env_proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        state_file = None
        assert self.env_proc.stdout is not None
        deadline = time.monotonic() + self.args.env_timeout
        while time.monotonic() < deadline:
            line = self.env_proc.stdout.readline()
            if not line:
                if self.env_proc.poll() is not None:
                    raise RuntimeError('r3l2_env.py 提前退出（见上方输出）')
                continue
            self.timeline.emit('env_stdout', line=line.strip()[:500])
            if 'R3L2_ENV_READY' not in line:
                continue
            payload = line.split('R3L2_ENV_READY', 1)[1].strip()
            # 首选解析：`R3L2_ENV_READY <整段 state JSON>`（r3l2_env.py 的实际格式）
            try:
                self.state = json.loads(payload)
            except ValueError:
                # 兼容：后面跟的是 env.json 路径
                for part in payload.split():
                    candidate = Path(part)
                    if candidate.suffix == '.json' and candidate.is_file():
                        state_file = candidate
                        self.state = json.loads(candidate.read_text(encoding='utf-8'))
                        break
            if self.state:
                break
        if not self.state:
            raise RuntimeError('没有解析到 R3L2_ENV_READY 的 state')
        self.front_url = self.state['host_url']
        self.db_path = str(Path(self.state['root']) / 'data' / 'manager.db')
        api_key_file = Path(self.state['api_key_file'])
        self.api_key = api_key_file.read_text(encoding='utf-8').strip()
        self.timeline.emit('environment_ready', front_url=self.front_url,
                           instances=self.state['instances'], db=self.db_path)

    def env_down(self) -> None:
        root = self.state.get('root', '')
        if root:
            script = Path(self.args.env_script).resolve()
            subprocess.run([sys.executable, str(script), 'down', '--root', str(root)],
                           capture_output=True, text=True, timeout=180)
        if self.env_proc is not None and self.env_proc.poll() is None:
            self.env_proc.terminate()
            try:
                self.env_proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.env_proc.kill()

    def restore(self, events_name: str) -> None:
        script = Path(self.args.switch_script).resolve()
        env_file = Path(self.state['root']) / 'env.json'
        events = self.out / events_name
        result = subprocess.run(
            [sys.executable, str(script), '--state', str(env_file), '--restore',
             '--events', str(events)],
            capture_output=True, text=True, timeout=300)
        self.timeline.emit('restore', returncode=result.returncode,
                           stderr=result.stderr[-500:])
        if result.returncode != 0:
            raise RuntimeError('restore 失败：' + (result.stderr or result.stdout)[-1000:])

    # ── HTTP（经前端；自签证书）───────────────────────────────────────
    def client(self, timeout: float = 60.0):
        import httpx
        return httpx.Client(base_url=self.front_url, verify=False, trust_env=False,
                            timeout=timeout)

    def front_readyz(self, client) -> int | None:
        try:
            return client.get('/api/readyz').status_code
        except Exception:  # noqa: BLE001
            return None

    def start_stream(self, protocol: str, tag: str,
                     progress: dict | None = None) -> dict:
        import httpx
        path = {'chat': '/v1/chat/completions', 'messages': '/v1/messages',
                'responses': '/v1/responses'}[protocol]
        body = ({'model': 'fake-model', 'messages': [{'role': 'user', 'content': 'entry'}],
                 'max_tokens': 32, 'stream': True}
                if protocol == 'chat' else
                {'model': 'fake-model', 'input': 'entry', 'max_output_tokens': 32,
                 'stream': True}
                if protocol == 'responses' else
                {'model': 'fake-model', 'max_tokens': 32, 'stream': True,
                 'messages': [{'role': 'user', 'content': 'entry'}]})
        record = {'tag': tag, 'protocol': protocol, 'status': None, 'frames': 0,
                  'terminals': [], 'saw_done': False, 'terminal': None,
                  't_start_ms': self.timeline.rel_ms(), 't_end_ms': None,
                  'request_id': None, 'error': None}
        try:
            with httpx.Client(base_url=self.front_url, verify=False, trust_env=False,
                              timeout=120.0) as client:
                with client.stream('POST', path,
                                   headers={'Authorization': 'Bearer ' + self.api_key},
                                   json=body) as response:
                    record['status'] = response.status_code
                    record['request_id'] = response.headers.get('x-request-id')
                    if response.status_code != 200:
                        text = b''
                        for chunk in response.iter_bytes():
                            text += chunk
                            if len(text) > 1500:
                                break
                        record['error'] = text.decode('utf-8', 'replace')[:300]
                    else:
                        for line in response.iter_lines():
                            stripped = line.strip()
                            if not stripped.startswith('data:'):
                                continue
                            payload = stripped[5:].strip()
                            if not payload:
                                continue
                            if payload == '[DONE]':
                                record['saw_done'] = True
                                record['terminals'].append('[DONE]')
                                break
                            record['frames'] += 1
                            if progress is not None:
                                progress['frames'] = record['frames']
                                progress['status'] = record['status']
                            try:
                                obj = json.loads(payload)
                            except ValueError:
                                continue
                            if not isinstance(obj, dict):
                                continue
                            if isinstance(obj.get('error'), dict):
                                record['terminals'].append('error')
                                continue
                            kind = obj.get('type')
                            if kind in ('response.completed', 'response.failed',
                                        'response.incomplete', 'message_stop'):
                                record['terminals'].append(kind)
                            if kind == 'message_delta':
                                delta = obj.get('delta')
                                if isinstance(delta, dict) and delta.get('stop_reason'):
                                    record['terminals'].append('stop_reason')
                            for choice in obj.get('choices') or []:
                                if isinstance(choice, dict) and choice.get('finish_reason'):
                                    record['terminals'].append('finish_reason')
        except Exception as exc:  # noqa: BLE001
            record['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
        record['t_end_ms'] = self.timeline.rel_ms()
        completed = (record['saw_done'] or 'message_stop' in record['terminals']
                     or 'response.completed' in record['terminals']
                     or 'finish_reason' in record['terminals']
                     or 'stop_reason' in record['terminals'])
        if record['status'] != 200:
            record['terminal'] = 'http_error'
        elif 'error' in record['terminals']:
            record['terminal'] = 'upstream_error'
        elif completed:
            record['terminal'] = 'completed'
        else:
            record['terminal'] = 'stream_cut'
        self.timeline.emit('stream_result', **record)
        return record

    def spawn_stream(self, protocol: str, tag: str) -> dict:
        progress: dict = {}
        holder: dict = {'progress': progress}
        thread = threading.Thread(
            target=lambda: holder.setdefault(
                'stream', self.start_stream(protocol, tag, progress=progress)),
            daemon=True)
        thread.start()
        holder['thread'] = thread
        return holder

    # ── 前端采样器（入口层 admission 探针）─────────────────────────────
    def start_sampler(self, interval_s: float) -> dict:
        stop = threading.Event()
        samples: list[dict] = []
        lock = threading.Lock()

        def run() -> None:
            import httpx
            client = httpx.Client(base_url=self.front_url, verify=False, trust_env=False,
                                  timeout=5.0)
            anchor = time.monotonic()
            index = 0
            previous = None
            try:
                while not stop.is_set():
                    target = anchor + index * interval_s
                    now = time.monotonic()
                    if target > now:
                        time.sleep(target - now)
                    elif previous is not None and (now - previous) < interval_s * 0.5:
                        anchor = now - index * interval_s
                    t_start = time.monotonic()
                    status = None
                    error = None
                    try:
                        status = client.get('/api/readyz').status_code
                    except Exception as exc:  # noqa: BLE001
                        error = type(exc).__name__ + ': ' + str(exc)[:120]
                    t_end = time.monotonic()
                    gap = None if previous is None else round((t_start - previous) * 1000, 3)
                    sample = {'seq': index, 't_start': t_start, 't_end': t_end,
                              'status': status, 'error': error, 'gap_ms': gap}
                    with lock:
                        samples.append(sample)
                    self.timeline.emit('front_probe', seq=index,
                                       t_start_ms=self.timeline.rel_ms(t_start),
                                       t_end_ms=self.timeline.rel_ms(t_end),
                                       status=status, error=error, gap_ms=gap)
                    previous = t_start
                    index += 1
            finally:
                client.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return {'stop': stop, 'thread': thread, 'samples': samples, 'lock': lock}

    def stop_sampler(self, sampler: dict) -> list[dict]:
        sampler['stop'].set()
        sampler['thread'].join(timeout=10)
        with sampler['lock']:
            return list(sampler['samples'])

    # ── 切换（调 switch_upstream.py）──────────────────────────────────
    def run_switch(self, mode: str, old_port: int, new_port: int,
                   events_name: str) -> tuple[dict, list[dict]]:
        script = Path(self.args.switch_script).resolve()
        env_file = Path(self.state['root']) / 'env.json'
        events = self.out / events_name
        started = time.monotonic()
        self.timeline.emit('switch_invoked', mode=mode, old_port=old_port,
                           new_port=new_port)
        proc = subprocess.run(
            [sys.executable, str(script), '--state', str(env_file), '--mode', mode,
             '--old-port', str(old_port), '--new-port', str(new_port),
             '--events', str(events)],
            capture_output=True, text=True, timeout=300)
        ended = time.monotonic()
        self.timeline.emit('switch_process_done', mode=mode, returncode=proc.returncode,
                           stdout_tail=proc.stdout[-500:], stderr_tail=proc.stderr[-500:])
        rows: list[dict] = []
        if events.is_file():
            for line in events.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return ({'returncode': proc.returncode, 'elapsed_s': round(ended - started, 3),
                 'stdout_tail': proc.stdout[-800:], 'stderr_tail': proc.stderr[-800:],
                 'events_file': str(events)}, rows)

    @staticmethod
    def event_elapsed(rows: list[dict], name: str) -> float | None:
        """switch_upstream.py 的阶段时间：`t_switch_done` 事件带完整 result。"""
        for row in rows:
            if row.get('event') == 't_switch_done':
                value = row.get(name)
                if value is not None:
                    return float(value)
        for row in rows:
            if row.get('event') == name and row.get('elapsed_s') is not None:
                return float(row['elapsed_s'])
        return None

    def run_mode(self, mode: str, old_port: int, new_port: int) -> dict:
        """一次入口层切换。

        并发约束：`r3l2_env.py` 的合成 key 带 `quota=1_000_000` → 预算层只允许
        **1 个 pending 预留**，三条流并发时后两条会 429（第一版实测踩到）。
        因此三协议 baseline 串行各跑一条（证明协议可用），切换窗口内只保留
        **1 条 chat 长流**（唯一的「已准入流」，N_cut 只对它判定）。
        """
        self.restore(f'restore-before-{mode}.ndjson')
        self.timeline.emit('mode_begin', mode=mode)
        baseline = {}
        for protocol in ('chat', 'messages', 'responses'):
            baseline[protocol] = self.start_stream(protocol, f'{mode}:baseline:{protocol}')
        self.timeline.emit('baseline_streams_done', mode=mode,
                           statuses={p: s.get('status') for p, s in baseline.items()})
        sampler = self.start_sampler(self.args.probe_interval_ms / 1000.0)
        holder = self.spawn_stream('chat', f'{mode}:in_flight:chat')
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            progress = holder.get('progress') or {}
            if progress.get('frames', 0) >= 1:
                break
            time.sleep(0.02)
        self.timeline.emit('in_flight_stream_accepted', mode=mode,
                           frames=(holder.get('stream') or {}).get('frames'))
        t_switch_invoked_ms = self.timeline.rel_ms()
        switch_info, rows = self.run_switch(mode, old_port, new_port,
                                            f'handoff-{mode}.ndjson')
        holder['thread'].join(timeout=180)
        in_flight = holder.get('stream', {})
        samples = self.stop_sampler(sampler)
        t_switch_done_ms = self.timeline.rel_ms()

        segments_switch = {
            'old_readyz_503_s': self.event_elapsed(rows, 't_old_readyz_503'),
            'nginx_reload_s': self.event_elapsed(rows, 't_switch_reload'),
            'front_ok_after_reload_s': self.event_elapsed(rows, 't_front_ok'),
            'first_front_200_s': self.event_elapsed(rows, 't_first_new_success'),
            'switch_script_total_s': switch_info['elapsed_s'],
        }
        # 入口层观测量：只统计 switch 调用之后、切换脚本结束之前的采样。
        # **所有比较都在本实验的同一时间基准上**（timeline.rel_ms，单调时钟）：
        # switch 脚本的事件只用于它自己那段 relative 时间，不跨进程相减 t_mono。
        window = [s for s in samples
                  if self.timeline.rel_ms(s['t_start']) >= t_switch_invoked_ms
                  and self.timeline.rel_ms(s['t_start']) <= t_switch_done_ms]
        measured = summarize_front_window(window,
            self.origin + t_switch_invoked_ms / 1000.0,
            self.origin + t_switch_done_ms / 1000.0)
        rejected, errors = measured['rejected'], measured['errors']
        abnormal, gaps = measured['abnormal'], measured['gaps']
        first_ok = measured['recovery']
        t_admission_front = measured['measured_ms']
        t_admission_upper_bound = measured['upper_bound_ms']
        invalid_reasons = measured['invalid_reasons']
        result = {
            'mode': mode,
            'switch_info': switch_info,
            'switch_self_reported': {
                'segments_s': segments_switch,
                'note': ('switch_upstream.py 自报：从它 emit t_switch_start 到'
                         '「前端首个 200」；这是**切换流程**耗时，不是用户中断时长'),
            },
            'switch_events': rows,
            't_switch_invoked_ms': t_switch_invoked_ms,
            't_switch_done_ms': t_switch_done_ms,
            'front_samples_total': len(samples),
            'front_samples_in_window': len(window),
            'front_sample_gap_median_ms': sorted(gaps)[len(gaps) // 2] if gaps else None,
            'front_sample_gap_max_ms': max(gaps) if gaps else None,
            'admission_metric_definition': measured['definition'],
            'front_rejected_samples': len(rejected),
            'front_rejected_statuses': sorted({s['status'] for s in rejected
                                               if s['status'] is not None}),
            'front_connection_errors': len(errors),
            'entry_observed_no_outage': not abnormal and bool(window),
            'front_error_details': [{'t_rel_ms': self.timeline.rel_ms(s['t_start']),
                                     'error': s.get('error')} for s in errors],
            'T_admission_front_ms': t_admission_front,
            'T_admission_front_upper_bound_ms': t_admission_upper_bound,
            'measurement_valid': not invalid_reasons,
            'invalid_reasons': invalid_reasons,
            'entry_verdict': ('measured' if abnormal and first_ok is not None
                              and not invalid_reasons
                              else 'observed_no_outage'
                              if not abnormal and bool(window) and not invalid_reasons
                              else '不足以判定'),
            'baseline_streams': {p: {k: s.get(k) for k in
                                     ('status', 'frames', 'terminal', 'request_id')}
                                 for p, s in baseline.items()},
            'in_flight_stream': {k: in_flight.get(k) for k in
                                 ('tag', 'protocol', 'status', 'frames', 'terminal',
                                  'request_id', 't_start_ms', 't_end_ms')},
            'in_flight_stream_done_after_switch_ms': (
                round(in_flight.get('t_end_ms', 0) - t_switch_invoked_ms, 3)
                if in_flight.get('t_end_ms') is not None else None),
            # 流必须在切换开始之后才结束，才算「切换期间的在途流」（否则它不是 N_cut 样本）
            'in_flight_stream_valid': (
                in_flight.get('t_end_ms') is not None
                and in_flight['t_end_ms'] - t_switch_invoked_ms > 0),
            'accepted_streams': sum(1 for s in list(baseline.values()) + [in_flight]
                                    if s.get('status') == 200),
            'stream_cuts': sum(1 for s in list(baseline.values()) + [in_flight]
                               if s.get('terminal') == 'stream_cut'),
            'stream_cut_details': [{'tag': s.get('tag'), 'protocol': s.get('protocol'),
                                    'frames': s.get('frames'),
                                    'request_id': s.get('request_id')}
                                   for s in list(baseline.values()) + [in_flight]
                                   if s.get('terminal') == 'stream_cut'],
        }
        result['ledger'] = self.reconcile()
        self.timeline.emit('mode_done', mode=mode,
                           switch_seconds=switch_info['elapsed_s'],
                           in_window_samples=len(window),
                           front_rejected=len(rejected), cuts=result['stream_cuts'],
                           entry_verdict=result['entry_verdict'])
        return result

    # ── 账本对账（口径与阶段 2 一致）───────────────────────────────────
    def reconcile(self) -> dict:
        conn = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            def count(sql: str, args: tuple = ()) -> int:
                row = conn.execute(sql, args).fetchone()
                return int(row[0] if row else 0)

            return {
                'request_logs_rows': count(
                    'SELECT COUNT(*) FROM request_logs WHERE request_id IS NOT NULL'),
                'duplicate_request_ids': count(
                    'SELECT COUNT(*) FROM (SELECT request_id FROM request_logs '
                    'WHERE request_id IS NOT NULL GROUP BY request_id HAVING COUNT(*) > 1)'),
                'inference_outcomes_rows': count(
                    "SELECT COUNT(*) FROM request_outcomes WHERE protocol != 'http'"),
                'expected_rejections_without_log': count(
                    "SELECT COUNT(*) FROM request_outcomes o WHERE o.protocol != 'http' "
                    'AND o.http_status IN (503, 429) AND NOT EXISTS ('
                    'SELECT 1 FROM request_logs l WHERE l.request_id = o.request_id)'),
                'unexpected_outcome_without_log': count(
                    "SELECT COUNT(*) FROM request_outcomes o WHERE o.protocol != 'http' "
                    'AND o.http_status NOT IN (503, 429) AND NOT EXISTS ('
                    'SELECT 1 FROM request_logs l WHERE l.request_id = o.request_id)'),
                'budget_reservations_rows': count('SELECT COUNT(*) FROM budget_reservations'),
                'settled_reservation_without_log': count(
                    'SELECT COUNT(*) FROM budget_reservations b WHERE NOT EXISTS ('
                    'SELECT 1 FROM request_logs l WHERE l.request_id = b.request_id) '
                    "AND b.state = 'settled'"),
                'open_or_unknown_reservations': count(
                    "SELECT COUNT(*) FROM budget_reservations "
                    "WHERE state IN ('unknown','reserved','forwarded')"),
                'request_logs_tokens': count(
                    'SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) '
                    'FROM request_logs WHERE status=200'),
                'usage_daily_tokens': count(
                    'SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage_daily'),
            }
        finally:
            conn.close()

    # ── 主流程 ───────────────────────────────────────────────────────
    def run(self) -> dict:
        self.env_up()
        try:
            instances = self.state['instances']
            old_port, new_port = int(instances[0]['port']), int(instances[1]['port'])
            self.report['instances'] = {'old': old_port, 'new': new_port,
                                        'front_url': self.front_url,
                                        'source_sha256': self.state.get('source_sha256')}
            for mode in (self.args.modes or MODES):
                self.report['modes'][mode] = self.run_mode(mode, old_port, new_port)
            self.report['verdict'] = self._verdict()
        finally:
            try:
                self.env_down()
            except Exception:  # noqa: BLE001
                pass
            self.report['finished_at'] = now_iso()
            self.report['timeline'] = str(self.out / 'entry-timeline.ndjson')
            self.timeline.close()
            (self.out / 'entry-report.json').write_text(
                json.dumps(self.report, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
            (self.out / 'entry-ledger-reconcile.json').write_text(
                json.dumps({mode: payload.get('ledger')
                            for mode, payload in self.report['modes'].items()},
                           ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return self.report

    def _verdict(self) -> dict:
        summary = {}
        for mode, payload in self.report['modes'].items():
            ledger = payload.get('ledger') or {}
            summary[mode] = {
                'switch_succeeded': payload['switch_info']['returncode'] == 0,
                'all_protocols_completed': (
                    all(item.get('status') == 200 and item.get('terminal') == 'completed'
                        for item in payload['baseline_streams'].values())
                    and len(payload['baseline_streams']) == 3
                    and payload['in_flight_stream'].get('status') == 200
                    and payload['in_flight_stream'].get('terminal') == 'completed'),
                'entry_verdict': payload.get('entry_verdict'),
                'measurement_valid': payload.get('measurement_valid'),
                'invalid_reasons': payload.get('invalid_reasons'),
                'T_admission_front_ms': payload.get('T_admission_front_ms'),
                'T_admission_front_upper_bound_ms': payload.get(
                    'T_admission_front_upper_bound_ms'),
                'switch_self_reported_first_front_200_s': payload['switch_self_reported'][
                    'segments_s'].get('first_front_200_s'),
                'switch_total_s': payload['switch_info']['elapsed_s'],
                'front_samples_in_window': payload['front_samples_in_window'],
                'front_sample_gap_median_ms': payload['front_sample_gap_median_ms'],
                'front_rejected_samples': payload['front_rejected_samples'],
                'front_connection_errors': payload['front_connection_errors'],
                'accepted_streams': payload['accepted_streams'],
                'stream_cuts': payload['stream_cuts'],
                'in_flight_stream_done_after_switch_ms': payload.get(
                    'in_flight_stream_done_after_switch_ms'),
                'billing_ok': (ledger.get('duplicate_request_ids') == 0
                               and ledger.get('unexpected_outcome_without_log') == 0
                               and ledger.get('settled_reservation_without_log') == 0),
            }
        # 合理性检查必须进 all_ok：负数/0 样本/超窗口/无在途流样本一律算「测量无效」，
        # 不能因为「流没截断 + 账本对」就放行（那是 K37 同类错误的翻版）。
        all_ok = bool(summary) and all(item['stream_cuts'] == 0 and item['billing_ok']
                     and item['switch_succeeded'] and item['all_protocols_completed']
                     and item['accepted_streams'] > 0
                     and item['measurement_valid']
                     and item['entry_verdict'] in ('observed_no_outage', 'measured')
                     for item in summary.values())
        return {'modes': summary, 'all_ok': all_ok}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default='evidence/r3-l2/entry')
    parser.add_argument('--host-port', type=int, default=443)
    parser.add_argument('--env-script', default='/tmp/r3l2_env.py')
    parser.add_argument('--switch-script', default='/tmp/switch_upstream.py')
    parser.add_argument('--probe-interval-ms', type=int, default=20)
    parser.add_argument('--env-timeout', type=float, default=420)
    parser.add_argument('--qa-file', default=None)
    parser.add_argument('--modes', nargs='*', default=None)
    args = parser.parse_args(argv)
    experiment = EntryExperiment(args)
    report = experiment.run()
    print(json.dumps(report.get('verdict'), ensure_ascii=False, indent=2))
    return 0 if report.get('verdict', {}).get('all_ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
