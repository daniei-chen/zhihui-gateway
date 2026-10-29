"""测试结果归因分析：把失败/错误按「环境问题 / 平台差异 / 真实缺陷候选」分桶。

输入是隔离运行器写的 ``evidence/stats-<target>.json``（来自 junit XML，含每条用例的
异常消息）。归因规则全部基于**可核对的错误证据**，不做“猜”；规则命中不了的落到
``待复核``，由人工看消息再定，绝不为了凑比例硬塞类别。

核心用法::

    # 单次运行归因
    python tests/zhihui_harness/analyze_results.py --stats evidence/stats-manager.json

    # 两次运行对比：无前端资产 vs 补资产后通过 -> 证明是环境问题（缺资产）
    python tests/zhihui_harness/analyze_results.py \
        --stats evidence/stats-manager.json \
        --assets-stats evidence/stats-manager-assets.json

输出：
  * evidence/classification-<target>.csv  逐用例归因
  * evidence/classification-<target>.md   汇总 + 真实缺陷候选清单
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "evidence"

CAT_ENV = "环境问题"
CAT_PLATFORM = "平台差异"
CAT_REAL = "真实缺陷候选"
CAT_REVIEW = "待复核"
CAT_TEST = "测试自身问题"

# ---------------------------------------------------------------------------
# 规则表：每条形如 (类别, 正则, 说明)；先匹配先赢（顺序有意义）
# ---------------------------------------------------------------------------
MISSING_ASSET_RE = re.compile(
    r"FileNotFoundError.*?(?:web|docs|CHANGELOG|README|Dockerfile|docker-compose|"
    r"package\.json|next\.config|tsconfig|\.mjs|public|windows|deploy|\.github|LICENSE|"
    r"requirements|upstream-schema)",
    re.IGNORECASE | re.DOTALL,
)

RULES: list[tuple[str, re.Pattern[str], str]] = [
    # ---- 环境：本机没有的能力 ----
    (CAT_ENV, re.compile(r"(docker|podman).{0,40}(not found|无法|not recognized|找不到)", re.I),
     "本机无 Docker（04 §7 已知：Windows 无 Docker/WSL）"),
    (CAT_ENV, re.compile(r"(systemctl|systemd|journalctl).{0,40}(not found|无法|not recognized)", re.I),
     "本机无 systemd（Windows 环境）"),
    (CAT_ENV, re.compile(r"(bash|sh|openssl|git).{0,40}(not found|not recognized|No such file)", re.I),
     "外部命令缺失（退出码/not found）"),
    (CAT_ENV, re.compile(r"No module named '(yaml|requests|aiohttp|redis|psycopg|asyncpg|bs4|lxml|jwt|cryptography)'",
                         re.I),
     "本地未安装该第三方依赖（环境问题，非代码缺陷）"),
    (CAT_ENV, re.compile(r"(ConnectionError|ConnectError|NameResolutionError|getaddrinfo failed|"
                         r"Temporary failure in name resolution|Network is unreachable|"
                         r"proxy|ProxyError|Max retries exceeded)", re.I),
     "依赖真实网络（隔离禁外网，属环境问题；需评估测试是否应改为假上游）"),
    (CAT_ENV, re.compile(r"Using SOCKS proxy|socksio", re.I),
     "本地未安装 httpx[socks]（socksio），环境问题"),
    (CAT_ENV, re.compile(r"只解析出 \d+ 条|NAV_COMMANDS 的写法变了|解析失效"),
     "补入的前端快照与 manager 部署版本不一致（命令面板条目 18 < 期望 20，属资产版本错配）"),
    (CAT_REVIEW, re.compile(r"受控维护版本|网页更新已暂停|Managed release"),
     "受控维护版本策略：deploy/update.py 检测到 managed-release.json 后主动拒绝自助升级"),
    (CAT_ENV, re.compile(r"Regex didn't match.*realm|Regex didn't match.*\\{"),
     "补入的前端快照与 manager 部署版本不一致（web/lib/api.ts 的请求体形状已变）"),
    (CAT_ENV, re.compile(r"\[ZH-ISO\].*(?:阻断|拒绝写入)", re.I),
     "隔离守卫拦截（外网或生产路径），属环境隔离预期行为"),
    # ---- 平台：Windows 与 Linux 语义差异 ----
    (CAT_PLATFORM, re.compile(r"PermissionError.*WinError 32|WinError 32", re.I),
     "Windows 文件占用语义（sqlite/句柄未关即删）—— POSIX 可删，Windows 报 32"),
    (CAT_PLATFORM, re.compile(r"WinError 5|Access is denied|拒绝访问", re.I),
     "Windows 权限模型差异（ACL 而非 POSIX 位）"),
    (CAT_PLATFORM, re.compile(r"0o?600|mode_0600|chmod|st_mode|0o700|0o644|0o666", re.I),
     "POSIX 权限位断言在 Windows 不成立（chmod 无效）"),
    (CAT_PLATFORM, re.compile(r"(symlink|symlink_|符号链接|Is a symbolic link|WinError 1314)", re.I),
     "Windows 符号链接权限/语义差异"),
    (CAT_PLATFORM, re.compile(r"(/proc/|/sys/|getppid|killpg|SIGKILL|SIGTERM|fork|daemon)", re.I),
     "POSIX 进程模型（/proc、信号、fork）在 Windows 不成立"),
    (CAT_PLATFORM, re.compile(r"ValueError: .*必须是绝对路径: '/opt|QTG_.*'/opt", re.I),
     "Linux 绝对路径默认值在 Windows 不是绝对路径（仅平台语义差异）"),
    (CAT_PLATFORM, re.compile(r"(CRLF|line endings|换行|\\\\r\\\\n)", re.I),
     "换行符/编码差异（Windows CRLF vs POSIX LF）"),
    (CAT_PLATFORM, re.compile(r"(path separator|路径分隔符|os\.sep)", re.I),
     "路径分隔符差异"),
    (CAT_PLATFORM, re.compile(
        r"MODULE_TYPELESS_PACKAGE_JSON|doesn't parse as CommonJS|'all passed' not found in '?\(node:",
        re.I),
     "Node v24 对 .ts 的模块类型判定变化：.mjs 直接 import .ts 触发解析差异（项目预期更低 Node）"),
    (CAT_PLATFORM, re.compile(r"(WinError 1314|需要 SeCreateSymbolicLinkPrivilege)", re.I),
     "Windows 建符号链接需要特权（POSIX 不需要）"),
    # ---- 环境：执行树不完整（manager 部署副本缺 web/dev/docs 等）----
    (CAT_ENV, re.compile(r"FileNotFoundError.*?(?:\\\\|/)web(?:\\\\|/)", re.I),
     "执行树不完整：manager 部署副本没有 web/ 前端资产（测试却在读 web/lib/*.test.mjs）"),
    (CAT_ENV, re.compile(r"(FileNotFoundError|No such file).*?(?:CHANGELOG|README|docs|Dockerfile|"
                         r"docker-compose|package\.json|next\.config|\.github|windows|deploy)", re.I),
     "执行树不完整：部署副本缺少该资产文件"),
    (CAT_ENV, MISSING_ASSET_RE, "执行树不完整：找不到测试要读的资产文件"),
    # ---- 环境：测试临时文件/目录问题 ----
    (CAT_ENV, re.compile(r"FileNotFoundError.*?(?:Temp|tmp|TemporaryDirectory)", re.I),
     "测试临时文件路径问题（清理顺序/临时目录被删）—— 需在 Linux 复核"),
    # ---- 断言：进入人工复核 ----
    (CAT_REAL, re.compile(r"AssertionError", re.I), "断言失败（需人工判断实现还是断言过期）"),
]

# 已知“可能真是缺陷”的关键词（用于在报告里优先列出，仍需人工复核）
SECURITY_HINTS = re.compile(
    r"(auth|权限|鉴权|csrf|token|bearer|ssrf|traversal|路径穿越|escape|越权|bypass|"
    r"credential|secret|密码|配额|quota|budget|预算|计量|usage|charge|扣费|"
    r"cancel|取消|timeout|超时|retry|重试|sse|stream|断流)" , re.I)


def _msg(item: dict[str, Any]) -> str:
    return f"{item.get('message') or ''}\n{item.get('text') or ''}"


def _missing_path(msg: str) -> str | None:
    m = re.search(r"No such file or directory: '([^']+)'", msg)
    if m:
        return m.group(1)
    m = re.search(r'No such file or directory: "([^"]+)"', msg)
    return m.group(1) if m else None


def _shorten_path(path: str) -> str:
    """把临时视图路径归一化成相对路径，便于报告阅读。"""
    p = re.sub(r"^.*?[\\/](manager-view|manager-assets-view|src-view|qtg-view)[\\/]", "", path,
               flags=re.I)
    p = re.sub(r"^.*?[\\/](zhihui-iso-[^\\/]+)[\\/]", r"<iso>/", p)
    return p


def classify(item: dict[str, Any]) -> tuple[str, str]:
    msg = _msg(item)
    for category, pattern, reason in RULES:
        if pattern.search(msg):
            return category, reason
    return CAT_REVIEW, "没有命中任何已知规则，需人工判断"


def load_stats(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def outcome_map(stats: dict[str, Any]) -> dict[str, str]:
    """test_id -> failed/error/passed/skipped；用于两次运行对比。"""
    out: dict[str, str] = {}
    for item in stats["stats"]["failures"]:
        out[item["id"]] = "failed"
    for item in stats["stats"]["error_items"]:
        out[item["id"]] = "error"
    for item in stats["stats"]["skips"]:
        out[item["id"]] = "skipped"
    return out


def passed_ids(stats: dict[str, Any]) -> set[str]:
    """从 junit 反推通过用例：junit 里没有 failure/error/skipped 的 testcase。

    stats 只保存失败/错误/跳过明细，所以这里用 tests 总数与明细差集不够精确；
    改为直接解析 junit（stats 里记录了 junit 路径）。
    """
    junit = stats.get("junit")
    if not junit:
        return set()
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        return set()
    passed: set[str] = set()
    for tc in root.iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None or tc.find("skipped") is not None:
            continue
        name = "::".join(x for x in (tc.get("classname", ""), tc.get("name", "")) if x)
        passed.add(name)
    return passed


def analyze(stats_path: Path, assets_stats_path: Path | None) -> dict[str, Any]:
    data = load_stats(stats_path)
    target = data.get("target", stats_path.stem)
    items: list[dict[str, Any]] = []
    for kind, lst in (("failed", data["stats"]["failures"]), ("error", data["stats"]["error_items"])):
        for it in lst:
            category, reason = classify(it)
            entry = {
                "target": target,
                "test_id": it["id"],
                "outcome": kind,
                "category": category,
                "reason": reason,
                "missing_path": _shorten_path(_missing_path(_msg(it)) or ""),
                "evidence": (it.get("message") or "").replace("\n", " ")[:400],
            }
            items.append(entry)

    # 两次运行对比：补资产后通过 => 环境问题（缺资产）的硬证据
    assets_flips: list[dict[str, Any]] = []
    if assets_stats_path and assets_stats_path.exists():
        assets = load_stats(assets_stats_path)
        assets_failed = outcome_map(assets)
        assets_passed = passed_ids(assets)
        for entry in items:
            tid = entry["test_id"]
            if assets_passed and tid in assets_passed and assets_failed.get(tid) is None:
                entry["category"] = CAT_ENV
                entry["reason"] = "补 web/dev/docs 资产后通过 → 证明失败源于执行树缺资产（环境问题）"
                entry["flipped_by_assets"] = True
                assets_flips.append(entry)

    categories = Counter(e["category"] for e in items)
    by_file = Counter(e["test_id"].split("::")[0] for e in items)
    real_candidates = [e for e in items if e["category"] == CAT_REAL]
    review = [e for e in items if e["category"] == CAT_REVIEW]
    security_relevant = [e for e in items + assets_flips
                         if SECURITY_HINTS.search(e["test_id"] + " " + e["evidence"])]
    return {
        "target": target,
        "stats_path": str(stats_path),
        "assets_stats_path": str(assets_stats_path) if assets_stats_path else "",
        "totals": data["stats"],
        "summary": data.get("summary", ""),
        "items": items,
        "categories": dict(categories),
        "by_file": dict(by_file),
        "real_candidates": real_candidates,
        "review": review,
        "security_relevant": security_relevant,
        "flipped_by_assets": len(assets_flips),
    }


def write_csv(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "target", "test_id", "outcome", "category", "reason", "missing_path",
            "flipped_by_assets", "evidence"])
        writer.writeheader()
        for e in result["items"]:
            writer.writerow({
                "target": e["target"],
                "test_id": e["test_id"],
                "outcome": e["outcome"],
                "category": e["category"],
                "reason": e["reason"],
                "missing_path": e.get("missing_path", ""),
                "flipped_by_assets": e.get("flipped_by_assets", False),
                "evidence": e["evidence"],
            })


def write_md(result: dict[str, Any], path: Path) -> None:
    t = result["totals"]
    lines: list[str] = []
    lines.append(f"# 测试结果归因：{result['target']}")
    lines.append("")
    lines.append(f"- 数据源：`{result['stats_path']}`")
    if result["assets_stats_path"]:
        lines.append(f"- 对照运行：`{result['assets_stats_path']}`")
    lines.append(f"- 总计：tests={t['tests']} passed={t['passed']} failed={t['failed']} "
                 f"errors={t['errors']} skipped={t['skipped']}")
    lines.append("")
    lines.append("## 类别统计")
    lines.append("")
    lines.append("| 类别 | 数量 |")
    lines.append("|---|---|")
    for cat, n in sorted(result["categories"].items(), key=lambda x: -x[1]):
        lines.append(f"| {cat} | {n} |")
    lines.append(f"| **合计** | **{len(result['items'])}** |")
    lines.append("")
    if result["flipped_by_assets"]:
        lines.append(f"> 其中 **{result['flipped_by_assets']}** 个用例在「补 web/dev/docs 资产」的"
                     "对照运行中转为通过，属于执行树不完整（环境问题）的硬证据。")
        lines.append("")
    lines.append("## 失败最多的文件（Top 20）")
    lines.append("")
    lines.append("| 文件 | 数量 |")
    lines.append("|---|---|")
    for f, n in sorted(result["by_file"].items(), key=lambda x: -x[1])[:20]:
        lines.append(f"| `{f}` | {n} |")
    lines.append("")
    for title, items in (("真实缺陷候选（需人工复核）", result["real_candidates"]),
                         ("待复核", result["review"])):
        lines.append(f"## {title}（{len(items)}）")
        lines.append("")
        if not items:
            lines.append("无。")
            lines.append("")
            continue
        for e in items:
            lines.append(f"- `{e['test_id']}`")
            lines.append(f"  - {e['reason']}")
            lines.append(f"  - 证据：{e['evidence'][:300]}")
        lines.append("")
    lines.append("## 与安全/计量/取消语义相关的条目")
    lines.append("")
    if not result["security_relevant"]:
        lines.append("无。")
    else:
        for e in result["security_relevant"][:60]:
            lines.append(f"- `{e['test_id']}`（{e['category']}）：{e['evidence'][:200]}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="测试结果归因分析")
    ap.add_argument("--stats", required=True, help="stats-<target>.json")
    ap.add_argument("--assets-stats", default="", help="补资产对照运行的 stats json")
    ap.add_argument("--out-dir", default=str(EVIDENCE))
    ap.add_argument("--list", default="", help="只列出某类别明细")
    args = ap.parse_args(argv)

    stats_path = Path(args.stats)
    if not stats_path.exists():
        print(f"找不到 {stats_path}", file=sys.stderr)
        return 2
    assets = Path(args.assets_stats) if args.assets_stats else None
    result = analyze(stats_path, assets)

    if args.list:
        for e in result["items"]:
            if e["category"] == args.list:
                print(f"{e['outcome']:6} {e['test_id']}")
                print(f"       {e['reason']}")
                print(f"       {e['evidence'][:220]}")
        return 0

    out_dir = Path(args.out_dir)
    csv_path = out_dir / f"classification-{result['target']}.csv"
    md_path = out_dir / f"classification-{result['target']}.md"
    write_csv(result, csv_path)
    write_md(result, md_path)

    print(f"数据源: {stats_path}")
    print(f"总计: tests={result['totals']['tests']} passed={result['totals']['passed']} "
          f"failed={result['totals']['failed']} errors={result['totals']['errors']} "
          f"skipped={result['totals']['skipped']}")
    print("类别统计:")
    for cat, n in sorted(result["categories"].items(), key=lambda x: -x[1]):
        print(f"  {cat:10s} {n:5d}")
    print(f"  合计       {len(result['items']):5d}")
    if result["flipped_by_assets"]:
        print(f"  其中补资产后通过: {result['flipped_by_assets']}")
    print(f"\n真实缺陷候选 {len(result['real_candidates'])} 个，待复核 {len(result['review'])} 个")
    print(f"CSV: {csv_path}")
    print(f"MD : {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
