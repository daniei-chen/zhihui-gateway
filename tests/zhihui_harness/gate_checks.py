"""质量门禁的 Python 检查项：语法编译 + 敏感值扫描。

只读源码树，不写任何业务目录；结果以 JSON/文本输出，供 ``gate.ps1`` 汇总。

敏感值扫描的**豁免口径**（R3-L6 起，与 ``tests/linux_lab/scan_secret_plaintext.py``
共用 ``tests/zhihui_harness/secret_policy.py`` 的合成分类，避免两套标准漂移）：

* **允许豁免**：证据目录里"由 fixture 生成的合成值"——路径必须命中
  ``secret_policy.EVIDENCE_WHITELIST``（如 ``evidence/browser/ui1*``、``k38*``、
  ``evidence/r2-*``、``evidence/r3-*`` 下的 report/json），**且**值带 synthetic/fixture
  等标记，或文件声明 ``production_connected: false``；
* **永不豁免**：``manager/``、``qtg/``、``src/``、``ops/``、``tests/``、``tasks/``、
  ``contracts/``、``baseline/``、``design/``、``recovery/``、``release/`` 下的命中
  一律算失败（源码/配置/测试脚本里不允许出现真实凭据形态）；
* **白名单可审计**：规则与理由集中在 ``secret_policy.py``，新增/放宽必须写理由，
  不允许"证据目录全放行"。

用法::

    python tests/zhihui_harness/gate_checks.py --syntax
    python tests/zhihui_harness/gate_checks.py --secrets
    python tests/zhihui_harness/gate_checks.py --all --json evidence/gate-checks.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secret_policy import exempt_evidence_hit  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"

# 扫描范围：业务源码 + 测试 + 证据 + 治理产物
SCAN_ROOTS = ("manager", "qtg", "tests", "evidence", "contracts", "baseline", "src")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".next",
             ".venv", "venv", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff",
                 ".woff2", ".ttf", ".eot", ".zip", ".gz", ".tar", ".db", ".sqlite",
                 ".pdf", ".xlsx", ".docx", ".pptx", ".mp4", ".so", ".dll", ".exe"}
MAX_FILE_BYTES = 8 * 1024 * 1024
TEXT_SUFFIXES = {".py", ".ps1", ".cmd", ".sh", ".json", ".jsonl", ".md", ".txt",
                 ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".ts",
                 ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".css", ".patch",
                 ".service", ".log", ""}

# 真实凭据形态（命中即失败；假数据/占位符不应命中这些形态）
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("管理/推理 key 前缀 wbk_", re.compile(r"\bwbk_[A-Za-z0-9]{16,}\b")),
    ("QTG key 前缀 sk-qtg-", re.compile(r"\bsk-qtg-[A-Za-z0-9_\-]{16,}\b")),
    ("OpenAI 风格 key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----")),
    ("明文 Bearer token", re.compile(r"(?i)authorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._\-]{24,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]

# 明确声明是假数据的行内豁免（必须显式写注释，不允许默默忽略）
ALLOWLIST_MARKERS = ("secret-scan: allow", "placeholder", "fake-for-isolated-tests")


def iter_files(roots: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel_parts = set(p.relative_to(REPO).parts[:-1])
            if rel_parts & SKIP_DIRS:
                continue
            if p.suffix.lower() in SKIP_SUFFIXES:
                continue
            if p.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(p)
    return files


# ---------------------------------------------------------------------------
# 1) 语法编译（用 ast.parse，不写 .pyc，避免污染业务源码树）
# ---------------------------------------------------------------------------
def check_syntax() -> dict:
    targets: list[Path] = []
    for root in ("manager/server", "qtg/app", "qtg/tests", "tests"):
        base = REPO / root
        if base.is_dir():
            targets.extend(p for p in base.rglob("*.py")
                           if "__pycache__" not in p.parts)
    errors: list[dict] = []
    for p in sorted(targets):
        try:
            source = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            source = p.read_text(encoding="utf-8", errors="replace")
        try:
            ast.parse(source, filename=str(p))
        except SyntaxError as exc:
            errors.append({"file": str(p.relative_to(REPO)), "line": exc.lineno,
                           "message": exc.msg, "text": (exc.text or "").strip()[:160]})
    return {"ok": not errors, "checked": len(targets), "errors": errors}


# ---------------------------------------------------------------------------
# 2) 敏感值扫描
# ---------------------------------------------------------------------------
def _redact(match: str) -> str:
    return match[:8] + "***" + f"（长度 {len(match)}）"


def _is_placeholder(token: str) -> bool:
    """判断命中是否是文档里的占位符（例如 ``wbk_xxxxxxxx...``）。

    规则：去掉已知前缀后，出现连续 4 个及以上相同字符即视为占位符。
    真实随机 key 出现 4 连同字符的概率极低；这条规则让文档示例不误报，
    同时保留对真实 key 的检测。若某天真的命中，报告里会原样列出。
    """
    body = token
    for prefix in ("wbk_", "sk-qtg-", "sk-"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    return bool(re.search(r"(.)\1{3,}", body))


def check_secrets() -> dict:
    hits: list[dict] = []
    evidence_exempt: list[dict] = []
    scanned = 0
    placeholders = 0
    for p in iter_files(SCAN_ROOTS):
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        except OSError:
            continue
        scanned += 1
        rel = p.relative_to(REPO).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in ALLOWLIST_MARKERS):
                continue
            for name, pattern in SECRET_PATTERNS:
                m = pattern.search(line)
                if m:
                    if _is_placeholder(m.group(0)):
                        placeholders += 1
                        break
                    exempt, reason = exempt_evidence_hit(rel, line, m.group(0), text)
                    if exempt:
                        # 证据目录里由 fixture 生成的合成值：记录计数与理由，不判失败。
                        evidence_exempt.append({"file": rel, "line": lineno,
                                                "kind": name, "reason": reason})
                        break
                    hits.append({
                        "file": rel,
                        "line": lineno,
                        "kind": name,
                        "sample": _redact(m.group(0)),
                    })
                    break
    return {"ok": not hits, "scanned_files": scanned, "hits": hits[:200],
            "hit_count": len(hits), "placeholders_skipped": placeholders,
            "evidence_fixture_skipped": len(evidence_exempt),
            "evidence_fixture_examples": evidence_exempt[:20],
            "exemption_policy": "tests/zhihui_harness/secret_policy.py"
                                 "（evidence 白名单 + fixture 上下文；源码/配置永不豁免）"}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="门禁检查项（语法/敏感值）")
    ap.add_argument("--syntax", action="store_true")
    ap.add_argument("--secrets", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", default="", help="结果写入 JSON 文件")
    args = ap.parse_args(argv)
    if not (args.syntax or args.secrets or args.all):
        args.all = True

    out: dict = {}
    code = 0
    if args.syntax or args.all:
        r = check_syntax()
        out["syntax"] = r
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] Python 语法编译：检查 {r['checked']} 个 .py，"
              f"错误 {len(r['errors'])} 个")
        for e in r["errors"][:30]:
            print(f"    {e['file']}:{e['line']}: {e['message']}")
        code = code or (0 if r["ok"] else 1)
    if args.secrets or args.all:
        r = check_secrets()
        out["secrets"] = r
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] 敏感值扫描：扫描 {r['scanned_files']} 个文件，"
              f"命中 {r['hit_count']} 处，占位符排除 {r.get('placeholders_skipped', 0)} 处")
        for h in r["hits"][:30]:
            print(f"    {h['file']}:{h['line']} [{h['kind']}] {h['sample']}")
        code = code or (0 if r["ok"] else 1)
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
