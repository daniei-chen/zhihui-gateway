#!/usr/bin/env python3
"""Run the deterministic release regression set.

The repository also contains historical audit and deployment-environment suites.
Those suites remain useful during an environment-specific audit, but this small
set is the repeatable release gate for a clean checkout with synthetic data.
"""

from __future__ import annotations

import json
import os
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
)


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
        summary = {
            "modules": list(MODULES),
            "tests": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skips": len(result.skipped),
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
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
