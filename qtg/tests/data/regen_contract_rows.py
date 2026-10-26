#!/usr/bin/env python3
"""从契约表重新投影 `contracts-qtg-rows.json`（K04 交叉核对的数据源）。

投影规则：
* 取 `contracts/roles-actions-platforms.csv`；
* 保留 `backend_authority` 含 QTG 的行（QTG 控制面侧行为）；
* **另外保留**矩阵显式引用的跨权威动作（A18 扫码登录、A36 /wb/ 别名）——
  它们在 Manager 侧是权威，但 QTG 侧也有对应端点/转发行为，矩阵需要能交叉核对。

用法：
  python regen_contract_rows.py [--audit-dir <审计包目录>] [--out <json>]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_AUDIT = Path(__file__).resolve().parents[3]
CROSS_AUTHORITY_ACTIONS = {"A18", "A36"}


def project(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            action = (row.get("action_id") or "").strip()
            authority = (row.get("backend_authority") or "").strip()
            if "QTG" not in authority and action not in CROSS_AUTHORITY_ACTIONS:
                continue
            allowed_raw = (row.get("allowed") or "").strip()
            rows.append({
                "row_id": row.get("row_id", ""),
                "action": action,
                "title": (row.get("action") or "").strip(),
                "role": (row.get("role") or "").strip(),
                "role_raw": (row.get("role") or "").strip(),
                "platform": (row.get("platform") or "").strip(),
                "backend_authority": authority,
                "method": (row.get("method") or "").strip(),
                "endpoint": (row.get("endpoint") or "").strip(),
                "guard": (row.get("guard") or "").strip(),
                "allowed_raw": allowed_raw,
                "status": "allow" if allowed_raw.startswith(("允许", "可")) else "deny",
                "failure_code": (row.get("failure_code") or "").strip(),
                "evidence": (row.get("evidence") or "").strip(),
            })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit-dir", default=str(DEFAULT_AUDIT))
    ap.add_argument("--out", default=str(HERE / "contracts-qtg-rows.json"))
    args = ap.parse_args(argv)

    csv_path = Path(args.audit_dir) / "contracts" / "roles-actions-platforms.csv"
    if not csv_path.is_file():
        # 兼容：契约表也可能在执行树的 contracts/ 下（qtg/tests/data -> 执行树根 = parents[3]）
        alt = Path(__file__).resolve().parents[3] / "contracts" / "roles-actions-platforms.csv"
        csv_path = alt if alt.is_file() else csv_path
    if not csv_path.is_file():
        print(f"找不到契约表: {csv_path}", file=sys.stderr)
        return 2

    rows = project(csv_path)
    out = {
        "source": "contracts/roles-actions-platforms.csv（S0/T01 产物）",
        "filter": "backend_authority 含 QTG 的行 + 矩阵显式引用的跨权威动作（A18/A36）",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "note": "契约表的只读投影，供 qtg 测试做交叉核对；改动请回契约表并重跑本脚本。",
        "row_count": len(rows),
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"rows={len(rows)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
