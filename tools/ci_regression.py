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
