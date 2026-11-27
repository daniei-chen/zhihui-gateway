"""D-04：停候选实例之前必须等在途请求跑完；排不空要显式失败。

## 缺陷（2026-11-28 审计 D-04，已验证 P0）

`deploy/rollout-rolling.sh` 在 `route_switch old` 之后只 `sleep 1` 就
`systemctl stop workbuddy-web-helper`。候选实例的 unit 是
`TimeoutStopSec=90` / `KillMode=control-group`
（`deploy/workbuddy-web-helper.service`），超过 90 秒的 SSE 必被 SIGKILL；
而脚本随后用 `request_outcomes` 统计窗口 5xx —— 流式截断通常不落 5xx，
于是故障被静默，发布照样记成功。

## 判据为什么是「进程退出 + 优雅关闭日志」

在途请求没有别的可靠来源：

* `request_outcomes` 只有终态，被截断的流大多落 200 或干脆没有终态；
* 端口 7865 上的 ESTABLISHED 连接数会被中继的连接池污染 ——
  `server/rollingrelay.py:109-112` 给 httpx 配了 `max_keepalive_connections`，
  空闲的 keep-alive 连接看起来和正在跑的流一样；
* `/api/system/drain` 确实有在途计数（`server/drain.py:50-54`），但只对面板
  读者开放（`server/routers/system.py:210-212`），root 的 shell 脚本没有会话凭据。

服务自己的优雅停止是唯一无凭据、可核对的信号：SIGTERM 之后 uvicorn 停止接新
连接、等在途响应写完（`ops/writer-guard/launch_guard.py:229-231` 实际传
`--timeout-graceful-shutdown 75`），进程退出即说明在途已归零；超过 75 秒它会打
`Cancel N running task(s), timeout graceful shutdown exceeded`
（`uvicorn/server.py:303`）并取消任务 —— 那一条就是「流被截断」的证据。
应用侧的 lifespan 还会打 `Shutdown reached cleanup with requests outstanding`
（`server/main.py:333-336`）。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 等候选实例真的退出再报成功 | 在途流还在跑就宣布发版成功 |
| 等待有上限 | 一个卡住的流把发布挂死 |
| 超时是显式失败 | 流被截断，发布仍记成功 |
| 优雅关闭超时/残留请求算失败 | 截断不落 5xx，静默通过 |
| systemd 的 SIGKILL 算失败 | `TimeoutStopSec` 杀流被当成正常停止 |
| 蓝绿路径把失败透传成退出码 2 | 排不空也走完统计、记成功 |

测试用假的 `systemctl`/`journalctl` 复现这条路径：不碰 systemd、nginx、
真实发布树，也不需要 root。
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
systemctl() {
    printf '%s\n' "$*" >> "$PWD/systemctl.log"
    case "$*" in
        'stop --no-block fake-helper') return 0 ;;
        *ActiveState*)
            n=$(( $(cat "$POLLS" 2>/dev/null || echo 0) + 1 ))
            printf '%s\n' "$n" > "$POLLS"
            if [ "$n" -le "${TEST_ACTIVE_POLLS:-0}" ]; then
                printf 'deactivating'
            else
                printf 'inactive'
            fi
            return 0 ;;
        *' -p Result'*) printf '%s' "${TEST_RESULT:-success}"; return 0 ;;
        *) return 0 ;;
    esac
}
journalctl() { printf '%s\n' "${TEST_JOURNAL:-}"; }
sleep() { :; }
retire_candidate
printf 'RET=%s\n' "$?"
'''


def function(name: str) -> str:
    match = re.search(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}', SOURCE, re.M | re.S)
    if not match:
        raise AssertionError(f'缺少 shell 函数 {name}')
    return match.group()


@unittest.skipUnless(BASH, 'bash is required')
class RetireCandidateTests(unittest.TestCase):
    """`retire_candidate`：停候选实例前的在途判据。"""

    def run_driver(self, *, active_polls=0, drain_timeout=5, result='success', journal=''):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ,
                   ROLLOUT_SCRIPT=str(SCRIPT).replace('\\', '/'),
                   TEST_ACTIVE_POLLS=str(active_polls),
                   TEST_DRAIN_TIMEOUT=str(drain_timeout),
                   TEST_RESULT=result,
                   TEST_JOURNAL=journal)
        run = subprocess.run([BASH, '-c', DRIVER], cwd=tmp, env=env, text=True,
                             encoding='utf-8', errors='replace',
                             capture_output=True, timeout=60)
        log = Path(tmp) / 'systemctl.log'
        calls = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
        code = re.search(r'RET=(\d+)', run.stdout)
        return run, calls, int(code.group(1)) if code else None

    def test_it_waits_for_the_candidate_to_actually_exit(self):
        run, calls, code = self.run_driver(active_polls=6, drain_timeout=10)
        self.assertEqual(code, 0, run.stdout + run.stderr)
        polls = [call for call in calls if 'ActiveState' in call]
        self.assertEqual(len(polls), 7,
                         '必须一直查到进程真的退出为止（deactivating 不是退出）')
        self.assertIn('stop --no-block fake-helper', calls,
                      '必须发起停止，但不能在没等到退出时就报成功')
        self.assertIn('排空', run.stdout)

    def test_no_in_flight_requests_exits_immediately(self):
        run, calls, code = self.run_driver(active_polls=0)
        self.assertEqual(code, 0, run.stdout + run.stderr)
        self.assertEqual(len([c for c in calls if 'ActiveState' in c]), 1)
        self.assertIn('无在途请求', run.stdout)

    def test_a_candidate_that_never_drains_is_an_explicit_failure(self):
        run, _, code = self.run_driver(active_polls=999, drain_timeout=2)
        self.assertEqual(code, 1, run.stdout + run.stderr)
        self.assertIn('上限', run.stderr)
        self.assertIn('排空', run.stderr)

    def test_truncated_streams_are_an_explicit_failure(self):
        run, _, code = self.run_driver(
            active_polls=1,
            journal='ERROR: Cancel 2 running task(s), timeout graceful shutdown exceeded')
        self.assertEqual(code, 1, run.stdout + run.stderr)
        self.assertIn('截断', run.stderr)

    def test_outstanding_requests_at_cleanup_are_a_failure(self):
        run, _, code = self.run_driver(
            active_polls=1,
            journal='ERROR: Shutdown reached cleanup with requests outstanding: {\'inference_stream\': 1}')
        self.assertEqual(code, 1, run.stdout + run.stderr)
        self.assertIn('在途请求', run.stderr)

    def test_a_systemd_sigkill_is_a_failure(self):
        run, _, code = self.run_driver(active_polls=1, result='timeout')
        self.assertEqual(code, 1, run.stdout + run.stderr)
        self.assertIn('timeout', run.stderr)

    def test_clean_shutdown_with_streams_reports_the_evidence(self):
        run, _, code = self.run_driver(
            active_polls=2,
            journal='INFO: Waiting for connections to close. (CTRL+C to force quit)\n'
                    'INFO: Application shutdown complete.')
        self.assertEqual(code, 0, run.stdout + run.stderr)
        self.assertIn('等在途响应写完', run.stdout)


class DrainTimeoutIsBoundedTests(unittest.TestCase):
    def test_the_bound_is_configurable_and_below_the_unit_timeout(self):
        self.assertIn('WB_ROLLOUT_DRAIN_TIMEOUT', SOURCE,
                      '等待上限必须可配置，否则只能改脚本')
        match = re.search(r'DRAIN_TIMEOUT="\$\{WB_ROLLOUT_DRAIN_TIMEOUT:-(\d+)\}"', SOURCE)
        self.assertIsNotNone(match, '等待上限必须来自 WB_ROLLOUT_DRAIN_TIMEOUT')
        default = int(match.group(1))
        self.assertLess(default, 90,
                        '上限必须小于 unit 的 TimeoutStopSec=90：'
                        '留出余量才能在 systemd SIGKILL 之前自己判定「排不空」')

    def test_an_out_of_range_bound_is_refused(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, ROLLOUT_SCRIPT=str(SCRIPT).replace('\\', '/'))
        run = subprocess.run(
            [BASH, '-c', '. "$ROLLOUT_SCRIPT"\nHELPER=fake-helper\nDRAIN_TIMEOUT=600\n'
                        'systemctl() { return 0; }\nretire_candidate; printf "RET=%s\\n" "$?"'],
            cwd=tmp, env=env, text=True, encoding='utf-8', errors='replace',
            capture_output=True, timeout=60)
        self.assertIn('RET=1', run.stdout)
        self.assertIn('TimeoutStopSec', run.stderr)


class BlueGreenFailsWhenTheCandidateDoesNotDrainTests(unittest.TestCase):
    """排不空不能让发布「零 5xx 通过」。"""

    def test_a_failed_drain_returns_two_and_records_the_failure(self):
        source = r'''
set -uo pipefail
step() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
sleep() { :; }
seq() { printf 1; }
systemctl() { :; }
prestart_candidate() { :; }
route_switch() { :; }
retire_candidate() { printf '排空失败\n' >&2; return 1; }
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
        self.assertIn('排空', records[0])


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

    def test_the_failure_path_stops_the_unit_but_never_claims_success(self):
        start = SOURCE.index('rollout_blue_green() {')
        body = SOURCE[start:SOURCE.index('\n}\n', start)]
        failure = body[body.index('retire_candidate'):]
        self.assertIn('systemctl stop "$HELPER"', failure,
                      '排空失败也要把 7865 收回来，否则下次发布没有候选实例可用')
        self.assertIn("return 2", failure)


if __name__ == '__main__':
    unittest.main()
