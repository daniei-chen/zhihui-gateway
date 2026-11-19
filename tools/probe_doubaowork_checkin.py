#!/usr/bin/env python3
"""豆包工作签到诊断探针：在真实环境里逐步验证这条链路。

和一个「跑一遍就完」的脚本的区别：它把每一步的**原始响应**打出来（脱敏），
出问题时你能立刻看出是哪个端点、哪个字段不对，而不是只得到「签到失败」。

用法（默认只读，不提交领取）：

    python tools/probe_doubaowork_checkin.py --accounts-dir /opt/doubaowork2api/accounts

确认无误后再真签：

    python tools/probe_doubaowork_checkin.py --accounts-dir /opt/doubaowork2api/accounts --claim

## 为什么要有 `--claim` 这个开关

签到会**真发奖**。诊断脚本默认不该有副作用，否则「我只是想看看通不通」就会
把当天的额度领掉、甚至让 `idempotent` 掩盖掉真正的状态。所以默认 `--query-only`
行为，显式 `--claim` 才提交。

## 2026-11-16 真机结论（写在这里，省得下一个人再踩）

* `POST /alice/user/launch` 可能返回 `{"code":710010702,"msg":"系统错误"}`，
  而同一时刻 `POST /alice/ug/campaign/v3/detail/` 完全正常 —— **launch 不可依赖**。
* `v3/detail/` **必须**带稳定 query 参数，否则 `{"code":1,"message":"invalid param"}`。
* `710010702` 是通用风控拒绝码，不是「需要 SDK 签名」也不是「设备指纹不匹配」。
* 活动页上「该活动仅支持在豆包电脑版参与」只是 UI 拦截，**接口不校验**。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 只用 ASCII 标记：中文 + emoji 在 GBK 控制台（Windows 默认 cp936）会抛
# UnicodeEncodeError，把一个「诊断工具」变成「崩溃源头」——这工具最该在
# 出问题时能跑，所以宁可朴素。
_TAG_OK = "[OK]"
_TAG_WARN = "[WARN]"
_TAG_BAD = "[BAD]"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from server.qtgcore.adapters import doubao_checkin as dc

# 这些字段在输出时会被打码，避免把凭据写进日志/工单
_SECRET_KEYS = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "msToken",
                "passport_csrf_token", "ttwid", "odin_tt", "tt_scid", "s_v_web_id")


def mask(value: object, *, keep: int = 6) -> str:
    text = str(value)
    if len(text) <= keep:
        return "*" * len(text)
    return f"{text[:keep]}…({len(text)} chars)"


def scrub(payload: object) -> object:
    """把响应里的敏感字段打码（响应本身一般不含 cookie，但防御性处理）。"""
    if isinstance(payload, dict):
        return {k: (mask(v) if k in _SECRET_KEYS else scrub(v)) for k, v in payload.items()}
    if isinstance(payload, list):
        return [scrub(item) for item in payload[:6]]
    return payload


def cookie_shape(cookies: dict) -> dict:
    """只报告 cookie 的**形状**（有哪些键、值多长），不报告值。"""
    return {k: (len(v) if k not in _SECRET_KEYS else mask(v, keep=4))
            for k, v in sorted(cookies.items())}


async def probe_one(client: httpx.AsyncClient, account: dict, claim: bool) -> int:
    """返回退出码（0 = 全部符合预期）。"""
    print(f"\n{'=' * 70}\n账号: {account['id']}  ({account.get('name') or '-'})")
    print("cookie 形状:", json.dumps(cookie_shape(account["cookies"]), ensure_ascii=False))
    has_device = any(k in account["cookies"] for k in
                     ("ttwid", "odin_tt", "tt_scid", "s_v_web_id"))
    print(f"设备级 cookie: {'有' if has_device else '**缺失**（发奖端点可能会拒）'}")
    problems = 0

    # ① launch
    print("\n[1/4] POST /alice/user/launch")
    try:
        launch = await dc._post(client, dc.LAUNCH_PATH, account["cookies"], {})
        code, msg = dc._error_of(launch)
        group = dc.activity_group(launch)
        print(f"      code={code} msg={msg!r} group={group!r}")
        if code is not None:
            print("      ↑ 这不算致命：launch 在真机上就会这样，下面会用已知组回退")
    except Exception as exc:  # noqa: BLE001
        print(f"      异常: {exc}")
        print("      ↑ 同样不致命，继续")

    # ② detail（用回退组）
    group = dc.V3_GROUP
    print(f"\n[2/4] POST {dc.detail_path(group)}  (group={group})")
    try:
        detail = await dc._post(client, dc.detail_path(group), account["cookies"],
                                {"campaign_group_code": group})
    except Exception as exc:  # noqa: BLE001
        print(f"      异常: {exc}")
        return 1
    code, msg = dc._error_of(detail)
    if code is not None:
        print(f"      **失败** code={code} msg={msg!r}")
        print(scrub(detail))
        return 1
    tasks = dc.walk_daily_tasks(detail, group)
    total = dc._total_amount(detail)
    print(f"      200  total_amount={total}  识别到 {len(tasks)} 个每日签到任务")
    for row in tasks:
        print(f"      · {row['item']}  checked={row['checked']}  "
              f"task_status={row['task_status']}  连签={row['continuous_days']}  "
              f"奖励={row['reward_amount']}")
    if len(tasks) != 1:
        print(f"      **预期 1 个任务，实得 {len(tasks)} 个** —— 多任务时本服务会拒绝猜测")
        problems += 1

    # ③ 空 query 对照（验证「query 是必需的」这个结论在目标环境也成立）
    print("\n[3/4] 对照：不带 query 的 detail（预期 invalid param）")
    try:
        bare = await dc._post(client, dc.detail_path(group), account["cookies"],
                              {"campaign_group_code": group}, query="")
        bcode, bmsg = dc._error_of(bare)
        payload_msg = ((bare.get("data") or {}) if isinstance(bare.get("data"), dict) else {})
        print(f"      code={bcode} msg={bmsg!r} data={payload_msg}")
        if bcode is None:
            print("      ↑ 不带 query 也成功了，说明这个环境不校验 query（与真机不同，留意）")
    except Exception as exc:  # noqa: BLE001
        print(f"      异常（预期内）: {exc}")

    # ④ 领取
    if not claim:
        print("\n[4/4] 跳过领取（默认只读；要真签请加 --claim）")
        return problems

    if not tasks:
        print("\n[4/4] 没有每日签到任务，跳过")
        return problems + 1
    task = tasks[0]
    if task["checked"]:
        print("\n[4/4] 今日已签到，跳过（不重复提交）")
        return problems

    print(f"\n[4/4] POST {dc.CLAIM_PATH}  body={{'scene': {task['group']!r}}}")
    try:
        receipt = await dc._post(client, dc.CLAIM_PATH, account["cookies"],
                                 {"scene": task["group"]})
        rcode, rmsg = dc._error_of(receipt)
        print(f"      code={rcode} msg={rmsg!r}")
        print("      raw:", json.dumps(scrub(receipt), ensure_ascii=False)[:600])
        if rcode is not None:
            return problems + 1
    except dc.CheckinError as exc:
        print(f"      **失败** {exc}")
        if exc.not_open_yet:
            print("      ↑ 这个码是「尚未开放」还是「被风控拒」，要看 msg 与端点；"
                  "服务端开窗时刻逐日漂移，稍后重试")
        return problems + 1
    except Exception as exc:  # noqa: BLE001
        print(f"      异常: {exc}")
        return problems + 1

    # 回查
    print("\n     回查确认（0/2/4 秒）…")
    for delay in dc._RECHECK_DELAYS:
        if delay:
            import asyncio
            await asyncio.sleep(delay)
        try:
            fresh = await dc._post(client, dc.detail_path(group), account["cookies"],
                                   {"campaign_group_code": group})
            rows = dc.walk_daily_tasks(fresh, group)
            hit = next((r for r in rows if r["item"] == task["item"]), None)
            if hit and hit["checked"]:
                print(f"      {_TAG_OK} 确认已签：total={dc._total_amount(fresh)}  "
                      f"task_status={hit['task_status']}  连签={hit['continuous_days']}")
                return problems
            print(f"      未确认（+{delay}s）：checked={hit['checked'] if hit else None}")
        except Exception as exc:  # noqa: BLE001
            print(f"      回查异常（+{delay}s）: {exc}")
    print(f"      {_TAG_WARN} 提交被接受但回查未确认 —— 服务端可能在异步刷新，稍后重跑只会回读")
    return problems + 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="豆包工作签到诊断探针")
    parser.add_argument("--accounts-dir", required=True,
                        help="上游账号落盘目录，例如 /opt/doubaowork2api/accounts")
    parser.add_argument("--claim", action="store_true",
                        help="**真的提交领取**（默认只读，不领取）")
    parser.add_argument("--uid", default="", help="只处理指定账号 id")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    accounts = dc.read_accounts(args.accounts_dir)
    if args.uid:
        accounts = [a for a in accounts if a["id"] == args.uid]
    if not accounts:
        print(f"账号目录里没有可签到的账号：{args.accounts_dir}")
        print("提示：文件里必须有 sessionid / sessionid_ss / sid_tt 之一")
        return 2

    print(f"读到 {len(accounts)} 个账号；模式 = "
          f"{'**领取**' if args.claim else '只读'}")
    problems = 0
    async with httpx.AsyncClient(timeout=args.timeout, follow_redirects=False) as client:
        for index, account in enumerate(accounts):
            if index:
                import asyncio
                await asyncio.sleep(0.8)
            problems += await probe_one(client, account, args.claim)

    print(f"\n{'=' * 70}")
    if problems:
        print(f"结论：有 {problems} 处需要关注（详见上面的分段输出）")
        return 1
    print("结论：链路正常" + ("，签到已完成" if args.claim else "（只读，未领取）"))
    return 0


if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
