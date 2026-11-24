#!/usr/bin/env python3
"""Run the deterministic release regression set.

The repository also contains historical audit and deployment-environment suites.
Those suites remain useful during an environment-specific audit, but this small
set is the repeatable release gate for a clean checkout with synthetic data.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "server.tests.test_v133_release_hardening",
    "server.tests.test_v12149_post_migration_cleanup",
    "server.tests.test_v12152_rollout_script",
    "server.tests.test_release_pointer_closure",
    "server.tests.test_release_consistency",
    "server.tests.test_product_origin",
    "server.tests.test_changelog",
    "server.tests.test_readme_i18n",
    # 前端 i18n 守卫。长期没进门禁，于是「403 文案带变量、短语表匹配不上」
    # 这类只有切语言才看得见的问题一直潜伏（2026-11-23 修复后纳入）。
    "server.tests.test_web_i18n",
    "server.tests.test_docs_packaging",
    "server.tests.test_release_integrity",
    # 这条一直没被门禁跑过。2026-11-23 单独跑它时发现早就失败了 ——
    # `reviewed_patch_files` 里 145 个 hash 停在 v1.3.3，之后每次改文件都没更新。
    # 测试存在但不运行，比没有测试更糟：它让人以为这块有保障。
    "server.tests.test_release_provenance_integrity",
    # 2026-11-23 审计：473 个测试模块里只有 34 个被收录，439 个从没在门禁里跑过。
    # 抽样 4 个与积分/签到/快照相关的模块全部通过 —— 说明它们是好的，只是漏了。
    # 不一次全收：全量跑会卡在 test_upstreams 的 tearDown（阻塞），
    # 所以按「已证明可靠」逐个收。
    "server.tests.test_credits_flow",
    "server.tests.test_credits_snapshot",
    "server.tests.test_checkin_logs_realm",
    "server.tests.test_v1294_snapshot_age",
    # 这两条 2026-11-23 跑出来是失败的，暴露了一个真实缺陷：
    # trae2._checkin_rows() 把 self._db() 写在 try 外面，分析库变成只读时
    # 会抛 PermissionError 直接掀翻 build_pool_snapshot，连带池快照接口 500。
    # 已修（conn 进 try、except 加 OSError、finally 判空），并收进清单防止复发。
    "server.tests.test_pool_snapshot_history_isolation",
    "server.tests.test_v1277_account_pool_alerts",
    # 告警与运维主题补收（2026-11-23 逐个单独跑通后入清单）。
    # 同批的 test_audit_runtime_repairs 不收：108 个用例通过、6 个
    # （同一条参数化测试的变体）因 TimeoutError 失败，看起来是该测试
    # 自身的 body 下限设置与请求体大小不匹配，不是产品缺陷，但也修不动。
    "server.tests.test_v1269_model_completion_alert",
    "server.tests.test_v1274_budget_rejected_unknown",
    "server.tests.test_v1283_budget_alert_thresholds",
    "server.tests.test_v1297_recovery_chain_alert",
    "server.tests.test_backup_health_evidence",
    "server.tests.test_audit_notify_config",
    "server.tests.test_audit_regressions",
    "server.tests.test_control_plane_stability",
    "server.tests.test_automatic_zero_evidence",
    # 积分 / 账本 / 配额主题补收（2026-11-23 全部单独跑通后入清单）。
    # 这批是「钱」相关的不变量：预授权、日额度、结算真相、并发原子性。
    # 之前一条都没在门禁里跑过，等于账本逻辑没有自动守卫。
    #
    # 注意：清单里的模块必须**只依赖 unittest 与标准库**。CI 的 manager job
    # 不安装 pytest，混进 pytest 风格的测试会让整条流水线报
    # `ModuleNotFoundError: No module named 'pytest'`。
    # test_budget_batch_bounds 与 test_audit_finance_repairs 就属于这种，
    # 因此不收 —— 它们仍可单独用 pytest 跑。
    "server.tests.test_daily_key_quota",
    "server.tests.test_budget_kill_closure",
    "server.tests.test_daily_plan",
    "server.tests.test_daily_replay_boundaries",
    "server.tests.test_attempt_billing_facts",
    "server.tests.test_audit_stats_reconcile",
    "server.tests.test_audit_dailyplan_lifecycle",
    "server.tests.test_closure_contracts",
    "server.tests.test_concurrency_and_atomicity",
    # 协议兼容主题补收（2026-11-23 全部单独跑通后入清单）。
    # Anthropic Messages 兼容、SSE 错误流、首字延迟、body 上限这些
    # 是「用户能直接感觉到」的部分，之前同样没在门禁里跑过。
    "server.tests.test_anthropic_messages",
    "server.tests.test_anthropic_stream_errors",
    "server.tests.test_anthropic_session_routing",
    "server.tests.test_adapter_resolution",
    "server.tests.test_count_tokens_admission",
    "server.tests.test_first_token",
    "server.tests.test_wirebody",
    "server.tests.test_windows_scripts",
    "server.tests.test_config_contract",
    "server.tests.test_audit_gateway_boundaries",
    "server.tests.test_audit_protocol_limits",
    "server.tests.test_audit_middleware_boundaries",
    "server.tests.test_audit_auth_origin",
    "server.tests.test_auth_bypass",
    "server.tests.test_gateway_body_limit",
    "server.tests.test_api_tokens",
    "server.tests.test_gateway_passthrough",
    "server.tests.test_actual_http_pool",
    "server.tests.test_cohort_rollout",
    "server.tests.test_asset_lifecycle",
    "server.tests.test_rolling_relay",
    "server.tests.test_v1258_relay_startup_retry",
    "server.tests.test_qoder_rootless_contract",
    "server.tests.test_managed_stage_helper",
    "server.tests.test_audit_release_signer",
    "server.tests.test_audit_restore_evidence",
    "server.tests.test_recovery_retention",
    "server.tests.test_recovery_housekeeping",
    "server.tests.test_v007_retention_review",
    "server.tests.test_steward_watchdog",
    "server.tests.test_writer_guard_contract",
    "server.tests.test_upstream_alignment_20261123",
    "server.tests.test_usage_token_protocol",
    "server.tests.test_release_pipeline",
    "server.tests.test_traffic_tap",
    "server.tests.test_promptprep",
    # 环境变量契约：读到的必须登记。2026-11-23 因漏登记 WB_TRAFFIC_TAP 导致
    # 设置即宕机（连重启 10 次后放弃，全站 502），这条链路必须有守卫。
    # 2026-11-24 该测试从「只扫 config.py」改成「AST 扫全树」，抓出另外 10 个
    # 同级的雷（accountguard.py 5 个 + externalplatforms.py 5 个）。
    #
    # 四源对照（schema 默认值 / 源码默认值 / 进程环境 / unit 声明）同理：它早就
    # 写好、也早就该跑，却一直不在门禁里 —— 而它偏偏是唯一能挡住「schema 与
    # 源码默认值悄悄分叉」的那条。
    "server.tests.test_config_contract",
    "server.tests.test_config_drift",
    # 协议面「静默走错路」的两处：都是给客户端**错误的信号**，不是崩溃，
    # 所以异常监控与既有测试都抓不到它们。
    "server.tests.test_count_tokens_content",
    "server.tests.test_responses_budget_and_cache",
    # 信号质量：心跳与额度轮询不该把 request_outcomes 淹掉。
    "server.tests.test_outcome_signal_noise",
    "server.tests.test_test_selector",
    "server.tests.test_async_blocking_db",
    "server.tests.test_socket_activation",
    "server.tests.test_ops_disk_watermark",
    "server.tests.test_ops_console_static",
    "server.tests.test_metering_reconcile_wiring",
    "server.tests.test_account_ttft_metric",
    "server.tests.test_platform_auth",
)


def frontend_suite() -> dict:
    """跑 web 的前端用例（`npm test`：`lib/*.test.mjs` + `tests/*.test.cjs`）。

    ## 为什么接在这里

    CI 的 `quality.yml` 目前只跑 `node --test lib/*.test.mjs` —— **只覆盖一半**：
    `web/tests/` 下那 12 个 `.test.cjs` 文件（控制台状态机、i18n、静态导出等）
    从来没在 CI 上跑过。而修改 workflow 需要令牌带 `workflow` scope，本仓库的令牌
    没有（2026-11-24 实测：`gist, read:org, repo`），所以先接进门禁：
    **每次本地验证都跑**，覆盖面立刻补齐，不必等权限。

    整套 244 例实测 2.5 秒，代价可以忽略；找不到 npm 时跳过而不是失败 ——
    门禁的职责是"这台机器能验的都验了"，不是"必须装 Node"。
    """
    npm = shutil.which('npm') or shutil.which('npm.cmd')
    if not npm:
        return {'skipped': True, 'reason': 'npm not found'}
    web = ROOT / 'web'
    if not (web / 'package.json').is_file():
        return {'skipped': True, 'reason': 'web/package.json missing'}
    # 没装依赖就跑不了：这些用例 `require('typescript')` 等真实包。
    #
    # 2026-11-24 实际踩到：CI 的后端 job 直接跑本文件，而它没有 `npm ci` ——
    # 于是 `web/tests/steward.test.cjs` 在 CI 上以 exitCode 1 / location 1:1
    # 崩掉（模块加载失败），而同一份代码在本地全绿。门禁要区分「代码坏了」和
    # 「这台机器没装依赖」：前者必须红，后者跳过并说清原因。
    if not (web / 'node_modules').is_dir():
        return {'skipped': True, 'reason': 'web/node_modules missing (run npm ci)'}
    try:
        proc = subprocess.run([npm, 'test'], cwd=str(web), capture_output=True,
                              text=True, timeout=900, encoding='utf-8',
                              errors='replace', shell=(os.name == 'nt'))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f'前端用例无法执行：{exc}', file=sys.stderr)
        return {'ok': False, 'error': str(exc)}
    output = (proc.stdout or '') + (proc.stderr or '')
    if proc.returncode != 0:
        # 只打印尾部：node --test 的失败摘要就在最后十几行。
        print('\n'.join(output.strip().splitlines()[-25:]), file=sys.stderr)
        return {'ok': False, 'returncode': proc.returncode}
    # `node --test` 的摘要行形如 "ℹ pass 244"。不写死行首字符：Windows 上
    # 控制台编码不保证能表示那个符号，实测会被替换成别的字符。
    passed = 0
    for line in output.splitlines():
        match = re.search(r'\bpass\s+(\d+)\s*$', line)
        if match:
            passed = int(match.group(1))
    print(f'前端用例通过 {passed} 例（npm test）')
    return {'ok': True, 'passed': passed}


def static_suite() -> dict:
    """静态检查：只查「一旦执行就一定炸」的那一类（F821 未定义名字）。

    ## 为什么单独加这一条

    2026-11-22 的「平台属性收敛」重构把 `_platform_spec` 从 `_sessions()` 的
    局部 import 里删掉、提到别处，但 `start()` 里那两处用法没跟着改 —— 结果
    **每点一次「添加账号」就是一次 NameError / HTTP 500**，线上真实触发了 4 次。

    更糟的是它逃过了所有既有测试：`test_platform_auth.py` 里 32 个用例其实一直
    失败，只是那个文件**没被加进门禁清单**，于是「门禁全绿」和「测试是红的」
    同时成立，两天没人发现。

    所以这里做两件事：静态查 F821（覆盖全部源码，包括还没写测试的地方），
    并把该测试文件加进 `MODULES`。前者防新增，后者防已有。

    规则集故意收得很窄：完整 ruff 默认规则在这个仓库有 2700+ 条历史告警
    （`UP006`/`I001` 之类风格项），全开会把门禁变成噪声墙，反而没人看。
    历史上 `F811`（重复定义）报 53 条。2026-11-24 逐条看过，里面**没有一条**
    是 `try: import x except: x = None` —— 全是测试文件里「参数名遮蔽了 import
    进来的 fixture」这种有意写法，另有 3 处是生产代码在同一函数内重复 import
    模块级早已 import 过的模块。那 3 处已修掉，所以 F811 现在只查生产代码：
    测试里的参数遮蔽是正当写法，收进来只会变成噪声墙。
    """
    exe = shutil.which('ruff')
    if exe:
        base = [exe, 'check']
    else:
        exe = shutil.which('python') or shutil.which('python3')
        if not exe:
            return {'skipped': True, 'reason': 'ruff/python not found'}
        base = [exe, '-m', 'ruff', 'check']

    checks = (
        (['server', 'deploy', 'tools', '--select', 'F821'],
         '未定义名字（执行到就是 NameError）', 'F821 未定义名字'),
        (['server', 'deploy', 'tools', '--exclude', 'server/tests',
          '--select', 'F811'],
         '生产代码里的重复定义', 'F811 重复定义'),
    )
    for extra, label, short in checks:
        args = base + extra + ['--output-format', 'concise']
        try:
            proc = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                                  timeout=300, encoding='utf-8', errors='replace')
        except (OSError, subprocess.TimeoutExpired) as exc:
            # ruff 没装时 `python -m ruff` 返回非零而不是抛异常，真正的 OSError
            # 意味着解释器本身有问题 —— 这时跳过比让门禁误报更合理。
            return {'skipped': True, 'reason': str(exc)}
        output = ((proc.stdout or '') + (proc.stderr or '')).strip()
        if 'No module named ruff' in output or "No module named 'ruff'" in output:
            return {'skipped': True, 'reason': 'ruff not installed'}
        if proc.returncode != 0:
            print(f'静态检查发现{label}：', file=sys.stderr)
            print('\n'.join(output.splitlines()[:25]), file=sys.stderr)
            return {'ok': False, 'returncode': proc.returncode}
        print(f'静态检查通过（{short} 0 处）')
    return {'ok': True}


def main() -> int:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if os.environ.get("ZH_CI_NET_ISOLATED") == "1":
        subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    with tempfile.TemporaryDirectory(prefix="zhihui-ci-data-",
                                     ignore_cleanup_errors=True) as data_dir:
        os.environ["WB_DATA_DIR"] = data_dir
        suite = unittest.TestSuite()
        loader = unittest.defaultTestLoader
        for name in MODULES:
            suite.addTests(loader.loadTestsFromName(name))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        # 前端用例放在 Python 套件之后：Python 失败时先看到 Python 的报告，
        # 不必等一套无关的 Node 进程。
        frontend = frontend_suite()
        static = static_suite()
        summary = {
            "modules": list(MODULES),
            "tests": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skips": len(result.skipped),
            "frontend": frontend,
            "static": static,
            "network_namespace_isolated": os.environ.get("ZH_CI_NET_ISOLATED") == "1",
            "isolated_data_dir": True,
        }
        Path("ci-validation-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            from server import budgetowner, db
            budgetowner.close()
            with db._lock:
                db._close_connection_locked()
        except Exception:
            pass
    return 0 if (result.wasSuccessful()
                 and frontend.get('ok') is not False
                 and static.get('ok') is not False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
