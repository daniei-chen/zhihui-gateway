"""验证聚合层（service.py）对第四平台的输出契约。

不启动整个 qtg（默认配置是 Linux 路径），直接构造 Aggregate 与原始行，
检查 accounts_payload / summarize / map_doubaowork_account 的实际输出。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.qtgcore.service import (  # noqa: E402
    Aggregate, accounts_payload, all_accounts, map_doubaowork_account,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


# 与 doubaowork2api 0.3.0 的 Account.public_dict() 完全同构的一行
RAW = {
    "id": "acct-1", "name": "主账号", "remark": "", "group": "g1",
    "enabled": True, "available": True, "state": "busy",
    "cooldown_remaining_seconds": 0, "last_error": "",
    "error_code": "", "last_error_at": 0.0,
    "device_id": "abc123...", "web_id": "def456...", "in_flight": 1,
}
RAW_COOLING = {
    "id": "acct-2", "name": "", "remark": "", "group": "",
    "enabled": True, "available": False, "state": "cooling",
    "cooldown_remaining_seconds": 42, "last_error": "人工验证 710022004",
    "error_code": "need_verify", "last_error_at": 1.0,
    "device_id": "", "web_id": "", "in_flight": 0,
}
RAW_DISABLED = dict(RAW, id="acct-3", name="停用", enabled=False, state="disabled", in_flight=0)

a = map_doubaowork_account(RAW)
check("platform == doubaowork", a.platform == "doubaowork", a.platform)
check("busy 归为 ready（不是异常态）", a.state == "ready", a.state)
check("in_flight 透传", a.in_flight == 1, str(a.in_flight))
check("credits 全 0", (a.credits_remain, a.credits_total, a.credits_used) == (0.0, 0.0, 0.0))
check("note 写明上游不提供额度", "上游不提供额度" in a.note, a.note)
check("realm 留空（无版本概念）", a.realm == "", repr(a.realm))
check("checked_today 恒 False", a.checked_today is False)
check("cooldown_until 不反推", a.cooldown_until is None)

b = map_doubaowork_account(RAW_COOLING)
check("cooling 映射", b.state == "cooling", b.state)
check("in_cooldown 一致", b.in_cooldown is True)
check("note 保留上游错误", b.note == "人工验证 710022004", b.note)
check("error_code 计入 errors", b.errors == 1, str(b.errors))

c = map_doubaowork_account(RAW_DISABLED)
check("disabled 映射", c.state == "disabled", c.state)

agg = Aggregate()
agg.doubaowork_accounts = [a, b, c]
payload = accounts_payload(agg)
check("payload 有 doubaowork 字段", hasattr(payload, "doubaowork"), type(payload).__name__)
check("payload.doubaowork 三行", len(payload.doubaowork) == 3, str(len(payload.doubaowork)))
check("summary 含 doubaowork", "doubaowork" in payload.summary, ",".join(payload.summary))
check("summary.doubaowork.total == 3", payload.summary["doubaowork"].total == 3,
      str(payload.summary["doubaowork"].total))
check("summary.all 计入第四平台", payload.summary["all"].total == 3,
      str(payload.summary["all"].total))
check("all_accounts 含第四平台", len(all_accounts(agg)) == 3, str(len(all_accounts(agg))))

# 序列化必须能被 FastAPI/JSON 接受
serialized = payload.model_dump()
check("model_dump 后 doubaowork 存在", "doubaowork" in serialized)
check("序列化后 credits 仍是 0 而不是 None",
      serialized["doubaowork"][0]["credits_remain"] == 0.0)

failed = [r for r in RESULTS if not r[1]]
print("\n" + "=" * 60)
print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
for name, _, detail in failed:
    print(f"  FAILED: {name} :: {detail}")
print("=" * 60)
raise SystemExit(1 if failed else 0)
