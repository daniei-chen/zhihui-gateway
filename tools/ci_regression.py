#!/usr/bin/env python3
"""Run the deterministic release regression set.

Every test module is assigned to its unittest or pytest loader. Tests use
temporary data and a local synthetic provider; checks that require external
deployment artifacts or a different operating system retain explicit skip reasons.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "server.tests.test_v12100_trae_native",
    "server.tests.test_platform_policy",
    "server.tests.test_version_candidate_provenance",
    "server.tests.test_audit_deploy_failure_paths",
    "server.tests.test_audit_identity_platform_boundary",
    "server.tests.test_security_audit_hardening",
    "server.tests.test_ci_required_gates",
    "server.tests.test_v133_release_hardening",
    "server.tests.test_v12149_post_migration_cleanup",
    "server.tests.test_v12152_rollout_script",
    # 与上面那个是**两个不同的脚本**：`rollout.sh` 管 prepare/run/cleanup，
    # `rollout-rolling.sh` 管滚动重启（发版不打断在途调用的核心）。
    # 后者长期没有测试覆盖，2026-11-25 因此同时漏掉了「版本号前缀没归一化」
    # 和「5xx 统计没限定 /v1/」两个问题。
    "server.tests.test_rollout_rolling_contract",
    # D-04：停候选实例之前等在途请求跑完（假的 systemctl/journalctl，不需要 systemd）。
    # 它和上面那条守的是同一个脚本，但一条读源码结构、一条真的把函数跑起来。
    "server.tests.test_rollout_candidate_drain",
    # 发布脚本轮询的那个只回环在途探针（/api/health/inflight）本身的契约。
    "server.tests.test_health_inflight",
    "server.tests.test_verify_runtime_config",
    "server.tests.test_healthcheck_coverage",
    "server.tests.test_key_quota_alerts",
    "server.tests.test_steward_credit_alerts",
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
    # 当时按主题逐步纳入；2026-11-27 已收完全部存量并对两个 loader 校验非空。
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
    # test_audit_runtime_repairs 的夹具边界已修复，现由下方 PYTEST_MODULES 强制运行。
    "server.tests.test_v1269_model_completion_alert",
    "server.tests.test_v1274_budget_rejected_unknown",
    "server.tests.test_v1283_budget_alert_thresholds",
    "server.tests.test_v1297_recovery_chain_alert",
    # 余额告警的方向判断：只报「低于阈值」不报「在涨还是在掉」，读的人判断不了
    # 急不急。2026-11-26 线上那条 alarm 连续 19.5 小时，文案引用的还是三天前
    # 已经失效的观察（「日消耗约 24 分，净增长 +76/天」）。
    "server.tests.test_v1613_low_account_trend",
    # 持续 active 的告警，detail 也要刷新：否则库里永远停在第一次进入时的文案。
    "server.tests.test_v1614_alert_detail_refresh",
    # 发布目录无上限增长：65 个版本 2.6 GB。prune 要能清理，但不能把回滚路径清掉。
    "server.tests.test_v1618_release_prune",
    "server.tests.test_backup_health_evidence",
    "server.tests.test_audit_notify_config",
    "server.tests.test_audit_regressions",
    "server.tests.test_control_plane_stability",
    "server.tests.test_automatic_zero_evidence",
    # 积分 / 账本 / 配额主题补收（2026-11-23 全部单独跑通后入清单）。
    # 这批是「钱」相关的不变量：预授权、日额度、结算真相、并发原子性。
    # 之前一条都没在门禁里跑过，等于账本逻辑没有自动守卫。
    #
    # unittest 不会发现顶层 pytest 函数。此类文件放入 PYTEST_MODULES，
    # CI 使用 hash 锁定的 pytest 依赖；缺少依赖、收集失败或模块零用例均失败。
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
    # 2026-11-26 才发现它一直不在列表里：`/api/stats/by-key` 的合计与日序列
    # 对不对得上，全靠这个文件。磁盘上 499 个测试文件、门禁只跑 93 个 ——
    # 剩下那 407 个的收敛是另一件事，这里只补上本轮动到的那一个。
    "server.tests.test_platform_key_stats",
    # 守「磁盘上有、清单里没有」的新测试文件：不写进 MODULES 就永远不执行，
    # 而写下它的人会以为「我加了测试」。存量 406 个记在白名单里，只许减少。
    "server.tests.test_v1622_gate_manifest_coverage",
    "server.tests.test_web_polling_visibility",
    "server.tests.test_v1276_platform_timeout",
    "server.tests.test_section_nav",
    # ── 2026-11-26 补入：磁盘上有、清单里一直没有的那批 ──
    # 摸底 406 个：381 个能过、25 个过不了。后者先留在白名单里逐个修，
    # 前者没有理由继续不跑 —— 它们本来就是好的，只是没人执行过。
    "server.tests.test_access_log_reason",
    "server.tests.test_account_disable",
    "server.tests.test_account_groups",
    "server.tests.test_account_list",
    "server.tests.test_account_proxy",
    "server.tests.test_account_status_fields",
    "server.tests.test_account_status",
    "server.tests.test_account_upload",
    "server.tests.test_accountguard",
    "server.tests.test_accountlog",
    "server.tests.test_admission_bytes",
    "server.tests.test_alert_persistence_across_restart",
    "server.tests.test_apply_upstream_patches",
    "server.tests.test_archive_cycle_alerts",
    "server.tests.test_async_state",
    "server.tests.test_attempt_explain",
    "server.tests.test_audit_2026_09_16",
    "server.tests.test_audit_20261029_gateway_ledger",
    "server.tests.test_audit_20261029_login_timing",
    "server.tests.test_audit_account_scope",
    "server.tests.test_audit_adapter_auth",
    "server.tests.test_audit_bodyguard_resources",
    "server.tests.test_audit_credential_purge",
    "server.tests.test_audit_ip_binding",
    "server.tests.test_audit_local_import_files",
    "server.tests.test_audit_log_filters",
    "server.tests.test_audit_management_boundaries",
    "server.tests.test_audit_portal_schema",
    "server.tests.test_audit_probe_evidence",
    "server.tests.test_audit_redpacket_atomicity",
    "server.tests.test_audit_taskrun_lifecycle",
    "server.tests.test_audit_upstreambody",
    "server.tests.test_audit_usage_evidence",
    "server.tests.test_audit_v2_regressions",
    "server.tests.test_auth_log_flood",
    "server.tests.test_authorization_generation",
    "server.tests.test_backup_bytecode_policy",
    "server.tests.test_base_path_guard",
    "server.tests.test_checkin_all_scope",
    "server.tests.test_checkin_today_status",
    "server.tests.test_compensation_claim",
    "server.tests.test_concurrent_account_import",
    "server.tests.test_config_io",
    "server.tests.test_credit_expiry",
    "server.tests.test_credit_reconcile_guard_core",
    "server.tests.test_credits_ledger",
    "server.tests.test_current_version_readonly",
    "server.tests.test_dashboard_credit_expiry",
    "server.tests.test_dashboard_snapshot_entry",
    # DAN-41：投递通道未配置时告警不许静默消失。unittest 类（没有顶层 test 函数），
    # 所以进 MODULES 而不是 PYTEST_MODULES —— 后者会被
    # test_function_tests_are_assigned_to_the_pytest_loader 当场抓住。
    # 修前 9 failed，修后 9 passed；其中一条走真实调用点 operational.transition()。
    "server.tests.test_dan41_alert_delivery_visible",
    "server.tests.test_day_boundary_consistency",
    "server.tests.test_day_filter_equivalence",
    "server.tests.test_db_connection_ownership",
    "server.tests.test_deadline_transport_boundaries",
    "server.tests.test_display_prefs",
    "server.tests.test_dock_groups",
    "server.tests.test_docker_deploy",
    "server.tests.test_doubao_account_document",
    "server.tests.test_doubao_checkin",
    "server.tests.test_doubaowork_adapter",
    "server.tests.test_doubaowork_upstream_v1",
    "server.tests.test_drain_asgi",
    "server.tests.test_drain",
    "server.tests.test_dynamic_upstream_switch",
    "server.tests.test_edge_identity_probe",
    "server.tests.test_engine_overview",
    "server.tests.test_error_hint",
    "server.tests.test_errtext",
    "server.tests.test_force_clear_cooling",
    "server.tests.test_force_cooling",
    "server.tests.test_full_upgrade",
    "server.tests.test_gateway_upstream_hint",
    "server.tests.test_github_mirror",
    "server.tests.test_health_empty_means_unreachable",
    "server.tests.test_hot_path",
    "server.tests.test_http_client_pool",
    "server.tests.test_image_capabilities",
    "server.tests.test_instance_lease_lock_clock",
    "server.tests.test_instance_lease_soak",
    "server.tests.test_instance_leases",
    "server.tests.test_instance_runtime",
    "server.tests.test_issue_reply_style",
    "server.tests.test_issue173_fallback_model",
    "server.tests.test_issue45_admin_diagnosis",
    "server.tests.test_issue47_alias_realm",
    "server.tests.test_issue48_rsc_navigation",
    "server.tests.test_issue51_realm_summary_rows",
    "server.tests.test_issue52_rate_window",
    "server.tests.test_issue53_today_range",
    "server.tests.test_issue55_compose_and_deploy_sync",
    "server.tests.test_issue56_viewer_credits",
    "server.tests.test_issue59_upstream_stats",
    "server.tests.test_issue62_prompt_file",
    "server.tests.test_issue67_account_note",
    "server.tests.test_k36_security_headers",
    "server.tests.test_key_export",
    "server.tests.test_key_import",
    "server.tests.test_key_realm_e2e",
    "server.tests.test_key_realm",
    "server.tests.test_key_usage",
    "server.tests.test_keys_corrupt_data",
    "server.tests.test_keys_scope",
    "server.tests.test_keys_validation",
    "server.tests.test_late_usage_race",
    "server.tests.test_ledger_channel_health",
    "server.tests.test_ledger_replay",
    "server.tests.test_legacy_keys",
    "server.tests.test_live_http_platform_flow",
    "server.tests.test_log_query_plan",
    "server.tests.test_login_admission",
    "server.tests.test_login_counters",
    "server.tests.test_login_lockout_contract",
    "server.tests.test_login_server_poll",
    "server.tests.test_manual_disabled",
    "server.tests.test_max_tokens_clamp",
    "server.tests.test_max_tokens_e2e",
    "server.tests.test_metering_attribution",
    "server.tests.test_metrics_auth",
    "server.tests.test_model_catalog",
    "server.tests.test_model_filter",
    "server.tests.test_model_name_prefix",
    "server.tests.test_models_listing_compat",
    "server.tests.test_models_refetch_feedback",
    "server.tests.test_models_scope_whitelist",
    "server.tests.test_models_source",
    "server.tests.test_module_convergence",
    "server.tests.test_multipart_upload",
    "server.tests.test_namespace_compat",
    "server.tests.test_native_modality",
    "server.tests.test_native_upstream",
    "server.tests.test_next_prompt_rules",
    "server.tests.test_next_requestperf_contracts",
    "server.tests.test_no_mock_pollution",
    "server.tests.test_notify",
    "server.tests.test_observability_contract",
    "server.tests.test_offsite_backup",
    "server.tests.test_outbound_alignment",
    "server.tests.test_outbound_concurrency",
    "server.tests.test_owner_permission",
    "server.tests.test_pack_upstream_src",
    "server.tests.test_packagecheck",
    "server.tests.test_panel_reader",
    "server.tests.test_pgsync_review",
    "server.tests.test_pgsync",
    "server.tests.test_planned_drain_alerts",
    "server.tests.test_playground",
    "server.tests.test_pool_tcp_closure",
    "server.tests.test_portal_activation",
    "server.tests.test_portal_bonus",
    "server.tests.test_portal_capped_billing",
    "server.tests.test_portal_closeout",
    "server.tests.test_portal_lockout_contract",
    "server.tests.test_portal_members_ux",
    "server.tests.test_portal_operations",
    "server.tests.test_portal_public_pool",
    "server.tests.test_portal_release",
    "server.tests.test_portal_shared_schedule",
    "server.tests.test_portal_stability",
    "server.tests.test_probe_channel_fallback",
    "server.tests.test_probe_request",
    "server.tests.test_product_stats",
    "server.tests.test_prompt_compat_switch",
    "server.tests.test_prompt_integrity",
    "server.tests.test_proxy_pool",
    "server.tests.test_proxy_routes_api",
    "server.tests.test_qoder_usage_scope",
    "server.tests.test_read_api_contract",
    "server.tests.test_read_scope",
    "server.tests.test_realm",
    "server.tests.test_reasoning_credential_roundtrip",
    "server.tests.test_redpacket",
    "server.tests.test_rejection_retry_integration",
    "server.tests.test_rejection_retry",
    "server.tests.test_rejection_visibility",
    "server.tests.test_relay_route",
    "server.tests.test_release_fallback",
    "server.tests.test_release_identity",
    "server.tests.test_reload_hotload",
    "server.tests.test_reload_state",
    "server.tests.test_remediation_contracts",
    "server.tests.test_request_binding",
    "server.tests.test_request_budgets",
    "server.tests.test_request_identity",
    "server.tests.test_requestbody_gzip",
    "server.tests.test_requestbody_timeout",
    "server.tests.test_response_message_order",
    "server.tests.test_responses_api",
    "server.tests.test_retention_archive",
    "server.tests.test_retention_policies",
    "server.tests.test_review_body_repairs",
    "server.tests.test_review_recovery_metering",
    "server.tests.test_review_tool_repairs",
    "server.tests.test_review_usage_repairs",
    "server.tests.test_router_module_names",
    "server.tests.test_schedule_form_validation",
    "server.tests.test_schema_reader_compatibility",
    "server.tests.test_schema_versioning",
    "server.tests.test_scope_contract",
    "server.tests.test_security_audit_fixes",
    "server.tests.test_security_lockout",
    "server.tests.test_security_report_fixes",
    "server.tests.test_session_hardening",
    "server.tests.test_session_secret_rotation",
    "server.tests.test_settings_tabs",
    "server.tests.test_shared_admission_middleware",
    "server.tests.test_shared_admission_protocols",
    "server.tests.test_shared_admission",
    "server.tests.test_sk_key_format",
    "server.tests.test_smtp_transport",
    "server.tests.test_ssrf_guard",
    "server.tests.test_startup_checks_lifetime",
    "server.tests.test_static_cache_headers",
    "server.tests.test_static_traversal",
    "server.tests.test_stats_failures",
    "server.tests.test_stats_throughput",
    "server.tests.test_steward_mail_policy",
    "server.tests.test_steward_metering",
    "server.tests.test_stream_error_logging",
    "server.tests.test_subpath_prefix",
    "server.tests.test_task_leases",
    "server.tests.test_task_log_filter_visibility",
    "server.tests.test_task_summary",
    "server.tests.test_tasklog_collect",
    "server.tests.test_tasklog",
    "server.tests.test_taskrun",
    "server.tests.test_time_display_marked",
    "server.tests.test_token_renew",
    "server.tests.test_tool_discovery_http",
    "server.tests.test_tool_discovery",
    "server.tests.test_trae_refresh_error_surface",
    "server.tests.test_trae_route",
    "server.tests.test_transition_ownership",
    "server.tests.test_trust_and_upstash_hardening",
    "server.tests.test_u05_max_tokens_contract",
    "server.tests.test_u13_error_contracts",
    "server.tests.test_u13_shared_budgets",
    "server.tests.test_u13_structured_output",
    "server.tests.test_unknown_closure",
    "server.tests.test_update_api_fallback",
    "server.tests.test_update_check_honesty",
    "server.tests.test_update_e2e",
    "server.tests.test_update_lifecycle",
    "server.tests.test_update_log_redaction",
    "server.tests.test_update_package",
    "server.tests.test_update_release_notes",
    "server.tests.test_update_robustness",
    "server.tests.test_updater_atomic_swap",
    "server.tests.test_updater_status",
    # DAN-24：发布源白名单 + 发布包验签（marker 防退化）
    "server.tests.test_updater_supply_chain",
    "server.tests.test_upgrade_audit",
    "server.tests.test_upload_loop_fairness",
    "server.tests.test_upstream_id_consistency",
    "server.tests.test_upstream_patches",
    "server.tests.test_upstreams",
    "server.tests.test_usage_chart",
    "server.tests.test_usage_credit",
    "server.tests.test_usage_daily_migration",
    "server.tests.test_usage_endpoint",
    "server.tests.test_usage_hourly",
    "server.tests.test_usage_realm",
    "server.tests.test_usage_timezone_forcing",
    "server.tests.test_usage_timezone",
    "server.tests.test_user_portal",
    "server.tests.test_user_roles",
    "server.tests.test_v004_admission_correctness",
    "server.tests.test_v004_mutation_results",
    "server.tests.test_v004_output_limits",
    "server.tests.test_v004_reload_safety",
    "server.tests.test_v004_trae_pool_safety",
    "server.tests.test_v004_upstream_review",
    "server.tests.test_v006_contracts",
    "server.tests.test_v006_mixed_load",
    "server.tests.test_v007_adoption",
    "server.tests.test_v007_catalog",
    "server.tests.test_v007_compatibility",
    "server.tests.test_v007_protocol_replay",
    "server.tests.test_v007_refresh",
    "server.tests.test_v007_release_branding",
    "server.tests.test_v007_startup",
    "server.tests.test_v12101_read_api_timezone",
    "server.tests.test_v12106_disk_metrics",
    "server.tests.test_v12107_prune_releases",
    "server.tests.test_v12108_internal_session_secret",
    "server.tests.test_v12109_qtg_secret_reader",
    "server.tests.test_v12110_proxy_client_ip",
    "server.tests.test_v12113_implicit_auth_contract",
    "server.tests.test_v12114_proxy_cache_isolation",
    "server.tests.test_v12115_metrics_warning_diagnostics",
    "server.tests.test_v12117_startup_identity_check",
    "server.tests.test_v12120_first_byte_budget",
    "server.tests.test_v12121_dead_config_watch",
    "server.tests.test_v12129_origin_exact_match",
    "server.tests.test_v12133_account_pool_metrics",
    "server.tests.test_v12136_metrics_dedupe",
    "server.tests.test_v12139_qoder_refresh_result_shape",
    "server.tests.test_v12140_trae_refresh_not_needed",
    "server.tests.test_v12142_stage_normalises_web_modes",
    "server.tests.test_v12144_backup_release_version",
    "server.tests.test_v12145_backup_stale_scratch",
    "server.tests.test_v12146_engine_metric",
    "server.tests.test_v12148_audit_chain_cycle",
    "server.tests.test_v12157_backup_version_pair",
    "server.tests.test_v12159_ttft_metric",
    "server.tests.test_v12161_attempt_events_deprecation",
    "server.tests.test_v12162_upstream_local_changes",
    "server.tests.test_v12163_throttle_retry_after",
    "server.tests.test_v12164_known_gap_doc",
    "server.tests.test_v12165_upstream_retry_after",
    "server.tests.test_v12168_metrics_doc",
    "server.tests.test_v12169_docs_index",
    "server.tests.test_v12170_build_script",
    "server.tests.test_v12174_python_deps_audit",
    "server.tests.test_v12175_route_coverage",
    "server.tests.test_v12177_ops_pitfalls_doc",
    "server.tests.test_v12181_preflight",
    "server.tests.test_v12186_error_baseline",
    "server.tests.test_v12188_backup_status",
    "server.tests.test_v12189_recovery_housekeeping",
    "server.tests.test_v12190_recovery_installer",
    "server.tests.test_v12192_service_hardening",
    "server.tests.test_v12193_tls_timers",
    "server.tests.test_v12194_11102_rationale",
    "server.tests.test_v12199_login_remaining",
    "server.tests.test_v1230_hardening",
    "server.tests.test_v1231_config_cache",
    "server.tests.test_v1232_settlement_offload",
    "server.tests.test_v1236_settlement_skip",
    "server.tests.test_v1237_archive_wiring",
    "server.tests.test_v1238_relay_timeout",
    "server.tests.test_v1239_tz_and_config_cache",
    "server.tests.test_v1240_prune_outside_transaction",
    "server.tests.test_v1241_usage_upsert",
    "server.tests.test_v1242_admission_scale",
    "server.tests.test_v1243_body_accounting",
    "server.tests.test_v1244_thread_pools",
    "server.tests.test_v1245_long_transaction_isolation",
    "server.tests.test_v1246_output_commit",
    "server.tests.test_v1247_declared_output",
    "server.tests.test_v1248_external_platforms",
    "server.tests.test_v1249_qoder_refresh",
    "server.tests.test_v1250_stream_error_detail",
    "server.tests.test_v1251_trae_accounts",
    "server.tests.test_v1255_attempt_backfill",
    "server.tests.test_v1257_shutdown_executor",
    "server.tests.test_v1260_budget_review_index",
    "server.tests.test_v1261_forwarding_phases",
    "server.tests.test_v1262_other_platforms",
    "server.tests.test_v1264_admission_snapshot_keys",
    "server.tests.test_v1265_capacity_rejection_hint",
    "server.tests.test_v1266_platform_prefix",
    "server.tests.test_v1268_process_metrics",
    "server.tests.test_v1271_disk_space_alerts",
    "server.tests.test_v1272_output_over_limit",
    "server.tests.test_v1273_forwarding_attempt",
    "server.tests.test_v1275_upstream_capabilities",
    "server.tests.test_v1278_readiness_detail",
    "server.tests.test_v1280_invalid_messages",
    "server.tests.test_v1281_cgroup_memory_peak",
    "server.tests.test_v1282_metric_semantics",
    "server.tests.test_v1284_bucket_key_coverage",
    "server.tests.test_v1285_monitor_heartbeat",
    "server.tests.test_v1286_nginx_fence_detection",
    "server.tests.test_v1287_staged_manifest",
    "server.tests.test_v1288_error_code_filter",
    "server.tests.test_v1289_account_counting",
    "server.tests.test_v1290_settle_client_rejection",
    "server.tests.test_v1291_body_timeout_split",
    "server.tests.test_v1292_db_size_metric",
    "server.tests.test_v1293_backup_age_metric",
    "server.tests.test_v1296_batch_resolve",
    "server.tests.test_v1298_archive_worker_visibility",
    "server.tests.test_v1299_http_pool_invariants",
    "server.tests.test_v132_manifest_identity",
    "server.tests.test_v132_settle_sees_http_status",
    "server.tests.test_version_cache",
    "server.tests.test_web_a11y",
    "server.tests.test_web_dangerous_actions",
    "server.tests.test_web_wiring",
    # Audit closure: every historical unittest suite and new regression runs.
    "server.tests.test_audit_20261029_security",
    "server.tests.test_audit_stats_public_scope",
    "server.tests.test_command_palette",
    "server.tests.test_doubaowork_platform",
    "server.tests.test_issue39_estimate_effort",
    "server.tests.test_issue69_cache_tokens",
    "server.tests.test_preauth_body",
    "server.tests.test_protocol_error_closure",
    "server.tests.test_qoder_checkin_evidence",
    "server.tests.test_recovery_closure",
    "server.tests.test_refresh_all_tokens",
    "server.tests.test_refresh_plan",
    "server.tests.test_release_quality_closure",
    "server.tests.test_runtime_ownership_memory",
    "server.tests.test_rolling_stream_evidence",
    "server.tests.test_settings_contract",
    "server.tests.test_speed_boundaries",
    "server.tests.test_steward",
    "server.tests.test_unknown_archive",
    "server.tests.test_v004_managed_update_boundary",
    "server.tests.test_v12137_snapshot_gauge_always_emitted",
    "server.tests.test_v12156_stale_transactions",
    "server.tests.test_v12160_context_metric",
    "server.tests.test_v1279_route_authorization",
    # DAN-19：拒绝路径不再同步写不可变账本，且 `usage_events` 有了归档出口。
    # 两条断言都先红后绿（修前 4 failed，修后 4 passed），所以必须进门禁 ——
    # 否则「账本写入又回到返回路径上」这种回归没有任何测试会喊。
    "server.tests.test_dan19_rejection_ledger",
    # userstore.locked() 的第一份测试。这个模块守着 users.json 的读改写，
    # 但此前零测试：丢更新、跨进程互斥、5 秒 deadline、symlink/nlink 拒绝
    # 全靠人读代码。五处变异各自杀掉一条用例（见 PR 证据）。
    "server.tests.test_userstore_lock",
)


# Pytest functions cannot be discovered by unittest; this suite is mandatory.
PYTEST_MODULES = (
    "server.tests.test_merge_page_latency",
    "server.tests.test_audit_20261029_web_gate",
    "server.tests.test_audit_finance_repairs",
    "server.tests.test_audit_runtime_repairs",
    "server.tests.test_audit_security_repairs",
    "server.tests.test_budget_batch_bounds",
    "server.tests.test_merge_read_integrity",
    "server.tests.test_merge_remaining_boundaries",
    "server.tests.test_platform_integration_guards",
    "server.tests.test_workbench_registration",
    # DAN-13：红包零份额 / 0 额度 = 不限。用 pytest 的夹具隔离数据目录，
    # unittest 发现不到顶层函数，必须放这一组。
    "server.tests.test_dan13_redpacket_zero_share",
    # DAN-23：流式断连必须收敛到确定结算，且不能把有额度密钥锁成终态。
    # 三条都先红后绿（修前 3 failed，修后 3 passed）—— 其中两条走真实 ASGI
    # 断连（上游发一帧就挂住，客户端在流中途挂断），不是手搓的假输入。
    "server.tests.test_dan23_disconnect_convergence",
    # G-02：DAN-23 的 settled 契约必须在崩溃恢复后仍然成立（回执重放走同一
    # 判据），且 charged_partial 必须有归档出口。修前 3 failed / 修后全绿。
    "server.tests.test_g02_replay_client_cancelled",
)


def pytest_suite() -> dict:
    """Run function/fixture suites with the same temporary data and loopback provider.

    Missing pytest, collection errors, zero tests and timeouts are failures.
    Disable third-party auto-loaded plugins to keep clean checkouts reproducible.
    """
    with tempfile.TemporaryDirectory(prefix='zhihui-ci-pytest-') as temporary:
        report = Path(temporary) / 'results.xml'
        command = [sys.executable, '-B', '-m', 'pytest', '-q', '--tb=short',
                   '-p', 'no:cacheprovider', f'--junitxml={report}']
        command.extend(name.replace('.', '/') + '.py' for name in PYTEST_MODULES)
        environment = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
                       'PYTHONDONTWRITEBYTECODE': '1'}
        try:
            proc = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                  text=True, encoding='utf-8', errors='replace', timeout=600)
            tree = ET.parse(report)
            suites = list(tree.getroot().iter('testsuite'))
            counts = {name: sum(int(row.get(name, '0')) for row in suites)
                      for name in ('tests', 'failures', 'errors', 'skipped')}
        except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired) as exc:
            return {'ok': False, 'required': True, 'reason': str(exc)}
        passed = counts['tests'] - counts['failures'] - counts['errors'] - counts['skipped']
        cases = list(tree.getroot().iter('testcase'))
        empty_modules = [name for name in PYTEST_MODULES if not any(
            case.get('classname', '') == name or case.get('classname', '').startswith(name + '.')
            for case in cases)]
        ok = (proc.returncode == 0 and passed > 0 and not empty_modules
              and not counts['failures'] and not counts['errors'])
        if not ok:
            output = (proc.stdout or '') + (proc.stderr or '')
            print('\n'.join(line[:1000] for line in output.splitlines()[-45:]), file=sys.stderr)
        print(f'历史 pytest 回归：{passed} passed / {counts["skipped"]} skipped')
        skipped = [{'test': row.get('classname', '') + '.' + row.get('name', ''),
                    'reason': row.find('skipped').get('message', '')}
                   for row in tree.getroot().iter('testcase') if row.find('skipped') is not None]
        return {'ok': ok, 'required': True, 'returncode': proc.returncode,
                **counts, 'passed': passed, 'skipped_tests': skipped,
                'empty_modules': empty_modules,
                'modules': list(PYTEST_MODULES)}


def frontend_suite() -> dict:
    """Run both frontend suites; release callers require them explicitly.

    Backend-only jobs leave frontend validation to the dedicated quality job.
    Local missing dependencies remain visible as skipped; --require-frontend
    turns that result into a failing gate instead of silently publishing it.
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
    if passed == 0:
        return {'ok': False, 'reason': 'frontend reported no passing tests'}
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
    # Use this interpreter's pinned tool, not an unrelated global ruff/python.
    base = [sys.executable, '-m', 'ruff', 'check']

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


def _gate_result(result: dict, *, required: bool) -> dict:
    value = {**result, 'required': required}
    if required and (value.get('skipped') or value.get('ok') is not True):
        value['ok'] = False
        value.setdefault('reason', 'required suite did not complete successfully')
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--require-frontend', action='store_true',
                        help='fail if frontend dependencies or tests are unavailable')
    parser.add_argument('--require-static', action='store_true',
                        help='fail if the pinned Python static checker is unavailable')
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if os.environ.get("ZH_CI_NET_ISOLATED") == "1":
        subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    from server.tests.synthetic_provider import synthetic_provider
    with tempfile.TemporaryDirectory(prefix="zhihui-ci-data-",
                                     ignore_cleanup_errors=True) as data_dir, synthetic_provider() as provider:
        os.environ["WB_DATA_DIR"] = data_dir
        suite = unittest.TestSuite()
        loader = unittest.defaultTestLoader
        empty_modules = []
        for name in MODULES:
            loaded = loader.loadTestsFromName(name)
            if loaded.countTestCases() == 0:
                empty_modules.append(name)
            suite.addTests(loaded)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        backend_nonempty = result.testsRun > 0 and not empty_modules
        if not backend_nonempty:
            print(f'Backend discovery produced empty modules: {empty_modules}', file=sys.stderr)
        historical = pytest_suite()
        # 前端用例放在 Python 套件之后：Python 失败时先看到 Python 的报告，
        # 不必等一套无关的 Node 进程。
        frontend = _gate_result(frontend_suite(), required=args.require_frontend)
        static = _gate_result(static_suite(), required=args.require_static)
        for name, gate in (('historical_pytest', historical), ('frontend', frontend), ('static', static)):
            if gate.get('required') and gate.get('ok') is not True:
                print(f'{name} required gate failed: {gate.get("reason", gate)}',
                      file=sys.stderr)
        summary = {
            "modules": list(MODULES),
            "tests": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skips": len(result.skipped),
            "empty_modules": empty_modules,
            "skipped_tests": [{"test": case.id(), "reason": reason} for case, reason in result.skipped],
            "loopback_provider": provider,
            "historical_pytest": historical,
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
                 and backend_nonempty
                 and historical.get('ok') is True
                 and frontend.get('ok') is not False
                 and static.get('ok') is not False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
