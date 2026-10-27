"""R3-L2：lab nginx 双 upstream 的切换控制器（本地 QEMU lab 专用）。

两种顺序，全部输出单调时钟 ndjson 事件，供 R3-L2 复算 T_admission / N_cut：

  * ``drain-then-switch``：先给旧 Manager 发 SIGUSR1（drain 开始，readyz 返回 503）
    并等到真的 503，再把 nginx upstream 收窄到新实例并 reload；
  * ``switch-then-drain``：先 reload 把流量切到新实例，再给旧实例发 SIGUSR1 并等 readyz 503。

控制面只做三件事：
  1. 改写 nginx.conf 里 ``# >>> manager_upstream`` 与 ``# <<< manager_upstream``
     之间的 upstream 段（这是 r3l2_env.py 生成的配置，标记由生成器写入）；
  2. ``sudo -n nginx -s reload`` 让变更生效（fixture/实验 nginx 用独立 prefix，绝不动系统 nginx）；
  3. 直接探测实例 loopback 的 ``/api/readyz``，以及经 nginx 前端的探针请求。

用法（在 VM 上）::

    python tests/linux_lab/switch_upstream.py --state <env.json> \
        --mode drain-then-switch --old-port <P1> --new-port <P2> \
        --events <handoff.ndjson>
    python tests/linux_lab/switch_upstream.py --state <env.json> --restore --events <restore.ndjson>

状态文件由 ``r3l2_env.py up`` 写出（含 nginx.conf/root/instances/stop_file）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MARK_BEGIN = '# >>> manager_upstream'
MARK_END = '# <<< manager_upstream'


class Timeline:
    """单调时钟事件流（ndjson 一行一个事件）。"""

    def __init__(self, path: Path | None):
        self.started = time.monotonic()
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('', encoding='utf-8')

    def emit(self, event: str, **detail) -> dict:
        row = {'t_mono': round(time.monotonic() - self.started, 6),
               't_wall': round(time.time(), 3), 'event': event, **detail}
        line = json.dumps(row, ensure_ascii=False)
        print(line, flush=True)
        if self.path is not None:
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write(line + '\n')
        return row


def load_state(path: Path) -> dict:
    state = json.loads(path.read_text(encoding='utf-8'))
    if not state.get('production_connected') is False:
        raise SystemExit('refusing a non-local experiment state')
    return state


def instance_by_port(state: dict, port: int) -> dict:
    for item in state['instances']:
        if int(item['port']) == int(port):
            return item
    raise SystemExit(f'port {port} is not part of this experiment')


def nginx_command(state: dict, *extra: str) -> list[str]:
    return ['sudo', '-n', '/usr/sbin/nginx', '-p', state['root'],
            '-c', state['nginx_conf'], *extra]


def render_upstream(ports: list[int]) -> str:
    servers = ''.join(f'\n    server 127.0.0.1:{port} max_fails=1 fail_timeout=2s;' for port in ports)
    return f'{MARK_BEGIN}\nupstream manager_backend {{{servers}\n}}\n{MARK_END}'


def set_upstream_ports(state: dict, ports: list[int]) -> None:
    conf = Path(state['nginx_conf'])
    text = conf.read_text(encoding='utf-8')
    begin = text.find(MARK_BEGIN)
    end = text.find(MARK_END)
    if begin < 0 or end < begin:
        raise SystemExit('nginx.conf lacks the manager_upstream markers')
    new_text = text[:begin] + render_upstream(ports) + text[end + len(MARK_END):]
    tmp = conf.with_suffix('.conf.tmp')
    tmp.write_text(new_text, encoding='utf-8')
    os.replace(tmp, conf)


def reload_nginx(state: dict) -> None:
    result = subprocess.run(nginx_command(state, '-t'), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit('nginx -t failed: ' + (result.stderr or result.stdout)[-2000:])
    result = subprocess.run(nginx_command(state, '-s', 'reload'), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit('nginx reload failed: ' + (result.stderr or result.stdout)[-2000:])


def http_readyz(port: int, timeout: float = 3.0) -> int | None:
    """Loopback 直连某个 Manager 实例的 /api/readyz；连不上返回 None。"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/readyz', timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def wait_readyz(port: int, want: set[int], timeout: float, poll: float) -> float | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = http_readyz(port)
        if status in want:
            return time.monotonic()
        time.sleep(poll)
    return None


def signal_drain(pid: int) -> None:
    os.kill(pid, signal.SIGUSR1)  # serve.py drain.begin()


def signal_resume(pid: int) -> None:
    os.kill(pid, signal.SIGUSR2)  # serve.py drain.resume()


def front_url(state: dict, path: str) -> str:
    return f"https://127.0.0.1:{state['host_port']}{path}"


def front_probe(state: dict, path: str = '/api/readyz', timeout: float = 10.0) -> dict:
    """经 nginx 前端的探针（自签证书 → 不校验）。"""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    started = time.monotonic()
    try:
        with urllib.request.urlopen(front_url(state, path), timeout=timeout, context=context) as resp:
            body = resp.read(200)
            return {'status': resp.status, 'elapsed_s': round(time.monotonic() - started, 4),
                    'body_head': body[:120].decode('utf-8', 'replace')}
    except urllib.error.HTTPError as exc:
        return {'status': exc.code, 'elapsed_s': round(time.monotonic() - started, 4),
                'body_head': exc.read(200).decode('utf-8', 'replace')}
    except Exception as exc:  # noqa: BLE001
        return {'status': None, 'elapsed_s': round(time.monotonic() - started, 4), 'error': repr(exc)[:200]}


def wait_front_ok(state: dict, timeout: float, poll: float) -> float | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = front_probe(state)
        if probe.get('status') == 200:
            return time.monotonic()
        time.sleep(poll)
    return None


def switch(state: dict, timeline: Timeline, mode: str, old_port: int, new_port: int,
           timeout: float, poll: float, keep_old_in_pool: bool = False) -> dict:
    """Require B readiness before mutation; restore the prior entry on failure."""
    old = instance_by_port(state, old_port)
    instance_by_port(state, new_port)
    if mode not in ('drain-then-switch', 'switch-then-drain') or old_port == new_port:
        raise SystemExit('invalid switch mode or identical old/new instance')
    ready = wait_readyz(new_port, {200}, timeout, poll)
    timeline.emit('new_ready_preflight', new_port=new_port, ready=ready is not None)
    if ready is None:
        raise SystemExit('candidate not ready; old instance and entry left unchanged')
    conf = Path(state['nginx_conf'])
    original = conf.read_bytes()
    try:
        return _perform_switch(state, timeline, mode, old_port, new_port,
                               timeout, poll, keep_old_in_pool)
    except BaseException:
        timeline.emit('rollback_start', old_port=old_port)
        errors = []
        try:
            signal_resume(int(old['pid']))
        except Exception as exc:
            errors.append('resume:' + type(exc).__name__)
        try:
            temporary = conf.with_suffix('.rollback.tmp')
            temporary.write_bytes(original)
            os.replace(temporary, conf)
            reload_nginx(state)
        except BaseException as exc:
            errors.append('entry_restore:' + type(exc).__name__)
        try:
            old_ok = wait_readyz(old_port, {200}, timeout, poll) is not None
            front_ok = wait_front_ok(state, timeout, poll) is not None
            if not old_ok:
                errors.append('old_not_ready')
            if not front_ok:
                errors.append('entry_not_ready')
        except Exception as exc:
            errors.append('rollback_probe:' + type(exc).__name__)
        timeline.emit('rollback_done', ok=not errors, errors=errors)
        raise


def _perform_switch(state: dict, timeline: Timeline, mode: str, old_port: int, new_port: int,
           timeout: float, poll: float, keep_old_in_pool: bool = False) -> dict:
    """执行一次切换；返回含关键时间戳与布尔结果的字典。"""
    old = instance_by_port(state, old_port)
    new = instance_by_port(state, new_port)
    if mode not in ('drain-then-switch', 'switch-then-drain'):
        raise SystemExit(f'unknown mode {mode!r}')
    active_ports = [item['port'] for item in state['instances']] if keep_old_in_pool else [new_port]
    result = {'mode': mode, 'old_port': old_port, 'new_port': new_port}
    timeline.emit('t_switch_start', mode=mode, old_port=old_port, new_port=new_port,
                  new_readyz=http_readyz(new_port), old_readyz=http_readyz(old_port))
    started = time.monotonic()
    if mode == 'drain-then-switch':
        signal_drain(int(old['pid']))
        timeline.emit('t_drain_signal', instance=old['port'], pid=old['pid'])
        when = wait_readyz(old_port, {503}, timeout, poll)
        result['old_readyz_503'] = when is not None
        timeline.emit('t_old_readyz_503' if when is not None else 't_old_readyz_503_timeout',
                      elapsed_s=round(time.monotonic() - started, 3))
        if when is None:
            raise SystemExit('old instance never reported readyz 503 after SIGUSR1')
        set_upstream_ports(state, active_ports)
        reload_nginx(state)
        result['t_switch_reload'] = round(time.monotonic() - started, 3)
        timeline.emit('t_switch_reload', active_ports=active_ports)
    else:
        set_upstream_ports(state, active_ports)
        reload_nginx(state)
        result['t_switch_reload'] = round(time.monotonic() - started, 3)
        timeline.emit('t_switch_reload', active_ports=active_ports)
        # 确认前端已走新实例后再 drain 旧实例
        when = wait_front_ok(state, timeout, poll)
        result['front_ok_after_reload'] = when is not None
        timeline.emit('t_front_ok' if when is not None else 't_front_ok_timeout',
                      elapsed_s=round(time.monotonic() - started, 3))
        if when is None:
            raise SystemExit('entry not ready after reload; refusing to drain old instance')
        signal_drain(int(old['pid']))
        timeline.emit('t_drain_signal', instance=old['port'], pid=old['pid'])
        when = wait_readyz(old_port, {503}, timeout, poll)
        result['old_readyz_503'] = when is not None
        timeline.emit('t_old_readyz_503' if when is not None else 't_old_readyz_503_timeout',
                      elapsed_s=round(time.monotonic() - started, 3))
    # 首条成功请求（经 nginx，只认 200）
    first_ok = wait_front_ok(state, timeout, poll)
    if first_ok is None:
        raise SystemExit('entry did not recover after switch')
    result['front_ready_after_switch'] = first_ok is not None
    result['t_first_new_success'] = round(time.monotonic() - started, 3) if first_ok else None
    timeline.emit('t_first_new_success' if first_ok else 't_first_new_success_timeout',
                  elapsed_s=result['t_first_new_success'])
    result['old_direct_readyz'] = http_readyz(old_port)
    result['new_direct_readyz'] = http_readyz(new_port)
    result['t_admission_s'] = round(time.monotonic() - started, 3)
    timeline.emit('t_switch_done', **result)
    return result


def restore(state: dict, timeline: Timeline, timeout: float, poll: float,
            resume_draining: bool = True) -> dict:
    """把两个实例都放回 upstream，并 resume 正在 drain 的实例。"""
    ports = [item['port'] for item in state['instances']]
    timeline.emit('t_restore_start', ports=ports)
    started = time.monotonic()
    set_upstream_ports(state, ports)
    reload_nginx(state)
    for item in state['instances']:
        try:
            signal_resume(int(item['pid']))
        except ProcessLookupError:
            continue
    ready = {}
    for item in state['instances']:
        ready[item['port']] = wait_readyz(item['port'], {200}, timeout, poll) is not None
    timeline.emit('t_restore_done', ready=ready, elapsed_s=round(time.monotonic() - started, 3))
    return {'ready': ready, 'elapsed_s': round(time.monotonic() - started, 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state', required=True)
    parser.add_argument('--mode', choices=['drain-then-switch', 'switch-then-drain'])
    parser.add_argument('--old-port', type=int)
    parser.add_argument('--new-port', type=int)
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--timeout', type=float, default=60.0)
    parser.add_argument('--poll', type=float, default=0.2)
    parser.add_argument('--events', default=None, help='ndjson timeline path')
    parser.add_argument('--keep-old-in-pool', action='store_true',
                        help='switch 时只调整顺序/保留旧实例（默认摘除旧实例）')
    args = parser.parse_args(argv)
    if socket.gethostname() != 'zhihui-local-lab':
        raise SystemExit('This controller is restricted to the disposable local VM')
    state = load_state(Path(args.state))
    timeline = Timeline(Path(args.events) if args.events else None)
    if args.restore:
        result = restore(state, timeline, args.timeout, args.poll)
    else:
        if args.mode is None or args.old_port is None or args.new_port is None:
            parser.error('--mode/--old-port/--new-port are required unless --restore')
        result = switch(state, timeline, args.mode, args.old_port, args.new_port,
                        args.timeout, args.poll, args.keep_old_in_pool)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
