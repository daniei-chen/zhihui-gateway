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
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "server.tests.test_release_consistency",
    "server.tests.test_product_origin",
    "server.tests.test_changelog",
    "server.tests.test_readme_i18n",
    "server.tests.test_docs_packaging",
    "server.tests.test_release_integrity",
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
    }
    Path("ci-validation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
