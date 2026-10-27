#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""status_lint.py — 校验 tasks/status.json（schema=zhihui-task-status/3）。

R0-C 交付。目标不是“让格式好看”，而是让机器执行者能信任台账：
  1. JSON 可解析；
  2. 52 个任务（K01–K52）一个不少、id 不重复；T00–T03 准备项齐全；
  3. 三轴字段非 null 且在枚举内；
  4. evidence_paths 全部真实存在（相对仓库根，文件或目录），不存在就失败；
  5. 不允许“local_acceptance_complete=true 但 evidence_paths 为空”的旧问题回归；
  6. defects（RV01–RV08）引用合法任务 id，fixed 必须有修复证据；
  7. 交互台账 5 类计数合计 209，标签缺口合计 141，且明确“不得合并成通过数”；
  8. fact_baseline 的 Windows Manager/QTG 数字与 evidence/runs 里的 stats 文件一致；
  9. 人读摘要 tasks/STATUS-R0.md 存在并引用基线证据路径。

用法：
    python -X utf8 tools/status_lint.py            # 在仓库根或任意目录运行
    python -X utf8 tools/status_lint.py --quiet    # 只输出 FAIL
退出码：0 = 全部通过；1 = 有失败项（逐条打印）。
"""

import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATUS = os.path.join(REPO, "tasks", "status.json")
STATUS_R0 = os.path.join(REPO, "tasks", "STATUS-R0.md")

IMPL = {"implemented", "partial", "not_started", "not_applicable"}
VER = {"verified", "partially_verified", "not_verified", "blocked"}
REL = {"not_deployed", "candidate", "deployed", "unknown"}
DEFECT_STATUS = {"open", "in_progress", "fixed"}
SEVERITY = {"P1", "P2", "P1-P2"}
SOURCE_HASH_RE = re.compile(r"^(view-sha256|bundle-sha256|source-sha256):[0-9a-f]{64}$")
# verified 卡的 remaining 只允许“非原卡要求”的门禁/外部/补强类条目（新 verified 定义）
VERIFIED_REMAINING_PREFIXES = ("（生产门禁）", "（外部条件）", "（生产规模）", "（流程加固）", "（补强）", "（发布计划）")
EXPECTED_K = ["K%02d" % i for i in range(1, 53)]
EXPECTED_T = ["T00", "T01", "T02", "T03"]
LEDGER_CATEGORIES = ["verified_full", "passed_in_recorded_scope", "not_present_in_ui",
                     "not_applicable_needs_product_decision", "verified_unsupported_in_lab"]

errors = []
checks = 0


def ok(msg):
    global checks
    checks += 1


def fail(msg):
    errors.append(msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        fail(msg)
    return cond


def rel_exists(rel_path):
    if not isinstance(rel_path, str) or not rel_path:
        return False
    if os.path.isabs(rel_path) or rel_path.startswith("\\"):
        return False
    if "\\" in rel_path:
        return False
    norm = os.path.normpath(rel_path)
    if norm.startswith(".."):
        return False
    return os.path.exists(os.path.join(REPO, norm))


def load_stats(rel_path):
    with open(os.path.join(REPO, rel_path.replace("/", os.sep)), encoding="utf-8") as fh:
        data = json.load(fh)
    return data["stats"]


def main():
    quiet = "--quiet" in sys.argv
    if not os.path.exists(STATUS):
        print("FAIL: missing %s" % STATUS)
        return 1
    try:
        with open(STATUS, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print("FAIL: status.json 无法解析为 JSON: %r" % (exc,))
        return 1
    ok("status.json 可解析")

    check(data.get("schema") == "zhihui-task-status/3",
          "schema 必须为 zhihui-task-status/3（实际 %r）" % (data.get("schema"),))

    tasks = data.get("tasks") or []
    check(isinstance(tasks, list) and len(tasks) == 52,
          "任务数必须为 52（实际 %d）" % (len(tasks) if isinstance(tasks, list) else -1))
    ids = [t.get("id") for t in tasks]
    check(ids == EXPECTED_K, "K01–K52 必须齐全且顺序一致；差异=%s" % (
        sorted(set(EXPECTED_K) ^ set(ids)),))

    preps = data.get("preparations") or []
    prep_ids = [p.get("id") for p in preps]
    check(prep_ids == EXPECTED_T, "T00–T03 准备项必须齐全；实际=%s" % prep_ids)

    # ---- 逐任务三轴与证据 ----
    tri_null = []
    bad_enum = []
    empty_scope = []
    empty_remaining = []
    bad_evidence = []
    verified_without_evidence = []
    source_bad = []
    legacy_missing = []
    for t in tasks + preps:
        tid = t.get("id")
        for field in ("implementation_status", "verification_status", "release_status"):
            val = t.get(field)
            if val is None:
                tri_null.append("%s.%s" % (tid, field))
        if t.get("implementation_status") is not None and t["implementation_status"] not in IMPL:
            bad_enum.append("%s.implementation_status=%r" % (tid, t["implementation_status"]))
        if t.get("verification_status") is not None and t["verification_status"] not in VER:
            bad_enum.append("%s.verification_status=%r" % (tid, t["verification_status"]))
        if t.get("release_status") is not None and t["release_status"] not in REL:
            bad_enum.append("%s.release_status=%r" % (tid, t["release_status"]))

        scope = t.get("verified_scope")
        if not isinstance(scope, list) or any(not isinstance(x, str) or not x for x in (scope or [])):
            empty_scope.append("%s.verified_scope 非字符串列表" % tid)
        elif not scope and t.get("verification_status") not in ("not_verified", "blocked"):
            empty_scope.append("%s.verified_scope 为空但 verification_status=%r" % (tid, t.get("verification_status")))

        rem = t.get("remaining_requirements")
        if not isinstance(rem, list) or any(not isinstance(x, str) or not x for x in (rem or [])):
            empty_remaining.append("%s.remaining_requirements 非字符串列表" % tid)
        elif not rem and t.get("verification_status") != "verified":
            empty_remaining.append("%s.remaining_requirements 为空但 verification_status=%r" % (tid, t.get("verification_status")))
        elif t.get("verification_status") == "verified":
            bad_prefix = [x for x in rem if not x.startswith(VERIFIED_REMAINING_PREFIXES)]
            if bad_prefix:
                empty_remaining.append(
                    "%s 已 verified，但 remaining 含原卡未满足项（不是生产门禁/外部条件类）：%s" % (tid, bad_prefix))

        ev = t.get("evidence_paths")
        if not isinstance(ev, list):
            bad_evidence.append("%s.evidence_paths 不是列表" % tid)
            ev = []
        for p in ev:
            if not rel_exists(p):
                bad_evidence.append("%s.evidence_paths 不存在或非法路径: %r" % (tid, p))
        if t.get("verification_status") in ("verified", "partially_verified") and not ev:
            verified_without_evidence.append("%s verification=%r 但 evidence_paths 为空"
                                             % (tid, t.get("verification_status")))
        if t.get("local_acceptance_complete") is True and not ev:
            verified_without_evidence.append("%s local_acceptance_complete=true 但 evidence_paths 为空（旧问题回归）" % tid)

        sh = t.get("source_hash")
        if sh is not None and not (isinstance(sh, str) and SOURCE_HASH_RE.match(sh)):
            source_bad.append("%s.source_hash 格式非法: %r" % (tid, sh))
        if not t.get("source_hash_note"):
            source_bad.append("%s 缺 source_hash_note" % tid)

        lm = t.get("legacy_status_mapping")
        if not isinstance(lm, dict) or "old_status" not in lm or "mapping_note" not in lm:
            legacy_missing.append(tid)

    check(not tri_null, "三轴字段不得为 null；问题=%s" % tri_null)
    check(not bad_enum, "三轴枚举值非法；问题=%s" % bad_enum)
    check(not empty_scope, "verified_scope 校验失败；问题=%s" % empty_scope)
    check(not empty_remaining, "remaining_requirements 校验失败；问题=%s" % empty_remaining)
    check(not bad_evidence, "evidence_paths 必须全部真实存在；问题=%s" % bad_evidence)
    check(not verified_without_evidence, "证据缺失却标 verified/local_acceptance_complete；问题=%s"
          % verified_without_evidence)
    check(not source_bad, "source_hash/source_hash_note 校验失败；问题=%s" % source_bad)
    check(not legacy_missing, "缺少 legacy_status_mapping；问题=%s" % legacy_missing)

    # ---- 三轴计数自洽 ----
    actual = {"implementation_status": {}, "verification_status": {}, "release_status": {}}
    for t in tasks:
        for f in actual:
            v = t.get(f)
            actual[f][v] = actual[f].get(v, 0) + 1
    declared = data.get("tri_axis_counts") or {}
    for f in actual:
        check(declared.get(f) == actual[f],
              "tri_axis_counts.%s 与实际不符：声明=%r 实际=%r" % (f, declared.get(f), actual[f]))

    # ---- defects ----
    defects = data.get("defects") or []
    dv_ids = [d.get("id") for d in defects]
    expect_rv = ["RV0%d" % i for i in range(1, 9)]
    check(dv_ids == expect_rv, "defects 必须为 RV01–RV08；实际=%s" % dv_ids)
    dproblems = []
    for d in defects:
        did = d.get("id")
        if d.get("status") not in DEFECT_STATUS:
            dproblems.append("%s.status=%r" % (did, d.get("status")))
        if d.get("severity") not in SEVERITY:
            dproblems.append("%s.severity=%r" % (did, d.get("severity")))
        if not d.get("title") or not d.get("description"):
            dproblems.append("%s 缺 title/description" % did)
        aff = d.get("affected_tasks") or []
        if not isinstance(aff, list) or not aff:
            dproblems.append("%s.affected_tasks 为空" % did)
        bad_aff = [a for a in aff if a not in EXPECTED_K]
        if bad_aff:
            dproblems.append("%s.affected_tasks 引用非法任务 id: %s" % (did, bad_aff))
        for p in d.get("evidence_paths") or []:
            if not rel_exists(p):
                dproblems.append("%s.evidence_paths 不存在: %r" % (did, p))
        if not d.get("evidence_paths"):
            dproblems.append("%s.evidence_paths 为空" % did)
        if d.get("status") == "fixed":
            fix_ev = d.get("fix_evidence_paths") or []
            if not fix_ev:
                dproblems.append("%s 已 fixed 但缺 fix_evidence_paths" % did)
            for p in fix_ev:
                if not rel_exists(p):
                    dproblems.append("%s.fix_evidence_paths 不存在: %r" % (did, p))
        elif d.get("fix_evidence_paths"):
            dproblems.append("%s 未 fixed 却填了 fix_evidence_paths（不得提前宣称修复）" % did)
    check(not dproblems, "defects 校验失败；问题=%s" % dproblems)

    # ---- 交互台账口径 ----
    il = data.get("interaction_ledger") or {}
    counts = il.get("counts") or {}
    check(set(counts.keys()) == set(LEDGER_CATEGORIES),
          "interaction_ledger.counts 必须包含 5 类：%s；实际=%s" % (LEDGER_CATEGORIES, sorted(counts.keys())))
    total = 0
    cat_ok = True
    for cat in LEDGER_CATEGORIES:
        c = counts.get(cat) or {}
        n = c.get("count")
        if not isinstance(n, int) or not c.get("meaning"):
            cat_ok = False
        else:
            total += n
    check(cat_ok, "interaction_ledger 每类必须有 count(int) 与 meaning")
    check(total == il.get("total_rows") == 209,
          "交互 5 类合计必须 209（实际合计 %d，声明 total_rows=%r）" % (total, il.get("total_rows")))
    check(il.get("do_not_sum_as_passed") is True, "interaction_ledger.do_not_sum_as_passed 必须为 true")
    gaps = il.get("verified_full_label_gaps") or {}
    plats = gaps.get("platforms") or {}
    roles = gaps.get("roles") or {}
    psum = sum(v for k, v in plats.items() if k != "note")
    rsum = sum(v for k, v in roles.items() if k != "note")
    check(psum == 141, "141 条 verified_full 的平台标签缺口合计必须 141（实际 %d）" % psum)
    check(rsum == 141, "141 条 verified_full 的角色标签缺口合计必须 141（实际 %d）" % rsum)
    check((gaps.get("platforms") or {}).get("blank") == 39, "平台标签缺 39 条")
    check((gaps.get("roles") or {}).get("blank") == 39, "角色标签缺 39 条")
    spread = il.get("snapshot_spread") or {}
    check(spread.get("distinct_snapshots_verified_full") == 4, "verified_full 引用 4 个快照")

    # ---- R2-C：28+25=53 行范围映射（r2_mapping；不改原计数口径） ----
    r2 = il.get("r2_mapping")
    r2_problems = []
    if not isinstance(r2, list) or len(r2) != 53:
        r2_problems.append("interaction_ledger.r2_mapping 必须为 53 条（实际 %r）"
                           % (len(r2) if isinstance(r2, list) else r2))
        r2 = []
    doc = il.get("r2_mapping_doc")
    if not doc or not rel_exists(doc):
        r2_problems.append("r2_mapping_doc 缺失或不存在: %r" % doc)
    csv_path = "evidence/browser/coverage-20261025-0145-engine/interaction-verification.csv"
    csv_rows = {}
    if rel_exists(csv_path):
        with open(os.path.join(REPO, csv_path.replace("/", os.sep)), encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if row["interaction_verification"] in ("not_applicable_needs_product_decision", "not_present_in_ui"):
                    csv_rows[row["row_id"]] = row["interaction_verification"]
    else:
        r2_problems.append("r2_mapping 依赖的 coverage CSV 不存在")
    verdict_enum = {"已实现", "被合并", "明确不适用", "迁到别处", "确实没做", "待产品决定"}
    map_ids = {m.get("row_id") for m in r2}
    if csv_rows and map_ids != set(csv_rows):
        r2_problems.append("r2_mapping 行 id 与 CSV 53 行不一致：多=%s 少=%s"
                           % (sorted(map_ids - set(csv_rows)), sorted(set(csv_rows) - map_ids)))
    for m in r2:
        if m.get("source_status") != csv_rows.get(m.get("row_id")):
            r2_problems.append("%s source_status 与 CSV 不一致" % m.get("row_id"))
        if m.get("verdict") not in verdict_enum:
            r2_problems.append("%s verdict 非法: %r" % (m.get("row_id"), m.get("verdict")))
        for key in ("r2_section", "current_entry", "basis", "decider"):
            if not m.get(key):
                r2_problems.append("%s 缺 %s" % (m.get("row_id"), key))
        evs = m.get("evidence_paths") or []
        if not evs:
            r2_problems.append("%s.evidence_paths 为空" % m.get("row_id"))
        for p in evs:
            if not rel_exists(p):
                r2_problems.append("%s.evidence_paths 不存在: %r" % (m.get("row_id"), p))
    stats = il.get("r2_mapping_stats") or {}
    if stats.get("total") != 53:
        r2_problems.append("r2_mapping_stats.total 必须为 53")
    actual_verdicts = {}
    for m in r2:
        actual_verdicts[m.get("verdict")] = actual_verdicts.get(m.get("verdict"), 0) + 1
    if stats.get("verdict_counts") != actual_verdicts:
        r2_problems.append("r2_mapping_stats.verdict_counts 与实际不符：%r vs %r"
                           % (stats.get("verdict_counts"), actual_verdicts))
    check(not r2_problems, "interaction_ledger.r2_mapping 校验失败；问题=%s" % r2_problems)
    pending = (data.get("open_tasks") or {}).get("pending_product_review") or []
    check(isinstance(pending, list) and pending and "53" in json.dumps(pending, ensure_ascii=False),
          "open_tasks.pending_product_review 必须记录“53 项待产品决定/落点确认”")

    # ---- R2 候选 / Windows / 双环境 Linux / 验证环境 / R2 修复 ----
    fb = data.get("fact_baseline") or {}
    r2_problems = []
    rc = fb.get("r2_candidate") or {}
    for key in ("archive", "sha256", "input_manifest_sha256", "frozen_candidate", "frontend", "first_freeze_void"):
        if not rc.get(key):
            r2_problems.append("r2_candidate 缺 %s" % key)
    for key in ("sha256", "input_manifest_sha256"):
        v = rc.get(key)
        if not (isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)):
            r2_problems.append("r2_candidate.%s 必须为 64 位 hex: %r" % (key, v))
    if rc.get("file_count") != 1472 or rc.get("python_files") != 472:
        r2_problems.append("r2_candidate 必须为 1472 文件 / 472 py")
    if not rel_exists(rc.get("frozen_candidate")):
        r2_problems.append("r2_candidate.frozen_candidate 不存在: %r" % rc.get("frozen_candidate"))
    fe = rc.get("frontend") or {}
    if fe.get("build_id") != "cLb59K0OKGdDFyKUOrDRl" or fe.get("single_build_id") is not True:
        r2_problems.append("r2_candidate.frontend build_id/单一 buildId 不符")
    for p in fe.get("evidence") or []:
        if not rel_exists(p):
            r2_problems.append("r2_candidate.frontend.evidence 不存在: %r" % p)
    if "48e443d4" not in json.dumps(rc.get("first_freeze_void"), ensure_ascii=False):
        r2_problems.append("r2_candidate.first_freeze_void 必须记录首次冻结 48e443d4 作废")
    wt = fb.get("post_r2_windows") or {}
    wm2 = wt.get("manager_assets") or {}
    if not wm2.get("stats") or not rel_exists(wm2.get("stats")):
        r2_problems.append("post_r2_windows.manager_assets.stats 不存在")
    else:
        st = load_stats(wm2["stats"])
        for k, v in {"tests": 2992, "passed": 2977, "failed": 0, "errors": 0, "skipped": 15}.items():
            if wm2.get(k) != st.get(k) or wm2.get(k) != v:
                r2_problems.append("post_r2_windows.manager_assets.%s 不符：%r/%r" % (k, wm2.get(k), st.get(k)))
        if not rel_exists(wm2.get("junit")):
            r2_problems.append("post_r2_windows.manager_assets.junit 不存在")
    q2 = wt.get("qtg") or {}
    if q2.get("not_rerun") is not True:
        r2_problems.append("post_r2_windows.qtg.not_rerun 必须为 true")
    for key in ("reference_run", "reference_junit", "proof"):
        if not rel_exists(q2.get(key)):
            r2_problems.append("post_r2_windows.qtg.%s 不存在: %r" % (key, q2.get(key)))
    if not all(t in json.dumps(q2.get("reference_summary"), ensure_ascii=False) for t in ("384", "382", "2")):
        r2_problems.append("post_r2_windows.qtg.reference_summary 必须引用 R1 的 384/382/2")
    pl = fb.get("post_r2_linux") or {}
    envs = pl.get("environments") or {}
    if set(envs.keys()) != {"local_qemu_vm", "cloud_native"}:
        r2_problems.append("post_r2_linux.environments 必须为 local_qemu_vm/cloud_native")
    for env_key, env in envs.items():
        for target, expect in (("manager_assets", None), ("qtg", None)):
            item = env.get(target) or {}
            sp = item.get("stats")
            if not sp or not rel_exists(sp):
                r2_problems.append("post_r2_linux.%s.%s.stats 不存在" % (env_key, target))
                continue
            st = load_stats(sp)
            for k in ("tests", "passed", "failed", "errors", "skipped"):
                if item.get(k) is not None and item.get(k) != st.get(k):
                    r2_problems.append("post_r2_linux.%s.%s.%s 与 stats 不符" % (env_key, target, k))
            for key in ("junit",):
                if not rel_exists(item.get(key)):
                    r2_problems.append("post_r2_linux.%s.%s.%s 不存在" % (env_key, target, key))
    vm = envs.get("local_qemu_vm") or {}
    cl = envs.get("cloud_native") or {}
    if (vm.get("manager_assets") or {}).get("passed") != 2980 or (vm.get("qtg") or {}).get("passed") != 384:
        r2_problems.append("VM Linux 必须为 manager 2980/2F、qtg 384/0F")
    if (cl.get("manager_assets") or {}).get("passed") != 2982 or (cl.get("qtg") or {}).get("passed") != 383:
        r2_problems.append("云 Linux 必须为 manager 2982/0F、qtg 383/1F")
    ce = pl.get("cross_environment") or {}
    if ce.get("non_overlapping_failures") is not True or ce.get("failure_count") != 3:
        r2_problems.append("post_r2_linux 必须记录 3 条互不重叠失败")
    check(not r2_problems, "R2 候选/门禁校验失败；问题=%s" % r2_problems)

    # ---- 验证环境（verification_environments） ----
    ve = data.get("verification_environments") or {}
    ve_items = ve.get("items") or []
    vep = []
    if not isinstance(ve_items, list) or {x.get("id") for x in ve_items} != {"local_qemu_vm", "cloud_native"}:
        vep.append("verification_environments.items 必须为 local_qemu_vm/cloud_native 两项")
    for item in ve_items:
        for key in ("name", "purpose", "spec", "full_run", "isolation", "constraints", "measured_impact", "tradeoffs"):
            if not item.get(key):
                vep.append("%s 缺 %s" % (item.get("id"), key))
        for p in item.get("evidence_dirs") or []:
            if not rel_exists(p):
                vep.append("%s.evidence_dirs 不存在: %r" % (item.get("id"), p))
    cloud = next((x for x in ve_items if x.get("id") == "cloud_native"), {})
    if "不得触碰 /opt" not in json.dumps(cloud.get("constraints"), ensure_ascii=False):
        vep.append("cloud_native.constraints 必须写“不得触碰 /opt”")
    impact = cloud.get("measured_impact") or {}
    if impact.get("healthz") != 200 or impact.get("services_active") != 4:
        vep.append("cloud_native.measured_impact 必须记录 4 服务 active / healthz 200")
    check(not vep, "verification_environments 校验失败；问题=%s" % vep)

    # ---- R2 修复（K37/K38） ----
    r2fx = fb.get("r2_fixes") or []
    r2fp = []
    if [x.get("id") for x in r2fx] != ["R2-A", "R2-B"]:
        r2fp.append("r2_fixes 必须为 R2-A/R2-B")
    for fx in r2fx:
        if fx.get("task") not in ("K37", "K38"):
            r2fp.append("%s.task 非法: %r" % (fx.get("id"), fx.get("task")))
        for key in ("title", "files", "evidence_paths", "tests", "residual_risk", "linux_status"):
            if not fx.get(key):
                r2fp.append("%s 缺 %s" % (fx.get("id"), key))
        for p in fx.get("evidence_paths") or []:
            if not rel_exists(p):
                r2fp.append("%s.evidence_paths 不存在: %r" % (fx.get("id"), p))
    check(not r2fp, "r2_fixes 校验失败；问题=%s" % r2fp)

    # ---- R3 执行基线 ----
    r3 = fb.get("r3") or {}
    r3p = []
    for key in ("doc", "inventory", "gaps"):
        if not rel_exists(r3.get(key)):
            r3p.append("r3.%s 不存在: %r" % (key, r3.get(key)))
    dec = r3.get("decisions") or {}
    if (dec.get("instance_lease") or {}).get("option") != "B":
        r3p.append("r3.decisions.instance_lease 必须为已批准方案 B")
    if (dec.get("rpo_rto_candidate_thresholds") or {}).get("status") != "pending_product_confirmation":
        r3p.append("r3.decisions.rpo_rto 状态必须 pending_product_confirmation")
    if (dec.get("k26_default_switch") or {}).get("batch") != "R4":
        r3p.append("r3.decisions.k26_default_switch 必须归 R4")
    if [b.get("id") for b in r3.get("batches") or []] != ["R3-L1", "R3-L2", "R3-L3", "R3-L4", "R3-L5", "R3-L6"]:
        r3p.append("r3.batches 必须为 L1–L6")
    for b in r3.get("batches") or []:
        for p in b.get("evidence") or []:
            if not rel_exists(p):
                r3p.append("%s.evidence 不存在: %r" % (b.get("id"), p))
    prog = r3.get("progress") or {}
    for name, item in prog.items():
        for p in item.get("evidence") or []:
            if not rel_exists(p):
                r3p.append("r3.progress.%s.evidence 不存在: %r" % (name, p))
    # R3 批次完成状态：L1/L2/L3/L5/L6 completed，L4 merged_into_L3
    bst = {b.get("id"): b.get("status") for b in r3.get("batches") or []}
    expect_bst = {"R3-L1": "completed", "R3-L2": "completed", "R3-L3": "completed",
                  "R3-L4": "merged_into_L3", "R3-L5": "completed", "R3-L6": "completed"}
    if bst != expect_bst:
        r3p.append("r3.batches 完成状态不符：%r" % bst)
    pst = {k: (v or {}).get("status") for k, v in prog.items()}
    expect_pst = {"R3-L1": "completed", "R3-L2": "completed", "R3-L3": "completed",
                  "R3-L4": "merged_into_L3", "R3-L5": "completed", "R3-L6": "completed"}
    if pst != expect_pst:
        r3p.append("r3.progress 完成状态不符：%r" % pst)
    if (dec.get("rpo_rto_candidate_thresholds") or {}).get("measured", {}).get("rpo_s") != 22.635:
        r3p.append("r3.decisions.rpo_rto.measured 必须记录 RPO 22.635s")
    check(not r3p, "r3 校验失败；问题=%s" % r3p)
    # R3 修复（逐批 files/evidence/tests/conclusion/uncovered）
    r3fx = fb.get("r3_fixes") or []
    r3fp = []
    if [x.get("id") for x in r3fx] != ["R3-L1", "R3-L2", "R3-L3", "R3-L5", "R3-L6"]:
        r3fp.append("r3_fixes 必须覆盖 L1/L2/L3/L5/L6")
    for fx in r3fx:
        for key in ("batch", "task", "title", "files", "evidence_paths", "tests", "conclusion", "uncovered"):
            if not fx.get(key):
                r3fp.append("%s 缺 %s" % (fx.get("id"), key))
        for p in fx.get("evidence_paths") or []:
            if not rel_exists(p):
                r3fp.append("%s.evidence_paths 不存在: %r" % (fx.get("id"), p))
        if fx.get("id") == "R3-L2" and not any(x in json.dumps(fx.get("tests"), ensure_ascii=False) for x in ("11484", "11 484")):
            r3fp.append("R3-L2.tests 必须记录 T_admission 11484ms")
        if fx.get("id") == "R3-L3" and not any("22.635" in json.dumps(t, ensure_ascii=False) for t in fx.get("tests") or []):
            r3fp.append("R3-L3.tests 必须记录 RPO 22.635s")
    check(not r3fp, "r3_fixes 校验失败；问题=%s" % r3fp)
    # 验证环境的 R3 用法
    ve2 = data.get("verification_environments") or {}
    vep2 = []
    for item in ve2.get("items") or []:
        if not item.get("r3_usage"):
            vep2.append("%s 缺 r3_usage" % item.get("id"))
    topo = ve2.get("r3_topology") or {}
    for key in ("source", "transport", "destination", "key_custody", "resource_limit", "rto_rule"):
        if not topo.get(key):
            vep2.append("r3_topology 缺 %s" % key)
    for p in topo.get("evidence") or []:
        if not rel_exists(p):
            vep2.append("r3_topology.evidence 不存在: %r" % p)
    check(not vep2, "verification_environments.R3 校验失败；问题=%s" % vep2)
    # RV02/03/04 保持 open，note 必须写明“本地可验证部分已完成，剩余为外部条件”
    defs = {d.get("id"): d for d in data.get("defects") or []}
    for did in ("RV02", "RV03", "RV04"):
        d = defs.get(did) or {}
        if d.get("status") != "open":
            check(False, "%s 必须保持 open（不得升级 fixed）" % did)
        elif "本地可验证部分已完成，剩余为外部条件" not in (d.get("note") or ""):
            check(False, "%s.note 必须写“本地可验证部分已完成，剩余为外部条件”" % did)
        else:
            check(True, "%s open 且 note 已标注本地/外部边界" % did)
    # 工程项新条目
    eng2 = (data.get("open_tasks") or {}).get("engineering_items") or []
    for token in ("测试硬编码生产路径", "双环境", "S1", "48e443d4",
                  "三条同源测量错误", "证据只落指纹", "B 未 ready"):
        check(token in json.dumps(eng2, ensure_ascii=False),
              "open_tasks.engineering_items 必须记录 %r 工程项" % token)
    # 统一交接文档指向（Lead 2026-10-25 新增）
    hd = data.get("handoff") or {}
    check(hd.get("doc") == "HANDOVER-R0R3-20261025.md" and rel_exists(hd.get("doc")),
          "status.json.handoff.doc 必须指向存在的 HANDOVER-R0R3-20261025.md")
    check(hd.get("summary") == "R0–R3 收口交接；R4 待授权",
          "status.json.handoff.summary 必须为“R0–R3 收口交接；R4 待授权”")
    with open(STATUS_R0, encoding="utf-8") as _fh:
        _status_doc = _fh.read()
    check("HANDOVER-R0R3-20261025.md" in _status_doc,
          "STATUS-R0.md 顶部必须指向 HANDOVER-R0R3-20261025.md")

    # ---- fact_baseline 与 evidence/runs 实际文件一致 ----
    fb = data.get("fact_baseline") or {}
    wm = fb.get("windows_manager_assets") or {}
    wq = fb.get("windows_qtg") or {}
    for label, item in (("windows_manager_assets", wm), ("windows_qtg", wq)):
        stats_path = item.get("stats")
        if not stats_path or not rel_exists(stats_path):
            fail("%s.stats 缺失或路径不存在: %r" % (label, stats_path))
            continue
        st = load_stats(stats_path)
        for key in ("tests", "passed", "failed", "skipped"):
            check(item.get(key) == st.get(key),
                  "%s.%s 与 stats 文件不一致：台账=%r 文件=%r"
                  % (label, key, item.get(key), st.get(key)))
    check(wm.get("tests") == 2922 and wm.get("passed") == 2907 and wm.get("failed") == 0 and wm.get("skipped") == 15,
          "Windows Manager 基线必须是 2922/2907/0/15")
    check(wq.get("tests") == 344 and wq.get("passed") == 342 and wq.get("failed") == 2,
          "QTG 基线必须是 344/342/2（2 为 Windows POSIX 权限断言）")
    check("POSIX" in json.dumps(wq.get("known_failures"), ensure_ascii=False),
          "QTG 失败必须注明是 Windows POSIX 权限断言")
    lm = fb.get("linux_manager_old_snapshot") or {}
    check(lm.get("status") == "expired", "Linux 旧快照必须标 expired（RV06）")
    prod = fb.get("production") or {}
    check(prod.get("changed") is False, "production.changed 必须为 false")
    check(prod.get("release_status") == "not_deployed", "生产发布状态必须为 not_deployed（本地候选未部署）")
    check(data.get("production_changed") is False, "顶层 production_changed 必须为 false")

    # ---- R0 修复清单占位/填充 ----
    r0_fixes = fb.get("r0_fixes")
    check(isinstance(r0_fixes, list), "fact_baseline.r0_fixes 必须是列表（占位时可为 []）")
    check(bool(fb.get("r0_fixes_note")), "fact_baseline.r0_fixes_note 必须说明占位/填充规则")
    if isinstance(r0_fixes, list) and r0_fixes:
        rf_problems = []
        fixed_ids = {d.get("id") for d in defects if d.get("status") == "fixed"}
        for i, fx in enumerate(r0_fixes):
            if not isinstance(fx, dict):
                rf_problems.append("r0_fixes[%d] 不是对象" % i)
                continue
            if fx.get("id") not in fixed_ids:
                rf_problems.append("r0_fixes[%d].id=%r 不是已 fixed 的缺陷" % (i, fx.get("id")))
            for key in ("defect", "fix_task", "files", "evidence_paths", "tests",
                        "residual_risk", "linux_status"):
                if not fx.get(key):
                    rf_problems.append("r0_fixes[%d] 缺 %s" % (i, key))
            for p in fx.get("evidence_paths") or []:
                if not rel_exists(p):
                    rf_problems.append("r0_fixes[%d].evidence_paths 不存在: %r" % (i, p))
            if "双平台" not in str(fx.get("linux_status") or ""):
                rf_problems.append("r0_fixes[%d].linux_status 必须写“双平台门禁通过”" % i)
        check(not rf_problems, "r0_fixes 校验失败；问题=%s" % rf_problems)

    # ---- RV06 关闭要求（本地不可变候选闭环） ----
    rv06 = next((d for d in defects if d.get("id") == "RV06"), None)
    if rv06 is not None:
        check(rv06.get("status") == "fixed", "RV06 应已关闭（status=fixed）")
        check(bool(rv06.get("fix_evidence_paths")), "RV06 fixed 必须挂 fix_evidence_paths")
        for p in rv06.get("fix_evidence_paths") or []:
            check(rel_exists(p), "RV06.fix_evidence_paths 不存在: %r" % p)

    # ---- R0 修复后的 Windows 门禁结果（若已填） ----
    post = fb.get("post_r0_windows")
    if post:
        pp = []
        for label, key in (("manager_assets", "post_r0_windows.manager_assets"), ("qtg", "post_r0_windows.qtg")):
            item = post.get(label) or {}
            sp = item.get("stats")
            if not sp or not rel_exists(sp):
                pp.append("%s.stats 缺失或不存在: %r" % (key, sp))
                continue
            st = load_stats(sp)
            for f in ("tests", "passed", "failed", "skipped"):
                if item.get(f) != st.get(f):
                    pp.append("%s.%s 与 stats 不一致：台账=%r 文件=%r"
                              % (key, f, item.get(f), st.get(f)))
        check(not pp, "post_r0_windows 校验失败；问题=%s" % pp)
        check("Linux" in json.dumps(post.get("note"), ensure_ascii=False),
              "post_r0_windows.note 必须说明 Linux 全量未完成、只算 Windows 门禁")

    # ---- R0 / R1 候选冻结记录 ----
    def check_candidate(label, cand, expect_files, expect_py):
        probs = []
        if not isinstance(cand, dict):
            fail("%s 必须是对象" % label)
            return
        for key in ("archive", "sha256", "input_manifest_sha256", "frozen_candidate",
                    "file_count", "python_files", "python_fingerprint"):
            if not cand.get(key):
                probs.append("%s 缺 %s" % (label, key))
        for key in ("sha256", "input_manifest_sha256", "python_fingerprint"):
            v = cand.get(key)
            if not (isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)):
                probs.append("%s.%s 必须为 64 位 hex: %r" % (label, key, v))
        if cand.get("file_count") != expect_files:
            probs.append("%s.file_count 期望 %d 实际 %r" % (label, expect_files, cand.get("file_count")))
        if cand.get("python_files") != expect_py:
            probs.append("%s.python_files 期望 %d 实际 %r" % (label, expect_py, cand.get("python_files")))
        if not rel_exists(cand.get("frozen_candidate")):
            probs.append("%s.frozen_candidate 不存在: %r" % (label, cand.get("frozen_candidate")))
        check(not probs, "%s 校验失败；问题=%s" % (label, probs))

    r0c = fb.get("r0_candidate")
    check_candidate("fact_baseline.r0_candidate", r0c, 1557, 468)
    if isinstance(r0c, dict):
        tree = r0c.get("tree_static_proof") or {}
        tp = []
        if tree.get("head") != "b53f36e0d5a66e49470c528dcab8a3e8cfc34b90":
            tp.append("tree_static_proof.head 必须为 b53f36e…")
        if tree.get("tracked_modified") != 60 or tree.get("untracked") != 94:
            tp.append("tree_static_proof 必须记录 60 modified / 94 untracked")
        if not (isinstance(tree.get("diff_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", tree.get("diff_sha256"))):
            tp.append("tree_static_proof.diff_sha256 必须为 64 位 hex")
        if not rel_exists(tree.get("evidence")):
            tp.append("tree_static_proof.evidence 不存在: %r" % tree.get("evidence"))
        check(not tp, "r0_candidate.tree_static_proof 校验失败；问题=%s" % tp)
        inst = r0c.get("install_verification") or {}
        ip = []
        for key in ("pre_restart", "post_restart"):
            if not rel_exists(inst.get(key)):
                ip.append("install_verification.%s 不存在: %r" % (key, inst.get(key)))
        if inst.get("pre_restart_verified") is not True:
            ip.append("install_verification.pre_restart_verified 必须为 true")
        if "missing=0" not in json.dumps(inst.get("post_restart_result"), ensure_ascii=False):
            ip.append("install_verification.post_restart_result 必须写明 missing=0")
        check(not ip, "r0_candidate.install_verification 校验失败；问题=%s" % ip)
        check((r0c.get("linux_full") or {}).get("status") in ("conditional_pass", "rerun_in_progress", "pending", "completed"),
              "r0_candidate.linux_full.status 非法")

    r1c = fb.get("r1_candidate")
    check_candidate("fact_baseline.r1_candidate", r1c, 1563, 471)

    # ---- incidents ----
    incidents = data.get("incidents") or {}
    ic_problems = []
    if not isinstance(incidents, dict) or not incidents.get("schema"):
        ic_problems.append("incidents 必须是带 schema 的对象")
    items = (incidents or {}).get("items") if isinstance(incidents, dict) else None
    if not isinstance(items, list) or not items:
        ic_problems.append("incidents.items 必须是非空列表")
    else:
        for it in items:
            for key in ("id", "severity", "occurred_at", "affected_task", "summary",
                        "impact", "recovery", "production_implication"):
                if not it.get(key):
                    ic_problems.append("%s 缺 %s" % (it.get("id"), key))
            evs = it.get("evidence_paths") or []
            if not evs:
                ic_problems.append("%s.evidence_paths 为空" % it.get("id"))
            for p in evs:
                if not rel_exists(p):
                    ic_problems.append("%s.evidence_paths 不存在: %r" % (it.get("id"), p))
            if it.get("used_as_formal_evidence") is not False:
                ic_problems.append("%s.used_as_formal_evidence 必须为 false（事故证据不作正式结果）" % it.get("id"))
            if "integrity_after_restart" in it:
                integ = it.get("integrity_after_restart") or {}
                if integ.get("missing") != 0 or integ.get("modified") != 0:
                    ic_problems.append("%s.integrity_after_restart 必须 missing=0/modified=0" % it.get("id"))
                if integ.get("extra") != 1:
                    ic_problems.append("%s.integrity_after_restart.extra 必须记录 1 个运行产物" % it.get("id"))
    check(not ic_problems, "incidents 校验失败；问题=%s" % ic_problems)
    check(isinstance(items, list) and len(items) >= 2,
          "incidents 必须同时记录 LAB-VM-DROP 与 LAB-HUNG-TEST 两次事故")

    # ---- R1 修复区块 ----
    r1_fixes = fb.get("r1_fixes") or []
    rf_problems = []
    if not isinstance(r1_fixes, list) or len(r1_fixes) != 2:
        rf_problems.append("r1_fixes 必须为 2 项（R1-A/K26、R1-B/K28）")
    else:
        got_ids = [x.get("id") for x in r1_fixes]
        if got_ids != ["R1-A", "R1-B"]:
            rf_problems.append("r1_fixes id 必须为 R1-A/R1-B；实际=%s" % got_ids)
        for fx in r1_fixes:
            if fx.get("fix_task") not in ("task-6", "task-7"):
                rf_problems.append("%s.fix_task 非法: %r" % (fx.get("id"), fx.get("fix_task")))
            if fx.get("task") not in EXPECTED_K:
                rf_problems.append("%s.task 引用非法任务 id: %r" % (fx.get("id"), fx.get("task")))
            for key in ("title", "files", "evidence_paths", "tests", "residual_risk", "linux_status"):
                if not fx.get(key):
                    rf_problems.append("%s 缺 %s" % (fx.get("id"), key))
            for p in fx.get("evidence_paths") or []:
                if not rel_exists(p):
                    rf_problems.append("%s.evidence_paths 不存在: %r" % (fx.get("id"), p))
            ls = str(fx.get("linux_status") or "")
            if "双平台" not in ls or "31/31" not in ls:
                rf_problems.append("%s.linux_status 必须写“双平台门禁通过 + K28 Linux 31/31”" % fx.get("id"))
    check(not rf_problems, "r1_fixes 校验失败；问题=%s" % rf_problems)

    # ---- R1 修复后的 Windows 门禁结果 ----
    post1 = fb.get("post_r1_windows")
    if not post1:
        fail("fact_baseline.post_r1_windows 缺失（R1 Windows 门禁数字必须入账）")
    else:
        p1 = []
        for label, key in (("manager_assets", "post_r1_windows.manager_assets"), ("qtg", "post_r1_windows.qtg")):
            item = post1.get(label) or {}
            sp = item.get("stats")
            if not sp or not rel_exists(sp):
                p1.append("%s.stats 缺失或不存在: %r" % (key, sp))
                continue
            st = load_stats(sp)
            for f in ("tests", "passed", "failed", "skipped"):
                if item.get(f) != st.get(f):
                    p1.append("%s.%s 与 stats 不一致：台账=%r 文件=%r"
                              % (key, f, item.get(f), st.get(f)))
        check(not p1, "post_r1_windows 校验失败；问题=%s" % p1)
        check("Linux" in json.dumps(post1.get("note"), ensure_ascii=False),
              "post_r1_windows.note 必须说明 Linux 全量未完成、只算 Windows 门禁")
        check((post1.get("manager_assets") or {}).get("run_id") == "20261025-143134-d9c3086c",
              "post_r1_windows.manager_assets.run_id 必须为 20261025-143134-d9c3086c")
        check((post1.get("qtg") or {}).get("run_id") == "20261025-140419-103c0350",
              "post_r1_windows.qtg.run_id 必须为 20261025-140419-103c0350")

    # ---- R0 Linux 门禁结果（suite-run 20261025T062946Z） ----
    post0 = fb.get("post_r0_linux")
    if not post0:
        fail("fact_baseline.post_r0_linux 缺失（R0 Linux 门禁必须入账）")
    else:
        p0 = []
        for label, key, expect in (
                ("manager_assets", "post_r0_linux.manager_assets",
                 {"tests": 2944, "passed": 2931, "failed": 3, "errors": 0, "skipped": 10}),
                ("qtg", "post_r0_linux.qtg",
                 {"tests": 367, "passed": 367, "failed": 0, "errors": 0, "skipped": 0})):
            item = post0.get(label) or {}
            sp = item.get("stats")
            if not sp or not rel_exists(sp):
                p0.append("%s.stats 缺失或不存在: %r" % (key, sp))
                continue
            st = load_stats(sp)
            for f in ("tests", "passed", "failed", "errors", "skipped"):
                if item.get(f) != st.get(f) or item.get(f) != expect[f]:
                    p0.append("%s.%s 期望 %r 台账 %r 文件 %r"
                              % (key, f, expect[f], item.get(f), st.get(f)))
            jp = item.get("junit")
            if not jp or not rel_exists(jp):
                p0.append("%s.junit 缺失或不存在: %r" % (key, jp))
        check(not p0, "post_r0_linux 校验失败；问题=%s" % p0)
        check(post0.get("suite_run") == "20261025T062946Z",
              "post_r0_linux.suite_run 必须为 20261025T062946Z")
        check("有条件通过" in str(post0.get("conclusion") or ""),
              "post_r0_linux.conclusion 必须写“有条件通过”，不得写全绿")
        check(len(post0.get("failures") or []) == 3,
              "post_r0_linux.failures 必须原样保留 3 条")
        check(post0.get("wall_s") == 3139.78, "post_r0_linux.wall_s 必须为 3139.78")
        ed = post0.get("evidence_dir")
        check(bool(ed) and rel_exists(ed), "post_r0_linux.evidence_dir 不存在: %r" % ed)

    # ---- R1 Linux 门禁结果（等价覆盖，suite-run 20261025T072810Z） ----
    post2 = fb.get("post_r1_linux")
    if not post2:
        fail("fact_baseline.post_r1_linux 缺失（R1 Linux 门禁必须入账）")
    else:
        p2 = []
        q = post2.get("qtg") or {}
        if not q.get("stats") or not rel_exists(q.get("stats")):
            p2.append("post_r1_linux.qtg.stats 缺失或不存在")
        else:
            st = load_stats(q["stats"])
            for f, expect in {"tests": 384, "passed": 384, "failed": 0, "errors": 0, "skipped": 0}.items():
                if q.get(f) != st.get(f) or q.get(f) != expect:
                    p2.append("post_r1_linux.qtg.%s 期望 %r 台账 %r 文件 %r" % (f, expect, q.get(f), st.get(f)))
        if not q.get("junit") or not rel_exists(q.get("junit")):
            p2.append("post_r1_linux.qtg.junit 缺失或不存在")
        k28 = post2.get("k28_linux") or {}
        if not k28.get("evidence") or not rel_exists(k28.get("evidence")):
            p2.append("post_r1_linux.k28_linux.evidence 缺失或不存在")
        else:
            kd = json.load(open(os.path.join(REPO, k28["evidence"].replace("/", os.sep)), encoding="utf-8"))
            rows = kd.get("rows") or []
            if len(rows) != 31 or not all(r.get("ok") for r in rows):
                p2.append("k28 Linux 必须 31 行且全 ok")
            if k28.get("passed") != 31 or k28.get("failed") != 0:
                p2.append("post_r1_linux.k28_linux 必须为 31 passed / 0 failed")
        ma = post2.get("manager_assets") or {}
        if not ma.get("merged") or not rel_exists(ma.get("merged")):
            p2.append("post_r1_linux.manager_assets.merged 缺失或不存在")
        else:
            md = json.load(open(os.path.join(REPO, ma["merged"].replace("/", os.sep)), encoding="utf-8"))
            tot = md.get("totals") or {}
            for k, expect in {"cases": 2983, "passed": 2970, "failed": 3, "error": 0, "skipped": 10}.items():
                if ma.get(k) != tot.get(k) or ma.get(k) != expect:
                    p2.append("post_r1_linux.manager_assets.%s 期望 %r 台账 %r 文件 %r"
                              % (k, expect, ma.get(k), tot.get(k)))
            if tot.get("cases") != 1773 + 313 + 897:
                p2.append("manager-assets 数量对账 1773+313+897 != 2983")
        if ma.get("not_a_full_single_run") is not True:
            p2.append("post_r1_linux.manager_assets.not_a_full_single_run 必须为 true")
        check(not p2, "post_r1_linux 校验失败；问题=%s" % p2)
        check(post2.get("suite_run") == "20261025T072810Z",
              "post_r1_linux.suite_run 必须为 20261025T072810Z")
        check("有条件通过" in str(post2.get("conclusion") or ""),
              "post_r1_linux.conclusion 必须写“有条件通过”")
        check(len(post2.get("failures") or []) == 3, "post_r1_linux.failures 必须保留 3 条")
        ed2 = post2.get("evidence_dir")
        check(bool(ed2) and rel_exists(ed2), "post_r1_linux.evidence_dir 不存在: %r" % ed2)

    # ---- R1 候选 linux_full 已完成（非单次全量） ----
    lf1 = (fb.get("r1_candidate") or {}).get("linux_full") or {}
    check(lf1.get("status") == "conditional_pass", "r1_candidate.linux_full.status 必须为 conditional_pass")
    check("有条件通过" in str(lf1.get("conclusion") or ""), "r1_candidate.linux_full.conclusion 必须写“有条件通过”")
    check((lf1.get("manager_assets") or {}).get("not_a_full_single_run") is True,
          "r1_candidate.linux_full.manager_assets.not_a_full_single_run 必须为 true")
    check((lf1.get("manager_assets") or {}).get("tests") == 2983
          and (lf1.get("manager_assets") or {}).get("passed") == 2970
          and (lf1.get("manager_assets") or {}).get("failed") == 3,
          "r1_candidate.linux_full.manager_assets 必须为 2983/2970/3")
    check((lf1.get("k28_linux") or {}).get("passed") == 31, "r1_candidate.linux_full.k28_linux 必须 31/31")
    check((lf1.get("qtg") or {}).get("passed") == 384, "r1_candidate.linux_full.qtg 必须 384/384")
    check(len(lf1.get("failures") or []) == 3, "r1_candidate.linux_full.failures 必须保留 3 条")
    for p in lf1.get("evidence_paths") or []:
        check(rel_exists(p), "r1_candidate.linux_full.evidence_paths 不存在: %r" % p)

    # ---- 工程改进项（不进验收计数） ----
    eng = (data.get("open_tasks") or {}).get("engineering_items")
    check(isinstance(eng, list) and len(eng) >= 1, "open_tasks.engineering_items 必须为非空列表")
    check("慢环境测试阈值" in json.dumps(eng, ensure_ascii=False),
          "open_tasks.engineering_items 必须记录“慢环境测试阈值”工程项")
    for token in ("测试隔离守卫", "pnpm-workspace.yaml", "干净单次"):
        check(token in json.dumps(eng, ensure_ascii=False),
              "open_tasks.engineering_items 必须记录 %r 工程项" % token)

    # ---- R0 候选 linux_full 已完成 ----
    r0c2 = fb.get("r0_candidate") or {}
    lf = r0c2.get("linux_full") or {}
    check(lf.get("status") == "conditional_pass", "r0_candidate.linux_full.status 必须为 conditional_pass")
    check("有条件通过" in str(lf.get("conclusion") or ""), "linux_full.conclusion 必须写“有条件通过”")
    check("非 R0 代码回归" in str(lf.get("conclusion") or ""), "linux_full.conclusion 必须写明“非 R0 代码回归”")
    check(lf.get("suite_run") == "20261025T062946Z", "linux_full.suite_run 必须为 20261025T062946Z")
    check(lf.get("source_match") is True, "linux_full.source_match 必须为 true")
    check((lf.get("manager_assets") or {}).get("passed") == 2931
          and (lf.get("manager_assets") or {}).get("failed") == 3,
          "linux_full.manager_assets 必须为 2944/2931/3/0/10")
    check((lf.get("qtg") or {}).get("tests") == 367 and (lf.get("qtg") or {}).get("passed") == 367,
          "linux_full.qtg 必须为 367/367/0/0/0")
    check(len(lf.get("failures") or []) == 3, "linux_full.failures 必须保留 3 条")
    for p in lf.get("evidence_paths") or []:
        check(rel_exists(p), "linux_full.evidence_paths 不存在: %r" % p)

    # ---- 批次表 ----
    batches = data.get("r0_batches") or []
    got = [b.get("batch") for b in batches]
    check(got == ["R0", "R1", "R2", "R3", "R4"], "r0_batches 必须为 R0–R4；实际=%s" % got)

    # ---- 证据补救账目自洽 ----
    rem_block = data.get("evidence_index_remediation") or {}
    remediated = set((rem_block.get("after") or {}).get("remediated_with_real_paths") or [])
    unavailable = set((rem_block.get("after") or {}).get("no_evidence_remaining") or [])
    actual_remediated = {t["id"] for t in tasks if t.get("evidence_status") == "remediated"}
    actual_unavailable = {t["id"] for t in tasks if t.get("evidence_status") == "unavailable"}
    check(remediated == actual_remediated,
          "evidence_index_remediation.remediated_with_real_paths 与实际不符：多=%s 少=%s"
          % (sorted(remediated - actual_remediated), sorted(actual_remediated - remediated)))
    check(unavailable == actual_unavailable,
          "evidence_index_remediation.no_evidence_remaining 与实际不符：多=%s 少=%s"
          % (sorted(unavailable - actual_unavailable), sorted(actual_unavailable - unavailable)))
    before = rem_block.get("before") or {}
    check(before.get("tasks_with_empty_evidence") == 34, "改造前空 evidence 任务数必须为 34")
    check(before.get("with_local_acceptance_complete_true") == 31,
          "改造前 local_acceptance_complete=true 且 evidence 为空的任务必须为 31")

    # ---- 人读摘要 ----
    if check(os.path.exists(STATUS_R0), "tasks/STATUS-R0.md 必须存在"):
        with open(STATUS_R0, encoding="utf-8") as fh:
            doc = fh.read()
        for token in ("2922", "2907", "344", "342", "e9f8e324",
                      "evidence/runs/20261025-060123-6d5efeb0/stats-manager-assets.json",
                      "evidence/runs/20261025-055319-f0464aee/stats-qtg.json",
                      "141", "not_deployed", "RV01", "RV08",
                      "workspace-d098141916470a90", "cbf030493d6f09b1",
                      "workspace-eb087b22acb347be", "12e8bd03",
                      "2983", "2968", "384", "382", "QEMU", "LAB-VM-DROP-20261025T141540+0800",
                      "20261025T062946Z", "2931", "有条件通过", "52 min",
                      "20261025T072810Z", "LAB-HUNG-TEST-20261025T081540+0800",
                      "2970", "1563", "31/31",
                      "2992", "2982", "2980", "197.55", "2709.88", "cloud_native",
                      "verification_environments",
                      "instance_leases", "11 484", "22.635", "124.634", "74.5", "71.6",
                      "observed_no_outage", "3014", "L4 并入"):
            check(token in doc, "STATUS-R0.md 必须引用 %r" % token)

    # ---- 输出 ----
    if errors:
        print("FAIL: %d 项校验失败（共 %d 项检查）" % (len(errors), len(errors) + checks))
        for i, e in enumerate(errors, 1):
            print("  %2d) %s" % (i, e))
        return 1
    if not quiet:
        print("PASS: status_lint 全部通过（%d 项检查）" % (checks + len(errors)))
        print("  tasks=52 preparations=4 defects=8 evidence_paths 全部存在；"
              "三轴非 null 且在枚举内；交互 5 类合计 209；基线数字与 stats 文件一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())





