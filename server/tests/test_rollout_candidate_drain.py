"""D-04：停候选实例之前必须等在途请求跑完；排不空要显式失败。

## 缺陷（2026-11-28 审计 D-04，已验证 P0）

`deploy/rollout-rolling.sh` 在 `route_switch old` 之后只 `sleep 1` 就
`systemctl stop workbuddy-web-helper`。候选实例的 unit 当时是
`TimeoutStopSec=90` / `KillMode=control-group`
（`deploy/workbuddy-web-helper.service`），超过 90 秒的 SSE 必被 SIGKILL；
而脚本随后用 `request_outcomes` 统计窗口 5xx —— 流式截断通常不落 5xx，
于是故障被静默，发布照样记成功。

## 判据：候选实例自己报的在途计数

主信号是新加的只回环探针 `GET /api/health/inflight`（`server/main.py`，计数来自
`DrainMiddleware` 的请求级跟踪，流式响应的整个生命周期都在其中）。脚本切回旧实例
后轮询它，归零才停；墙钟（`WB_ROLLOUT_DRAIN_TIMEOUT`，默认 300 秒）只是安全上限。
停之后再用候选实例的优雅关闭日志兜一次底：`timeout graceful shutdown exceeded`
（`uvicorn/server.py:303`）与 `requests outstanding`（`server/main.py:333-336`）
都是「流被截断」的证据。

为什么不用 `ss` 的连接数或 `/api/system/drain`：前者被中继 httpx 连接池的空闲
keep-alive 污染（`server/rollingrelay.py:109-112`），后者要面板会话
（`server/routers/system.py:210-212`），root 的 shell 脚本没有凭据。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 计数归零之前不 stop | 在途流没跑完就停候选，回答被截断 |
| 计数读不到就失败 | 「读不到」被当成「排空了」 |
| 上限到了显式失败 | 卡住的流把发布挂死，或静默截断 |
| 截断日志/残留请求算失败 | 截断不落 5xx，静默通过 |
| `journalctl` 读不到算失败 | 证据缺失被当成干净退出 |
| 大日志里靠前的截断证据仍能被读到 | `printf | grep -q` 被 SIGPIPE 打断，证据被吃掉 |
| `Result != success` 算失败 | systemd SIGKILL 被当成正常停止 |
| 蓝绿路径把失败透传成退出码 2 + 失败记录 | 排不空也走完统计、记成功 |

测试用假的 `curl`/`systemctl`/`journalctl` 复现这条路径：不碰 systemd、nginx、
真实发布树，也不需要 root。其中的「长流」用真实时钟跑一个可调时长的后台进程，
默认 2 秒（`TEST_STREAM_SECONDS`），与 120 秒的同一条代码路径。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'deploy' / 'rollout-rolling.sh'
UNIT = ROOT / 'deploy' / 'workbuddy-web-helper.service'
GUARD = ROOT / 'ops' / 'writer-guard' / 'launch_guard.py'
SOURCE = SCRIPT.read_text(encoding='utf-8')

BASH = shutil.which('bash')

DRIVER = r'''
set -uo pipefail
# 只取函数定义：脚本底部的 BASH_SOURCE 守卫保证不会执行 main。
. "$ROLLOUT_SCRIPT"
HELPER=fake-helper
CANDIDATE_PORT=7865
DRAIN_TIMEOUT="${TEST_DRAIN_TIMEOUT:-5}"
POLLS="$PWD/polls"
: > "$POLLS"
SEQ="$PWD/sequence"
printf '%s\n' ${TEST_INFLIGHT:-0} > "$SEQ"
systemctl() {
    case "$*" in
        'stop fake-helper')
            printf 'stop zero_seen=%s\n' "$([ -f "$PWD/zero_seen" ] && echo yes || echo no)" \
                >> "$PWD/systemctl.log"
            return 0 ;;
        *' -p Result'*) printf '%s' "${TEST_RESULT:-success}"; return 0 ;;
        *) return 0 ;;
    esac
}
journalctl() {
    [ "${TEST_JOURNAL_FAIL:-0}" = 1 ] && return 1
    printf '%s\n' "${TEST_JOURNAL:-}"
}
curl() {
    printf '%s\n' "$*" >> "$PWD/curl.log"
    n=$(( $(cat "$POLLS" 2>/dev/null || echo 0) + 1 ))
    printf '%s\n' "$n" > "$POLLS"
    if [ "${TEST_PROBE_MISSING:-0}" = 1 ]; then
        printf '{"error":"loopback only"}'
        return 0
    fi
    value=$(sed -n "${n}p" "$SEQ")
    if [ -z "$value" ]; then
        value=$(tail -1 "$SEQ")
    fi
    if [ "$value" = 0 ]; then
        : > "$PWD/zero_seen"
    fi
    printf '{"service":"zhihui-gateway","total_in_flight":%s,"in_flight":{},"draining":false}' "$value"
}
sleep() { :; }
retire_candidate
printf 'RET=%s\n' "$?"
'''


def function(name: str) -> str:
    match = re.search(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}', SOURCE, re.M | re.S)
    if not match:
        raise AssertionError(f'缺少 shell 函数 {name}')
    return match.group()


class _DriverCase(unittest.TestCase):
    driver = DRIVER

    def run_driver(self, *, inflight='0', drain_timeout=5, result='success',
                   journal='', journal_fail=False, probe_missing=False,
                   env=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return self._run(tmp, inflight=inflight, drain_timeout=drain_timeout,
                         result=result, journal=journal, journal_fail=journal_fail,
                         probe_missing=probe_missing, env=env), Path(tmp)

    def _run(self, tmp, *, inflight, drain_timeout, result, journal, journal_fail,
             probe_missing, env):
        environment = dict(os.environ,
                           ROLLOUT_SCRIPT=str(SCRIPT).replace('\\', '/'),
                           TEST_INFLIGHT=' '.join(str(value) for value in inflight)
                           if isinstance(inflight, (list, tuple)) else str(inflight),
                           TEST_DRAIN_TIMEOUT=str(drain_timeout),
                           TEST_RESULT=result,
                           TEST_JOURNAL=journal,
                           TEST_JOURNAL_FAIL='1' if journal_fail else '0',
                           TEST_PROBE_MISSING='1' if probe_missing else '0',
                           **(env or {}))
        run = subprocess.run([BASH, '-c', self.driver], cwd=tmp, env=environment,
                             text=True, encoding='utf-8', errors='replace',
                             capture_output=True, timeout=180)
        code = re.search(r'RET=(\d+)', run.stdout)
        run.returncode_parsed = int(code.group(1)) if code else None
        return run


@unittest.skipUnless(BASH, 'bash is required')
class RetireCandidateTests(_DriverCase):
    """`retire_candidate`：停候选实例前的在途判据。"""

    def calls(self, tmp: Path, name: str):
        path = tmp / name
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    def test_it_stops_only_after_in_flight_reaches_zero(self):
        run, tmp = self.run_driver(inflight='5 3 1 0')
        self.assertEqual(run.returncode_parsed, 0, run.stdout + run.stderr)
        self.assertEqual(len(self.calls(tmp, 'curl.log')), 4,
                         '必须一直查到计数归零为止')
        self.assertIn('stop zero_seen=yes', self.calls(tmp, 'systemctl.log'),
                      '在途计数还没归零就停候选 = D-04 本身')
        self.assertIn('在途已归零', run.stdout)
        self.assertIn('/api/health/inflight', ' '.join(self.calls(tmp, 'curl.log')))

    def test_no_in_flight_stops_immediately(self):
        run, tmp = self.run_driver(inflight='0')
        self.assertEqual(run.returncode_parsed, 0, run.stdout + run.stderr)
        self.assertEqual(len(self.calls(tmp, 'curl.log')), 1)
        self.assertIn('stop zero_seen=yes', self.calls(tmp, 'systemctl.log'))

    def test_the_cap_is_an_explicit_failure(self):
        run, tmp = self.run_driver(inflight='4', drain_timeout=2)
        self.assertEqual(run.returncode_parsed, 1, run.stdout + run.stderr)
        self.assertIn('上限', run.stderr)
        self.assertIn('在途请求', run.stderr)
        self.assertEqual(self.calls(tmp, 'systemctl.log'), [],
                         '上限到了也不能在这里静默 stop —— 由调用方先记失败再收回端口')

    def test_a_missing_probe_is_a_failure(self):
        run, _ = self.run_driver(probe_missing=True)
        self.assertEqual(run.returncode_parsed, 1, run.stdout + run.stderr)
        self.assertIn('没有在途判据就不停它', run.stderr)

    def test_truncated_streams_after_stop_are_a_failure(self):
        run, _ = self.run_driver(
            inflight='1 0',
            journal='ERROR: Cancel 2 running task(s), timeout graceful shutdown exceeded')
        self.assertEqual(run.returncode_parsed, 1, run.stdout + run.stderr)
        self.assertIn('截断', run.stderr)

    def test_outstanding_requests_after_stop_are_a_failure(self):
        run, _ = self.run_driver(
            inflight='1 0',
            journal="ERROR: Shutdown reached cleanup with requests outstanding: {'inference_stream': 1}")
        self.assertEqual(run.returncode_parsed, 1, run.stdout + run.stderr)
        self.assertIn('在途请求', run.stderr)

    def test_journal_collection_failure_is_a_failure(self):
        run, _ = self.run_driver(inflight='1 0', journal_fail=True)
        self.assertEqual(run.returncode_parsed, 1, run.stdout + run.stderr)
        self.assertIn('无法证明没有流被截断', run.stderr)

    def test_a_large_journal_with_an_early_marker_is_still_detected(self):
        journal = ('ERROR: Cancel 1 running task(s), timeout graceful shutdown exceeded\n'
                   + ('x' * 300_000 + '\n') * 8)
        run, _ = self.run_driver(inflight='1 0', journal=journal)
        self.assertEqual(run.returncode_parsed, 1, run.stdout + run.stderr)
        self.assertIn('截断', run.stderr)

    def test_a_systemd_sigkill_is_a_failure(self):
        run, _ = self.run_driver(inflight='1 0', result='timeout')
        self.assertEqual(run.returncode_parsed, 1, run.stdout + run.stderr)
        self.assertIn('timeout', run.stderr)


@unittest.skipUnless(BASH, 'bash is required')
class ALongStreamKeepsRunningUntilItFinishesTests(unittest.TestCase):
    """真实时钟：在途流没结束就不停；结束了才停，发布算成功。

    时长可调（默认 2 秒）。120 秒跑的是同一条代码路径 —— 脚本没有任何时长假设，
    只有「计数 > 0 就继续等」。
    """

    def test_the_stream_finishes_before_the_candidate_is_stopped(self):
        seconds = int(os.environ.get('TEST_STREAM_SECONDS', '2'))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        driver = r'''
set -uo pipefail
. "$ROLLOUT_SCRIPT"
HELPER=fake-helper
CANDIDATE_PORT=7865
DRAIN_TIMEOUT=60
( sleep "$TEST_STREAM_SECONDS"; : > "$PWD/stream.done" ) &
systemctl() {
    case "$*" in
        'stop fake-helper')
            printf 'stop stream_done=%s\n' "$([ -f "$PWD/stream.done" ] && echo yes || echo no)" \
                >> "$PWD/systemctl.log"
            return 0 ;;
        *' -p Result'*) printf success ;;
        *) return 0 ;;
    esac
}
journalctl() { printf 'INFO: Waiting for connections to close. (CTRL+C to force quit)\n'; }
curl() {
    if [ -f "$PWD/stream.done" ]; then
        value=0
    else
        value=1
    fi
    printf '{"total_in_flight":%s,"in_flight":{}}' "$value"
}
started=$(date +%s)
retire_candidate
rc=$?
printf 'RET=%s ELAPSED=%s\n' "$rc" "$(( $(date +%s) - started ))"
'''
        run = subprocess.run([BASH, '-c', driver], cwd=tmp,
                             env=dict(os.environ,
                                      ROLLOUT_SCRIPT=str(SCRIPT).replace('\\', '/'),
                                      TEST_STREAM_SECONDS=str(seconds)),
                             text=True, encoding='utf-8', errors='replace',
                             capture_output=True, timeout=180)
        self.assertIn('RET=0', run.stdout, run.stdout + run.stderr)
        elapsed = int(re.search(r'ELAPSED=(\d+)', run.stdout).group(1))
        self.assertGreaterEqual(elapsed, seconds, '还没跑完就停了')
        calls = (Path(tmp) / 'systemctl.log').read_text(encoding='utf-8').splitlines()
        self.assertEqual(calls, ['stop stream_done=yes'],
                         'stop 必须发生在流结束之后，且只发生一次')


class DrainNumbersAreConsistentTests(unittest.TestCase):
    """脚本上限、unit 的优雅关闭窗口、systemd 的耐心必须是一个自洽的序列。"""

    def setUp(self):
        match = re.search(r'DRAIN_TIMEOUT="\$\{WB_ROLLOUT_DRAIN_TIMEOUT:-(\d+)\}"', SOURCE)
        self.assertIsNotNone(match, '等待上限必须来自 WB_ROLLOUT_DRAIN_TIMEOUT')
        self.default = int(match.group(1))
        stop = re.search(r'^CANDIDATE_STOP_TIMEOUT=(\d+)$', SOURCE, re.M)
        self.assertIsNotNone(stop, '缺少 CANDIDATE_STOP_TIMEOUT（unit 的 TimeoutStopSec）')
        self.stop_timeout = int(stop.group(1))
        self.unit = UNIT.read_text(encoding='utf-8')

    def unit_value(self, name):
        match = re.search(r'^Environment=' + name + r'=(\d+)$', self.unit, re.M)
        self.assertIsNotNone(match, f'unit 里没有 {name}')
        return int(match.group(1))

    def test_the_cap_is_under_the_systemd_patience(self):
        self.assertLess(self.default, self.stop_timeout,
                        '上限必须小于 TimeoutStopSec：否则等于在等 SIGKILL')
        self.assertIn(f'TimeoutStopSec={self.stop_timeout}', self.unit,
                      '脚本里的 CANDIDATE_STOP_TIMEOUT 必须与 unit 的 TimeoutStopSec 一致')

    def test_the_unit_lets_the_application_finish_before_systemd_gives_up(self):
        graceful = self.unit_value('WB_GRACEFUL_SHUTDOWN_S')
        drain = self.unit_value('WB_DRAIN_TIMEOUT_SEC')
        self.assertGreaterEqual(graceful, self.default,
                                '应用优雅关闭窗口必须 >= 脚本上限，否则 120 秒的流被应用自己截断')
        self.assertGreaterEqual(drain, self.default, 'DRAIN_TIMEOUT_SEC 同样不能小于脚本上限')
        self.assertLessEqual(graceful, self.stop_timeout,
                             '优雅关闭窗口超过 TimeoutStopSec 没有意义：systemd 先杀')

    def test_the_launcher_takes_the_graceful_window_from_the_environment(self):
        guard = GUARD.read_text(encoding='utf-8')
        body = guard[guard.index('def writer_command'):guard.index('class Launcher')]
        self.assertIn('WB_GRACEFUL_SHUTDOWN_S', body,
                      'launch_guard 写死 75 秒，unit 里的 WB_GRACEFUL_SHUTDOWN_S 不会生效')
        self.assertNotIn("'--timeout-graceful-shutdown', '75'", body)


class BlueGreenFailsWhenTheCandidateDoesNotDrainTests(unittest.TestCase):
    """排不空不能让发布「零 5xx 通过」。"""

    def test_a_failed_drain_returns_two_and_records_the_failure(self):
        source = r'''
set -uo pipefail
step() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
sleep() { :; }
seq() { printf 1; }
systemctl() { printf '%s\n' "$*" >> systemctl.log; }
prestart_candidate() { :; }
route_switch() { :; }
retire_candidate() { printf '在途未排空\n' >&2; return 1; }
db_scalar() {
    case "$1" in
        *'max(rowid)'*) printf 10;;
        *'http_status >= 500'*) printf 0;;
        *) printf 4;;
    esac
}
curl() { case "$*" in *'/api/release'*) printf '{"version":"1.6.27"}';; *) printf 200;; esac; }
DB=fake.db
GATEWAY=fake-gateway
HELPER=fake-helper
STARTED_AT=test
RECORDED=0
ROLLOUT_LOG=rollout.jsonl
''' + '\n'.join(function(name) for name in ('record_rollout', 'check_pages', 'rollout_blue_green')) \
            + '\nrollout_blue_green 1.6.27\nprintf "RET=%s\\n" "$?"\n'
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        run = subprocess.run([BASH, '-c', source], cwd=tmp, env=dict(os.environ),
                             text=True, encoding='utf-8', errors='replace',
                             capture_output=True, timeout=60)
        self.assertIn('RET=2', run.stdout, run.stdout + run.stderr)
        records = [line for line in (Path(tmp) / 'rollout.jsonl').read_text(
            encoding='utf-8').splitlines() if line.strip()]
        self.assertEqual(len(records), 1, records)
        self.assertIn('"exit":2', records[0])
        self.assertIn('在途未排空', records[0])
        # 失败记录必须先落盘，再收回 7865。
        self.assertIn('stop fake-helper',
                      (Path(tmp) / 'systemctl.log').read_text(encoding='utf-8'))


class TheOneSecondSleepIsGoneTests(unittest.TestCase):
    def test_the_candidate_is_retired_through_the_drain_gate(self):
        start = SOURCE.index('rollout_blue_green() {')
        body = SOURCE[start:SOURCE.index('\n}\n', start)]
        self.assertIn('route_switch old', body)
        self.assertIn('retire_candidate', body,
                      '切回旧实例之后必须经过排空判据才能停候选')
        self.assertGreater(body.index('retire_candidate'), body.index('route_switch old'),
                           '排空判据必须在切流之后')
        self.assertNotIn('route_switch old\n    sleep 1\n    systemctl stop "$HELPER"',
                         body,
                         '「切流后 sleep 1 就停候选」就是 D-04 本身')

    def test_the_failure_path_records_before_it_stops_the_unit(self):
        start = SOURCE.index('rollout_blue_green() {')
        body = SOURCE[start:SOURCE.index('\n}\n', start)]
        failure = body[body.index('retire_candidate'):]
        recorded = failure.index('record_rollout')
        stopped = failure.index('systemctl stop "$HELPER"')
        self.assertLess(recorded, stopped,
                        '先落失败记录再收回端口：顺序反了，回收卡住就一条记录都没有')
        self.assertIn("return 2", failure)


if __name__ == '__main__':
    unittest.main()
