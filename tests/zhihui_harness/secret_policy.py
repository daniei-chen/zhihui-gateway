"""门禁共享的"合成/真实凭据"判定口径（R3-L6）。

为什么单独一个模块：`gate_checks.py --secrets`（历史敏感值扫描）与
`tests/linux_lab/scan_secret_plaintext.py`（R3-L6 明文扫描）必须用**同一套**
合成分类，否则两套标准必然漂移（例如同一个 fixture 值一个门禁放行、另一个报红）。

口径（三条硬约束，修改前先读）：
1. **只豁免"证据目录里由 fixture 生成的合成值"**：路径必须命中
   `EVIDENCE_WHITELIST`（例如 `evidence/browser/ui1*`、`evidence/browser/k38*`、
   `evidence/r2-*`、`evidence/r3-*` 下的 report/json），**并且**值本身带合成标记，
   或所在文件明确声明 `production_connected: false` / `synthetic` / `fixture` 等上下文；
2. **源码与配置一律不豁免**：`manager/`、`qtg/`、`src/`、`ops/`、`tests/`、`tasks/`、
   `contracts/`、`baseline/`、`design/`、`recovery/`、`release/` 下的命中永远算失败；
3. **白名单必须白盒可审计**：每条规则都有 reason，新增/放宽必须在此文件说明理由，
   不允许"按文件后缀整体放行"或"证据目录全放行"。
"""
from __future__ import annotations

import re

# 与 scan_secret_plaintext.py 共用的合成/占位标记（值级）。
SYNTHETIC_MARKERS: tuple[str, ...] = (
    'synthetic', 'fixture', 'placeholder', 'example', 'dummy', 'redacted',
    'changeme', 'your-', 'your_', 'token-here', 'test-token', 'test_token',
    'abcdef', 'xxxx', '****', '<token>', '${', '$(',
    # 测试/文档里常见的显式"假值"写法
    'not-a-real', 'not_real', 'notreal', 'legacy-token', 'expired', 'forged',
    'invalid', 'fake', 'abcd', 'demo', 'sample', 'harness', '-for-test',
    'for-test', 'for_test', 'dummy-value',
)

# S1（2026-10-25）：dev 探针里的疑似真实 wbk_ 旧值。若在快照/旧包里再出现，
# 必须单独标记为 known-s1（不能归 synthetic）。
KNOWN_S1_SHA16 = '5e103bca9f6d53e8'

# 证据目录的白名单（只有这些路径的"报告类文件"允许走合成豁免）。
# 每条 = (正则, 理由)。路径用仓库相对路径（/ 分隔）。
EVIDENCE_WHITELIST: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'^evidence/browser/(?:ui1|k38)[^/]*/[^/]*\.json$'),
     '浏览器 fixture 报告（ui1*/k38*）：由测试脚本生成，production_connected=false'),
    (re.compile(r'^evidence/r2-[^/]+/[^/]*\.(?:json|ndjson|log)$'),
     'R2 实验/浏览器证据：fixture 数据，报告类文件'),
    (re.compile(r'^evidence/r3-[^/]+/[^/]*\.(?:json|ndjson|log)$'),
     'R3 实验/浏览器证据：fixture 数据，报告类文件'),
    (re.compile(r'^evidence/browser/[^/]+/(?:report|report-[^/]*)\.json$'),
     '浏览器报告（任意批次）：仅 report*.json，且仍需 fixture 上下文'),
)

# 源码/配置等"永不豁免"的根（显式列出，防止以后白名单写宽）。
NEVER_EXEMPT_ROOTS = (
    'manager/', 'qtg/', 'src/', 'ops/', 'tests/', 'tasks/', 'contracts/',
    'baseline/', 'design/', 'recovery/', 'release/',
)

# 文件级 fixture 上下文：证明"这是测试报告，不是业务配置"。
_CONTEXT_PATTERNS = (
    re.compile(r'"production_connected"\s*:\s*false'),
    re.compile(r'(?i)"?(synthetic|fixture|fake)"?\s*[:=]'),
)

_VALUE_RE = re.compile(r'[A-Za-z0-9._~+/=-]{8,}')


def looks_synthetic_value(value: str) -> bool:
    low = (value or '').lower()
    return any(marker in low for marker in SYNTHETIC_MARKERS)


def file_has_fixture_context(text: str) -> bool:
    return any(pattern.search(text or '') for pattern in _CONTEXT_PATTERNS)


def evidence_whitelist_reason(rel_path: str) -> str:
    for pattern, reason in EVIDENCE_WHITELIST:
        if pattern.match(rel_path):
            return reason
    return ''


def exempt_evidence_hit(rel_path: str, line: str, value: str,
                        file_text: str = '') -> tuple[bool, str]:
    """判断一个命中是否属于"证据目录里 fixture 生成的合成值"。

    返回 (是否豁免, 理由)。豁免必须同时满足：
      * 路径在 `evidence/` 下且命中 `EVIDENCE_WHITELIST`；
      * 命中位置不是"源码根"（双保险：NEVER_EXEMPT_ROOTS）；
      * 值本身带合成标记，或文件有 fixture 上下文（production_connected=false 等）。
    不满足任何一条 → 不豁免（宁可误报，也不放水）。
    """
    rel = (rel_path or '').replace('\\', '/')
    if any(rel.startswith(root) for root in NEVER_EXEMPT_ROOTS):
        return False, '源码/配置路径不豁免'
    if not rel.startswith('evidence/'):
        return False, '非证据目录'
    reason = evidence_whitelist_reason(rel)
    if not reason:
        return False, '证据路径不在白名单'
    if looks_synthetic_value(value):
        return True, f'{reason}；值带合成标记'
    if file_has_fixture_context(file_text):
        return True, f'{reason}；文件有 fixture 上下文（production_connected=false 等）'
    return False, '白名单文件但缺少 fixture 上下文'
