"""豆包（doubao.com）每日签到：纯 HTTP 实现，不需要桌面客户端。

## 为什么需要这么一个模块

`doubaowork2api`（我们的上游）**没有任何签到端点** —— 它的 25 条路由里没有
checkin。但豆包官方的签到是**网页接口**，客户端也只是调它。所以一个跑在
Linux 上的服务端反代完全可以签，前提是拿到账号的完整 Cookie。

## 协议来源（两份独立实现交叉验证）

1. [`smart-open/TraeWorkAssistant`](https://github.com/smart-open/TraeWorkAssistant)
   （MIT，480 stars）`src-tauri/src/tasks/doubao_checkin.rs` —— 抓包固化，
   给出 `landing/checkin` 的 body 与稳定 query 参数。
2. [`L0NE-6/DoubaoWork-Daily`](https://github.com/L0NE-6/DoubaoWork-Daily)
   （MIT，v1.3.4，2026-11-14）`doubaowork_daily.py` —— 单文件 Python 实现，
   补上了 **`POST /alice/user/launch` 发现活动组**、**双详情路径**、
   **`data.biz_error` 回执判据** 和 **回查确认**。

第 2 份的流程更完整，本模块按它的语义实现：

    Cookie
      → POST /alice/user/launch                      ← 发现当前活动组
      → POST /alice/ug/campaign/{v3/detail|list}/     ← 按组选路径，读任务
      → 递归找**唯一**的 daily_checkin 任务
      → 已勾选 → already
      → 未勾选 → POST /alice/ug/campaign/landing/checkin  {"scene": <组>}
      → 回执 data.biz_error.code == 0
      → 回查（0/2/4 秒），只有 checked_in_today == true 才算 claimed

## 关于 `710010702`（我第一版解释错了，这里改对）

第一版把它写成「设备指纹与账号不匹配」。**不准确。** 上游实现（DoubaoWork-Daily）
的说明是：

> 若 `landing/checkin` 返回 `710010702`，当前页面通常要求 SDK 运行时生成
> `biz_params` 与 `AgwCommonParam`。脚本不会伪造这两个风控字段；
> 请在官方客户端点击「立即签到」，然后再次运行脚本。

也就是说它是**需要 SDK 签名的风控字段**，不是「你的 cookie 不对」。本模块同样
**不伪造**这两个字段——伪造签名是另一码事，而且随时会被上游改掉。

## 红线

只做官方签到动作；不伪造 `msToken`/`a_bogus`/`biz_params`/`AgwCommonParam`；
失败如实上报并给出**可执行**的下一步；**绝不**为了界面上有个绿勾而编造成功。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASE_URL = (os.getenv("QTG_DOUBAOWORK_SITE") or "https://www.doubao.com").rstrip("/")
V3_GROUP = "pc_incentive_task_mvp_v3"

LAUNCH_PATH = "/alice/user/launch"
DETAIL_V3_PATH = "/alice/ug/campaign/v3/detail/"
DETAIL_LIST_PATH = "/alice/ug/campaign/list/"
CLAIM_PATH = "/alice/ug/campaign/landing/checkin"

# 稳定 query 参数（TraeWorkAssistant 抓包固化；msToken/a_bogus 属一次性反爬 token，不带）
_QUERY = ("version_code=20800&language=zh&device_platform=web&aid=497858&real_aid=497858"
          "&pc_version=2.33.5&samantha_web=1&web_platform=desktop&use-olympus-account=1")
REFERER = f"{BASE_URL}/pc/agent-explore"
# 桌面客户端自带 SamanthaDoubao 标识（区别于纯浏览器请求）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 SamanthaDoubao/2.33.5")

# 会话身份键；其余（ttwid/odin_tt/tt_scid/s_v_web_id）是设备指纹，发奖端点会校验
_SESSION_KEYS = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "msToken",
                 "passport_csrf_token")

# 回查节奏（秒）：提交后按这个序列回读详情，只有 checked_in_today=true 才算确认
_RECHECK_DELAYS = (0.0, 2.0, 4.0)

_NOT_OPEN_YET = 710010702

_CODES = {
    710012001: "登录态已失效（cookie 过期），请在上游面板重新导入该账号",
    _NOT_OPEN_YET: ("服务端拒绝了这次调用（710010702）。这个码**不唯一**："
                    "既可能是「每日签到尚未开放」（服务端按天开窗、时刻逐日漂移），"
                    "也可能是风控拒绝 —— 真机实测它在 `launch` 端点上就出现，"
                    "`msg` 只是「系统错误」。先稍后重试；若持续多天都这个码，"
                    "再按账号问题排查"),
    712010702: "cookie 或设备指纹缺失/编码错误，请重新保存该账号的完整登录态",
    710022004: "触发风控验证码，需在豆包客户端人工完成一次验证后重试",
}


class CheckinError(RuntimeError):
    """签到过程中的可解释失败（带上游 code 与提示）。"""

    def __init__(self, message: str, *, code: Optional[int] = None,
                 not_open_yet: bool = False) -> None:
        super().__init__(message)
        self.code = code
        # 「尚未开放」是**可重试**的，不该按失败处理，更不该让人去点什么按钮
        self.not_open_yet = not_open_yet


# ------------------------------------------------------------------ 账号读取

def _cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)


def read_accounts(accounts_dir: str) -> List[Dict[str, Any]]:
    """读上游的账号落盘文件，取 id / name / cookies。

    上游 `/admin/accounts` 的 `public_dict()` 是脱敏的（不含 cookie），但
    `to_document()` 落盘时含完整 cookies —— 同机部署下这是唯一能拿到
    `sessionid` 的地方。读不到返回空列表；单个坏文件只跳过它。
    """
    root = Path(accounts_dir)
    out: List[Dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        cookies = doc.get("cookies") or doc.get("cookie") or doc.get("cookie_string")
        if isinstance(cookies, str):
            cookies = parse_cookie_string(cookies)
        if not isinstance(cookies, dict) or not cookies:
            continue
        if not (cookies.get("sessionid") or cookies.get("sessionid_ss") or cookies.get("sid_tt")):
            continue
        out.append({
            "id": str(doc.get("id") or path.stem),
            "name": str(doc.get("name") or "") or str(doc.get("id") or path.stem),
            "enabled": doc.get("enabled", True) is not False,
            "cookies": {str(k): str(v) for k, v in cookies.items()},
        })
    return out


def parse_cookie_string(value: str) -> Dict[str, str]:
    """把 `sessionid=a; ttwid=b` 形式的字符串拆成字典。"""
    out: Dict[str, str] = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        key, _, item = part.partition("=")
        key, item = key.strip(), item.strip()
        if key and item:
            out[key] = item
    return out


# ------------------------------------------------------------------ HTTP 层

def _error_of(raw: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """取业务错误码与消息。

    两层都要看：顶层 `code` 是网关级，`data.biz_error.code` 是**领取回执**级。
    第一版只看了顶层，于是「回执里 biz_error 非 0」会被当成成功 —— 那正是
    最危险的一种假成功。
    """
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    biz = data.get("biz_error")
    if isinstance(biz, dict):
        code = biz.get("code")
        if code not in (None, 0):
            try:
                return int(code), str(biz.get("message") or biz.get("msg") or "").strip()
            except (TypeError, ValueError):
                return None, str(biz.get("message") or "").strip()
    top = raw.get("code")
    if top not in (None, 0):
        try:
            return int(top), str(raw.get("message") or raw.get("msg") or "").strip()
        except (TypeError, ValueError):
            return None, str(raw.get("message") or "").strip()
    return None, ""


def _raise_for(raw: Dict[str, Any]) -> Dict[str, Any]:
    code, message = _error_of(raw)
    if code is None and not message:
        return raw
    hint = _CODES.get(code) if code is not None else None
    text = f"code={code}" if code is not None else "业务错误"
    if hint:
        text += f"：{hint}"
    elif message:
        # 上游 message 是**外部数据**：截断 + 去掉不可打印字符。两个理由——
        # ① 它可能把日志或界面撑爆；② 万一上游把凭据回显在 message 里，
        #   截断能降低被原样带出去的面。我们自己不会把 cookie 拼进任何异常
        #   （已核对：模块内 4 处 raise 都不含 cookie，也没有 logger/print）。
        safe = "".join(ch for ch in message if ch.isprintable())[:200]
        if safe:
            text += f"：{safe}"
    raise CheckinError(text, code=code, not_open_yet=code == _NOT_OPEN_YET)


async def _post(client: httpx.AsyncClient, path: str, cookies: Dict[str, str],
                body: Dict[str, Any], *, query: str = _QUERY) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    resp = await client.post(
        url,
        content=json.dumps(body),
        headers={
            "Cookie": _cookie_header(cookies),
            "User-Agent": UA,
            "Referer": REFERER,
            "Origin": BASE_URL,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "agw-js-conv": "str",
        },
    )
    if resp.status_code != 200:
        raise CheckinError(f"HTTP {resp.status_code}")
    try:
        raw = resp.json()
    except ValueError:
        raise CheckinError("响应不是 JSON（可能被风控拦截）") from None
    if not isinstance(raw, dict):
        raise CheckinError("响应结构异常")
    return raw


# ------------------------------------------------------------------ 协议动作

def activity_group(launch: Dict[str, Any]) -> str:
    """从 `/alice/user/launch` 的响应里取当前活动组。

    第一版把 `pc_incentive_task_mvp_v3` 写死了。活动是**会换的**，写死的后果是
    官方一换组，签到就静默失败（去读一个已经不存在的活动）。
    """
    data = launch.get("data") if isinstance(launch.get("data"), dict) else {}
    info = data.get("ug_campaign_info") if isinstance(data.get("ug_campaign_info"), dict) else {}
    return str(info.get("campaign_group_code") or "").strip()


def detail_path(group: str) -> str:
    return DETAIL_V3_PATH if group == V3_GROUP else DETAIL_LIST_PATH


def walk_daily_tasks(payload: Dict[str, Any], group: str) -> List[Dict[str, Any]]:
    """递归收集「每日签到」任务，兼容两种判据。

    两份真机取证用了不同字段，必须都认，否则会漏任务：

    * `task_status`：**3 = 今日已签，1 = 未签**（`agent-checkin` 的
      `recipes/doubaowork.yaml` 2026-10-21 真机实录：
      `before {"total":0,"signStatus":1}` → `after {"total":200,"signStatus":3}`）；
    * `daily_checkin.checked_in_today`：布尔（`DoubaoWork-Daily` 用它）。

    只认后者的话，某些账号返回里没有 `daily_checkin` 就会被我判成「没有签到任务」
    —— 那是把「能签」误报成「没得签」，比报错更糟。
    """
    out: List[Dict[str, Any]] = []

    def pick(row: Dict[str, Any], current_group: str) -> Optional[Dict[str, Any]]:
        item = str(row.get("campaign_code") or row.get("task_code") or row.get("code") or "")
        if not item:
            return None
        info = row.get("daily_checkin") if isinstance(row.get("daily_checkin"), dict) else {}
        status = row.get("task_status")
        is_daily = (
            isinstance(info.get("checked_in_today"), bool)
            or item == "daily_signin"
            or str(row.get("task_kind") or "") == "daily_signin"
        )
        if not is_daily:
            return None
        checked: Optional[bool] = None
        if isinstance(info.get("checked_in_today"), bool):
            checked = info["checked_in_today"] is True
        if isinstance(status, int):
            # 3 = 已签；1 = 未签。两者不一致时以「已签」为准：宁可少提交一次，
            # 也不要在能领的时候重复打（真机记录里 claim 的返回值不代表新领到）。
            checked = True if status == 3 else (checked if checked is not None else False)
        if checked is None:
            return None
        days = info.get("continuous_days")
        reward = None
        rw = info.get("reward")
        if isinstance(rw, dict) and isinstance(rw.get("reward_amount"), int):
            reward = rw["reward_amount"]
        last_date = info.get("last_checkin_date")
        total_days = info.get("total_days")
        return {
            "item": item,
            "group": str(row.get("campaign_group_code") or current_group),
            "checked": checked,
            "continuous_days": days if isinstance(days, int) else None,
            # 连签周期长度（真机实测存在，值如 3）。用来说明「连签 N 天额外奖励」，
            # 不参与成功判定。
            "total_days": total_days if isinstance(total_days, int) else None,
            "last_checkin_date": last_date if isinstance(last_date, str) else None,
            "reward_amount": reward,
            "task_status": status,
        }

    def walk(value: Any, depth: int) -> None:
        if depth > 8 or not isinstance(value, dict):
            return
        current_group = (str(value.get("campaign_group_code") or value.get("campaign_code") or "")
                         or group)
        for key in ("tasks", "campaigns", "milestones"):
            rows = value.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                found = pick(row, current_group)
                if found is not None:
                    out.append(found)
        for child in ("data", "result", "ug_campaign_info"):
            if isinstance(value.get(child), dict):
                walk(value[child], depth + 1)
        for group_key in ("campaign_groups", "groups"):
            rows = value.get(group_key)
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        walk(row, depth + 1)

    walk(payload.get("data") if isinstance(payload.get("data"), dict) else payload, 0)
    return out


def _total_amount(payload: Dict[str, Any]) -> Optional[int]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    progress = data.get("progress") if isinstance(data.get("progress"), dict) else {}
    value = progress.get("total_amount")
    return value if isinstance(value, int) else None


def _total_amount_from_list(payload: Dict[str, Any]) -> Optional[int]:
    """`/campaign/list/` 把活动包在 `data.campaign_group` 里，进度也可能在那儿。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    group = data.get("campaign_group") if isinstance(data.get("campaign_group"), dict) else {}
    progress = group.get("progress") if isinstance(group.get("progress"), dict) else {}
    value = progress.get("total_amount")
    return value if isinstance(value, int) else None


async def _read_state(client: httpx.AsyncClient, cookies: Dict[str, str]) -> Dict[str, Any]:
    """launch → detail，返回 {group, detail, tasks, total}。

    ### `launch` 不可依赖（2026-11-16 真机实测）

    在已登录的网页环境里 `POST /alice/user/launch` 返回
    `{"code":710010702,"msg":"系统错误"}` —— 而同一时刻
    `POST /alice/ug/campaign/v3/detail/` 返回 200 且带完整任务表。

    也就是说：**`launch` 会失败，但签到本身可用**。原实现把 launch 当唯一
    发现途径，遇到这个就判 `inactive`，等于永远签不到。所以这里的顺序是：
    先用 launch 试（活动组可能换），**失败就回退到已知组**再读详情。
    """
    group = ""
    launch_error = ""
    try:
        launch = await _post(client, LAUNCH_PATH, cookies, {})
        code, _msg = _error_of(launch)
        if code is None:
            group = activity_group(launch)
        else:
            launch_error = f"launch code={code}"
    except Exception as exc:  # noqa: BLE001 - launch 只是优化路径，失败要能继续
        launch_error = f"launch 异常: {exc}"

    # 回退：launch 拿不到组就用已知组（v3 是当前在跑的；不是猜测，是实测值）
    if not group:
        group = V3_GROUP

    detail = await _post(client, detail_path(group), cookies, {"campaign_group_code": group})
    _raise_for(detail)
    tasks = walk_daily_tasks(detail, group)
    if not tasks and group == V3_GROUP:
        # v3 详情里没有任务时，试 list 端点（它给的是另一个投影，含 campaigns[]）
        try:
            alt = await _post(client, DETAIL_LIST_PATH, cookies,
                             {"campaign_group_code": group})
            code, _ = _error_of(alt)
            if code is None:
                alt_tasks = walk_daily_tasks(alt, group)
                if alt_tasks:
                    detail, tasks = alt, alt_tasks
        except Exception:  # noqa: BLE001
            pass
    return {
        "group": group,
        "detail": detail,
        "tasks": tasks,
        "total": _total_amount(detail) or _total_amount_from_list(detail),
        "launch_error": launch_error,
    }


# ------------------------------------------------------------------ 签到主流程

async def checkin_one(client: httpx.AsyncClient, account: Dict[str, Any],
                      *, recheck_delays: Tuple[float, ...] = _RECHECK_DELAYS) -> Dict[str, Any]:
    """单账号：launch → detail → （未签则）claim → 回查确认。

    状态语义（对齐上游实现）：
      * `already`   今天签过（detail 已勾选，或回查确认已勾选）
      * `claimed`   本次提交并**经回查确认**
      * `confirming` 提交被接受但回查没确认（如需要 SDK 签名）——**不重提**
      * `inactive`  没有活动 / 没有唯一每日任务
      * fail        明确失败
    """
    uid = account["id"]
    name = account.get("name") or uid
    cookies = account["cookies"]
    base = {"uid": uid, "name": name}

    # ① 读状态（launch + detail）
    state = None
    try:
        state = await _read_state(client, cookies)
    except CheckinError as exc:
        return {**base, "ok": False, "status": "fail", "message": f"读取签到状态失败：{exc}"}
    except Exception as exc:  # noqa: BLE001 - 网络异常也要如实上报，不能让整批炸掉
        return {**base, "ok": False, "status": "fail", "message": f"读取签到状态失败：{exc}"}

    if not state["group"]:
        return {**base, "ok": False, "status": "inactive",
                "message": "当前账号没有豆包工作签到活动（launch 未返回活动组）",
                "total_amount": None}
    tasks = state["tasks"]
    if not tasks:
        return {**base, "ok": False, "status": "inactive",
                "message": "活动里没有「每日签到」任务，无需签到",
                "continuous_days": None, "total_amount": state["total"]}
    if len(tasks) > 1:
        # 猜一个去提交 = 可能领错奖励。上游实现遇到多个候选也是报无法确认。
        return {**base, "ok": False, "status": "inactive",
                "message": f"活动里有 {len(tasks)} 个每日签到任务，无法确定该领哪个，未提交",
                "total_amount": state["total"]}
    task = tasks[0]
    if task["checked"]:
        return {**base, "ok": True, "status": "already", "message": "今日已签到",
                "continuous_days": task["continuous_days"],
                "total_days": task.get("total_days"),
                "last_checkin_date": task.get("last_checkin_date"),
                "total_amount": state["total"], "reward": None}

    # ② 提交领取（scene 用**发现的活动组**，不是硬编码）
    try:
        # `biz_params` 带一个空 dict：`niyouqianma/doubaowork2api`（v1.0.0）
        # 的 benefits.py 就是这么发的（`{'scene': group, 'biz_params': {}}`）。
        # 2026-11-16 真机实测：**不传也能签成功**，说明服务端不强制；
        # 但带上更接近官方客户端的请求形状，成本为零。
        receipt = await _post(client, CLAIM_PATH, cookies,
                              {"scene": task["group"], "biz_params": {}})
        _raise_for(receipt)
    except CheckinError as exc:
        if exc.not_open_yet:
            # 服务端还没到今天这一天的窗口。**这不是失败**，也不是账号问题：
            # 真机记录显示开窗时刻逐日漂移（10:45 拒 / 10:55 放行）。
            # 标记 retryable 让调度器在稍后的回合再试。
            return {**base, "ok": False, "status": "not_open_yet", "retryable": True,
                    "message": f"签到未执行：{exc}",
                    "continuous_days": task["continuous_days"],
                    "total_amount": state["total"]}
        return {**base, "ok": False, "status": "fail", "message": f"签到提交失败：{exc}",
                "continuous_days": task["continuous_days"], "total_amount": state["total"]}
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "status": "fail", "message": f"签到提交失败：{exc}",
                "continuous_days": task["continuous_days"], "total_amount": state["total"]}

    # ③ 回查确认：**回执不等于成功**，官方详情的 checked_in_today 才是
    last_error = ""
    for delay in recheck_delays:
        if delay:
            await asyncio.sleep(delay)
        try:
            fresh = await _read_state(client, cookies)
        except Exception as exc:  # noqa: BLE001 - 回查失败不改变「已提交」这个事实
            last_error = str(exc)
            continue
        for row in fresh["tasks"]:
            if row["item"] == task["item"] and row["checked"]:
                reward = row["reward_amount"] if row["reward_amount"] is not None else task["reward_amount"]
                return {**base, "ok": True, "status": "claimed",
                        "message": f"签到成功" + (f" +{reward} 额度" if reward else ""),
                        "reward": reward,
                        "continuous_days": row["continuous_days"] or task["continuous_days"],
                        "total_days": row.get("total_days") or task.get("total_days"),
                        "last_checkin_date": row.get("last_checkin_date"),
                        "total_amount": fresh["total"] or state["total"]}

    # 提交被接受但没确认：保留现场，明确告诉用户「别重试、去客户端看一眼」
    return {**base, "ok": False, "status": "confirming",
            "message": ("签到请求已被上游接受，但回查未确认（可能正在异步刷新）。"
                        "本轮不会重复提交；稍后重跑只会回读状态。"
                        + (f" 回查错误：{last_error}" if last_error else "")),
            "continuous_days": task["continuous_days"], "total_amount": state["total"]}


# ------------------------------------------------------------------ 历史与批量

def _append_history(history_path: Optional[str], results: List[Dict[str, Any]]) -> None:
    """把本轮结果追加到 JSONL 历史（签到记录页读它）。

    为什么不落 `proxy_requests`：那张表记的是**推理请求**，签到不是一次推理，
    混进去会把「今日请求数」算错。写失败不影响签到结论。
    """
    if not history_path:
        return
    try:
        path = Path(history_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            for item in results:
                if not isinstance(item, dict):
                    continue
                handle.write(json.dumps({
                    "ts": stamp,
                    "platform": "doubaowork",
                    "uid": str(item.get("uid") or ""),
                    "nickname": str(item.get("name") or ""),
                    "status": str(item.get("status") or ""),
                    "ok": bool(item.get("ok")),
                    "message": str(item.get("message") or ""),
                    "reward": item.get("reward"),
                    "continuous_days": item.get("continuous_days"),
                }, ensure_ascii=False) + "\n")
    except OSError:
        return


def read_history(history_path: str, *, days: int = 30,
                 limit: int = 100) -> List[Dict[str, Any]]:
    """读签到历史（最新在前）。"""
    cutoff = (dt.datetime.now() - dt.timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%S")
    rows: List[Dict[str, Any]] = []
    try:
        with open(history_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                if str(item.get("ts") or "") < cutoff:
                    continue
                rows.append(item)
    except (OSError, UnicodeError):
        return []
    rows.reverse()
    return rows[:max(1, min(1000, limit))]


async def checkin_all(accounts_dir: str, *, gap_seconds: float = 0.8,
                      timeout: float = 15.0,
                      history_path: Optional[str] = None) -> Dict[str, Any]:
    """批量签到。账号之间留间隔（同一 IP 连续打多个账号最容易吃风控）。"""
    accounts = [a for a in read_accounts(accounts_dir) if a.get("enabled", True)]
    if not accounts:
        return {
            "ok": False,
            "unsupported": False,
            "results": [],
            "message": f"账号目录里没有可签到的账号（{accounts_dir}）："
                       "要么还没导入账号，要么文件里没有 sessionid",
            "total": 0, "succeeded": 0, "already": 0, "failed": 0,
        }
    results: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for index, account in enumerate(accounts):
            if index:
                await asyncio.sleep(max(0.0, float(gap_seconds)))
            results.append(await checkin_one(client, account))
    claimed = len([r for r in results if r.get("status") == "claimed"])
    already = len([r for r in results if r.get("status") == "already"])
    confirming = len([r for r in results if r.get("status") == "confirming"])
    not_open = len([r for r in results if r.get("status") == "not_open_yet"])
    # 「尚未开放」不计入失败：它是可重试的时间问题，不是账号坏了。
    failed = len([r for r in results
                  if not r.get("ok") and r.get("status") not in ("confirming", "not_open_yet")])
    reward_total = sum(int(r.get("reward") or 0) for r in results)
    parts: List[str] = []
    if claimed:
        parts.append(f"新签 {claimed} 个" + (f"，累计 +{reward_total} 额度" if reward_total else ""))
    if already:
        parts.append(f"今日已签 {already} 个")
    if not_open:
        parts.append(f"尚未开放 {not_open} 个（服务端按天开窗，稍后重试）")
    if confirming:
        parts.append(f"待确认 {confirming} 个（未重复提交）")
    if failed:
        first = next((r.get("message") for r in results
                      if not r.get("ok") and r.get("status") not in ("confirming", "not_open_yet")), "")
        parts.append(f"失败 {failed} 个" + (f"；首个原因：{first}" if first else ""))
    message = "签到完成：" + "，".join(parts) if parts else "本次没有账号需要签到"
    _append_history(history_path, results)
    return {
        "ok": failed == 0 and (claimed + already) > 0,
        "unsupported": False,
        "results": results,
        "message": message,
        "total": len(results), "succeeded": claimed, "already": already,
        "failed": failed, "confirming": confirming, "not_open_yet": not_open,
        "reward_total": reward_total,
        "retryable": not_open > 0,
        "checked_at": int(time.time()),
    }
