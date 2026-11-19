"""豆包工作（Doubao Work）引擎适配器。

为什么要有它
------------
豆包工作（Doubao Work）是字节跳动的 AI 办公产品。2026-11-12 起它开放接入
第三方模型（DeepSeek / Kimi / GLM），社区随即出现把客户端「工作模式」的
`/chat/completion` 协议包装成 OpenAI 兼容接口的反代实现。

qtg 对它的定位与 workbuddy **完全同构**：反代引擎自己管账号池、冷却与并发
调度，qtg 只做「聚合展示 + 受控写入 + 统一入口」，不重造那套状态机。

对接面（读自 doubaowork2api 0.3.0 源码，不是猜测）
--------------------------------------------------
| 用途 | 入口 |
|---|---|
| 健康 | `GET /health` |
| 管理登录 | `POST /admin/login {"password": ...}` → Set-Cookie（path=/admin） |
| 配置与模型 | `GET /admin/config` |
| 账号池 | `GET /admin/accounts` → `{"data": [...]}` |
| 密钥 | `GET /admin/keys` → `{"data": [...]}` |
| 请求日志 | `GET /admin/logs?limit=N` → `{"data": [...]}` |
| 模型（数据面） | `GET /v1/models`（需 API Key，不是管理会话） |
| 启停账号 | `PATCH /admin/accounts/{id} {"enabled": bool}` |
| 清冷却 | `POST /admin/accounts/{id}/clear-cooldown` |
| 导入账号 | `POST /admin/accounts/import` |
| 批量操作 | `POST /admin/accounts/batch` |
| 数据面 | `POST /v1/chat/completions`、`POST /v1/responses` |

上游**没有**的能力（不模拟、不补零、不假装已执行）
--------------------------------------------------
1. **没有签到/积分领取接口**。`checkin()` 明确返回不支持，不返回
   「成功 0 / 0」这种看起来像跑过一次的结果——那正是本项目反复批评的
   「把未发生的事渲染成已发生」。
2. **没有额度字段**。账号对象只有 `enabled / available / state /
   cooldown_remaining_seconds / last_error / group`，**不含剩余积分**。
   因此 `map_doubaowork_account()` 把 `credits_*` 留在 0 且 `note`
   写明「上游不提供额度」，而不是编一个数字。
3. **没有用量统计端点**。请求日志只有 `started_at / duration_ms / model /
   status`，**没有 token 计数**。`usage()` 因此返回空列表 + 说明，
   让前端显示「上游未提供」，而不是伪造一条 0 用量曲线。

会话与 CSRF
-----------
doubaowork2api 的 `require_admin` 会调用 `check_origin()`：**仅当请求带
`Origin` 头**时才比对 scheme/netloc，且拒绝 `Sec-Fetch-Site: cross-site`。
服务端到服务端调用不带这两个头，因此正常通过；这里也**不主动添加**
`Origin`，以免把自己伪装成浏览器来源。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..errors import UpstreamError
from .base import BaseAdapter

# 上游账号 id 会被拼进 URL 路径。只接受可安全路径化的字符，避免把用户可控的
# id 变成路径穿越。**首字符不许是 `.`**：否则 `..` 能通过校验，拼出
# `/admin/accounts/..`，被 HTTP 客户端规范化成 `/admin` 打到别的端点。
# 上游 id 形如 `<slug>-<sha256>`，不含前导点，这条限制不影响真实数据。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}$")

# 上游 `state` 取值 → qtg `AccountState`。`busy` 不是异常状态，
# 只是该账号正在服务一个请求，映射为 `ready` 并由 `in_flight` 表达。
# 上游 `state` 的归一化（busy→ready 等）**不在这里做**：适配器只负责把上游
# 的原始字段如实取回来，业务映射在 `service.map_doubaowork_account()` 一处完成。
# 这里曾经有一份 `_STATE_MAP` 常量，但从未被引用，而 service 里另写了一套
# if/elif —— 两套口径迟早会漂移，所以删掉，只留一处。


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "")[:limit]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.match(text):
        raise ValueError("invalid doubao work account id")
    return text


class DoubaoWorkAdapter(BaseAdapter):
    name = "doubaowork"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.doubaowork_base, settings)
        self.cfg = settings
        # 写入串行化：上游对并发写有 409（"有请求正在执行"）与文件锁，
        # qtg 侧先自己排好队，避免把 409 当成用户错误抛给前端。
        self._write_lock = asyncio.Lock()
        # 上游版本（`/health` 的 version 字段）。空串 = 未知/老版本，
        # 此时所有 v1 专有能力（analytics/checkin/quota）都按「不支持」降级。
        self._upstream_version = ""

    # ---------------------------------------------------------------- 鉴权
    def _credentials(self) -> str:
        try:
            return Path(self.cfg.doubaowork_admin_password_file).read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            return ""

    async def authenticate(self) -> None:
        password = self._credentials()
        if not password:
            raise UpstreamError(
                f"豆包工作管理密码缺失: {self.cfg.doubaowork_admin_password_file}"
            )
        # auth=False：登录本身就是建立会话，不能触发 ensure_auth 递归。
        resp = await self._request(
            "POST", "/admin/login", json_body={"password": password},
            retry=False, auth=False,
        )
        if resp.status_code == 401:
            raise UpstreamError("豆包工作管理密码不正确")
        if resp.status_code == 429:
            raise UpstreamError("豆包工作管理登录失败次数过多，已被限流")
        if resp.status_code != 200:
            # **不要**拼接 resp.text：上游是 FastAPI，422 会把请求体（含明文
            # 管理密码）回显在 detail.input 里，而这条异常会进 last_error、
            # health 快照、前端错误提示和审计详情。只报状态码。
            raise UpstreamError(
                f"豆包工作管理登录被上游拒绝（HTTP {resp.status_code}）"
            )

    # ------------------------------------------------------------ 管理通道
    async def _admin(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[Dict[str, Any]] = None,
        retry: bool = True,
        required: bool = True,
    ) -> Any:
        """走管理会话的 JSON 调用。

    **401 自动重登只在读路径成立**。`base.py` 的重登条件是
    `401 and auth and retry and attempt == 0 and attempt+1 < attempts`，而
    `retry` 本身又被收窄成「GET/HEAD 或白名单里的 POST」。写路径
    （`set_account`/`add_account`/`clear-cooldown`）一律传 `retry=False`，
    所以它们遇到 401 会直接抛 UpstreamError，不会静默重试。

    这是有意的：一次写操作要不要重试，取决于它是否幂等，不能靠框架默认行为。
    """
        return await self._json(
            method, path, json_body=json_body, params=params,
            retry=retry, required=required,
        )

    async def _admin_data(
        self, method: str, path: str, *, json_body: Any = None,
        params: Optional[Dict[str, Any]] = None, retry: bool = True,
        required: bool = True,
    ) -> Any:
        """解析 `{"data": ...}` 信封；信封缺失即视为契约破坏，不猜。

        `required=False` 时上游失败返回 `None`，由调用方决定降级方式。
        """
        body = await self._admin(method, path, json_body=json_body, params=params,
                                 retry=retry, required=required)
        if body is None:
            return None
        if not isinstance(body, dict) or "data" not in body:
            raise UpstreamError(f"豆包工作 {method} {path} 响应缺少 data 字段")
        return body["data"]

    # ---------------------------------------------------------------- 健康
    async def healthz(self) -> Any:
        """`/health` 不需要管理会话——它也是进程存活的最轻探针。"""
        body = await self._json("GET", "/health", auth=False, required=False)
        # 顺带记住上游版本：v1.0.0（niyouqianma/doubaowork2api）多了
        # `/admin/analytics`、`/admin/checkin`、`/quota` 等能力，而
        # v0.3.0 没有。下面几个方法据此降级，不去猜。
        if isinstance(body, dict):
            version = body.get("version")
            if isinstance(version, str) and version:
                self._upstream_version = version
        return {"ok": isinstance(body, dict) and body.get("ok") is True}

    @property
    def upstream_version(self) -> str:
        return self._upstream_version

    def _supports_v1(self) -> bool:
        """上游是否是 v1 系列（带 analytics/checkin/quota 的那版）。

        判据只有一处：`/health` 里 `version` 的主版本号 >= 1。
        没有 version 字段（v0.3.0）一律当不支持 —— **不靠探测端点猜**，
        因为「端点存在但返回 404/405」和「端点不存在」在 httpx 里长得一样。
        """
        raw = self._upstream_version.strip()
        if not raw:
            return False
        head = raw.split(".", 1)[0]
        try:
            return int(head) >= 1
        except (TypeError, ValueError):
            return False

    async def health(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            probe = await self.healthz() or {}
        except Exception as exc:  # noqa: BLE001 - 健康探针任何异常都算不可用
            return {
                "up": False,
                "latency_ms": None,
                "last_error": str(exc)[:200],
                "engine": "doubaowork",
            }
        return {
            "up": bool(probe.get("ok")),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "last_error": "" if probe.get("ok") else "health_not_ok",
            "engine": "doubaowork",
        }

    # ---------------------------------------------------------------- 只读
    async def _accounts(self) -> List[Dict[str, Any]]:
        rows = await self._admin_data("GET", "/admin/accounts")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise UpstreamError("豆包工作账号列表格式无效")
        return rows

    async def overview(self) -> Dict[str, Any]:
        """qtg 的 `map_doubaowork_account()` 期望 `{"accounts": [...]}`。

        这里**原样透传**上游字段，不在这里做任何「看起来更好」的加工：
        映射只发生在 `service.map_doubaowork_account()` 一处，避免两套口径。
        """
        return {"accounts": await self._accounts(), "checkin": []}

    async def _checkin_via_upstream(self) -> Dict[str, Any]:
        """调 v1 上游的 `POST /admin/checkin`（批量）。

        上游的 `AccountSelection` 把 `ids` 定为必填（传 `{}` 会 422），
        所以先从 `/admin/accounts` 取 id；取不到就传 `null` 让上游自己
        挑所有 available 账号（它的 `payload or [...]` 分支）。

        上游状态 → qtg 契约：

        | 上游 status | 这里 | 理由 |
        |---|---|---|
        | `done` / `claimed` | `claimed` | 已回查确认的签到 |
        | `skipped` | `already` | 账号未启用/冷却/今日已签 |
        | `busy` | `confirming` | 该账号正在签，**别重复提交** |
        | `manual_required` | `confirming` | 710010702 那类，要人工看一眼 |
        | `pending` | `confirming` | 提交了但没确认 |
        | `verification` / `error` | `fail` | 真失败 |

        `confirming` 一律 `ok=False`、`retryable=False` —— 它既不是成功也不是
        可以无脑重试的失败，界面该显示「待确认」。
        """
        ids: List[str] = []
        try:
            rows = await self._accounts()
            ids = [str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")]
        except Exception:  # noqa: BLE001 - 拿不到 id 就让上游自己挑
            ids = []
        body: Optional[Dict[str, Any]] = {"ids": ids} if ids else None

        try:
            raw = await self._admin("POST", "/admin/checkin", json_body=body,
                                    required=False)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "unsupported": False, "results": [],
                "message": f"调用上游签到接口失败：{str(exc)[:200]}",
                "total": 0, "succeeded": 0, "already": 0, "failed": 0,
            }
        rows = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return {
                "ok": False, "unsupported": False, "results": [],
                "message": "上游签到接口没有返回 results（v1 契约不符，qtg 不猜）",
                "total": 0, "succeeded": 0, "already": 0, "failed": 0,
            }

        mapping = {
            "done": ("claimed", True),
            "claimed": ("claimed", True),
            "skipped": ("already", True),
            "busy": ("confirming", False),
            "pending": ("confirming", False),
            "manual_required": ("confirming", False),
            "verification": ("fail", False),
            "error": ("fail", False),
        }
        results: List[Dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            upstream_status = str(item.get("status") or "")
            status, ok = mapping.get(upstream_status, ("fail", False))
            results.append({
                "uid": str(item.get("id") or ""),
                "name": str(item.get("name") or item.get("id") or ""),
                "ok": ok,
                "status": status,
                "upstream_status": upstream_status,
                "message": str(item.get("message") or ""),
                "code": item.get("code"),
                "reward": item.get("points"),
                "day": item.get("day"),
                "retryable": status == "fail" and upstream_status == "error",
            })
        claimed = len([r for r in results if r["status"] == "claimed"])
        already = len([r for r in results if r["status"] == "already"])
        confirming = len([r for r in results if r["status"] == "confirming"])
        failed = len([r for r in results if r["status"] == "fail"])
        parts: List[str] = []
        if claimed:
            parts.append(f"新签 {claimed} 个")
        if already:
            parts.append(f"今日已签/跳过 {already} 个")
        if confirming:
            parts.append(f"待确认 {confirming} 个（未重复提交）")
        if failed:
            first = next((r["message"] for r in results
                          if r["status"] == "fail" and r["message"]), "")
            parts.append(f"失败 {failed} 个" + (f"；首个原因：{first}" if first else ""))
        return {
            "ok": failed == 0 and (claimed + already) > 0,
            "unsupported": False,
            "results": results,
            "message": ("签到完成：" + "，".join(parts)) if parts else "本次没有账号需要签到",
            "total": len(results), "succeeded": claimed, "already": already,
            "failed": failed, "confirming": confirming,
            "source": "upstream",
            "checked_at": int(time.time()),
        }

    async def _checkin_via_local(self) -> Dict[str, Any]:
        """占位：本地签到的入口在 `checkin()` 里（保持原有实现不动）。"""
        raise NotImplementedError("local checkin runs inline in checkin()")

    async def diagnostic_pool(self) -> Dict[str, Any]:
        rows = await self._accounts()
        return {"accounts": rows, "scope": "engine_account_list"}

    async def usage(self, days: int = 14) -> Dict[str, Any]:
        """用量。

        **v1 上游**（`niyouqianma/doubaowork2api`）有 `GET /admin/analytics`，
        返回 `by_day` / `by_model` / `by_account` / `by_key`；其中 `by_day`
        在 SQL 里按 `date(at,'unixepoch','+8 hours')` 分桶 —— 就是北京时间，
        正好对得上 qtg 的按天视图，不需要再做时区换算。

        **v0.3.0** 没有这个端点，那时如实报 `unsupported`，**不补零**：
        一条看起来真实的 0 曲线比「上游未提供」更误导。
        """
        if not self._supports_v1():
            return {
                "usage": [],
                "credits_daily": [],
                "unsupported": ["usage", "credits"],
                "note": "豆包工作反代未提供用量与额度统计接口，qtg 不伪造这两项",
            }
        window = max(1, min(30, int(days or 7)))  # 上游硬限制 1~30
        try:
            data = await self._admin("GET", "/admin/analytics",
                                     params={"days": window}, required=False)
        except Exception:  # noqa: BLE001 - 拿不到就降级，不让统计页挂掉
            data = None
        if not isinstance(data, dict):
            return {
                "usage": [],
                "credits_daily": [],
                "unsupported": ["usage", "credits"],
                "note": "上游 /admin/analytics 本次不可用，qtg 不伪造用量",
            }
        rows: List[Dict[str, Any]] = []
        for item in (data.get("by_day") or []):
            if not isinstance(item, dict):
                continue
            prompt_tokens = int(item.get("input_tokens") or 0)
            completion_tokens = int(item.get("output_tokens") or 0)
            rows.append({
                "day": str(item.get("id") or ""),
                "requests": int(item.get("requests") or 0),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "failed": int(item.get("errors") or 0),
                "credit": 0.0,  # 上游不给额度消耗折算，保持 0 但不当作「已知为 0」
            })
        return {
            "usage": rows,
            "credits_daily": [],
            "unsupported": ["credits"],
            "by_model": data.get("by_model") or [],
            "note": ("按天用量来自上游 /admin/analytics（北京时间分桶）；"
                     "额度折算上游不提供，本次不补"),
        }

    async def settings(self) -> Dict[str, Any]:
        config = await self._admin("GET", "/admin/config", required=False)
        config = config if isinstance(config, dict) else {}
        return {
            "scheduler": {
                # 上游没有内置定时签到，也没有自动调度开关；
                # `enabled=False` 是事实描述，不是「坏了」。
                "enabled": False,
                "mode": "豆包工作反代无内置调度",
                "tasks": [],
            },
            "pool": {
                "supported": True,
                "scope": "engine_account_list",
            },
            "raw": config,
        }

    async def keys(self) -> Dict[str, Any]:
        rows = await self._admin_data("GET", "/admin/keys", required=False)  # type: ignore[call-arg]
        keys = rows if isinstance(rows, list) else []
        return {"data": {"auth_disabled": False, "keys": keys}}

    async def models(self) -> Dict[str, Any]:
        """模型清单来自 `/admin/config`（管理会话即可读），不需要数据面 Key。

        2026-11-20 修：上游卡片用的字段名是 `context_window`（`/admin/config`
        实测返回 `{"id","name","upstream_key","mode","price","description",
        "context_window","reasoning_efforts","image_input",...}`），而
        `compat_fixes.fixed_models` 读的是 `context_length`。不转换的直接后果
        是模型页上每个模型的上下文长度都显示 **0** —— 用户读成「这个模型不可用」。
        `max_output_tokens` 上游没有，保持 None（不猜一个数字出来）。
        """
        config = await self._admin("GET", "/admin/config", required=False)
        models = config.get("models") if isinstance(config, dict) else None
        if not isinstance(models, list):
            return {"data": []}
        out: List[Dict[str, Any]] = []
        for item in models:
            if isinstance(item, dict):
                card = dict(item)
            else:
                card = {"id": str(item)}
            if not card.get("id"):
                continue
            # `context_window` → `context_length`：上游字段名与 qtg 契约不同名，
            # 在这里一次性归一，别让下游各自再猜一遍。
            if not card.get("context_length") and card.get("context_window"):
                try:
                    card["context_length"] = int(card["context_window"])
                except (TypeError, ValueError):
                    card["context_length"] = 0
            # 上游的 `reasoning_efforts` 对应 qtg 的 `efforts`。
            if not card.get("efforts") and card.get("reasoning_efforts"):
                card["efforts"] = list(card["reasoning_efforts"])
            out.append(card)
        return {"data": out}

    async def logs(self, limit: int = 150) -> Dict[str, Any]:
        cap = max(1, min(int(limit), 200))
        rows = await self._admin_data("GET", "/admin/logs", params={"limit": cap}, required=False)  # type: ignore[call-arg]
        entries = rows if isinstance(rows, list) else []
        out: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            started = entry.get("started_at")
            ts = ""
            if isinstance(started, (int, float)) and started > 0:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
            status = _text(entry.get("status"), 16)
            error = _text(entry.get("error"), 200)
            out.append({
                "ts": ts,
                "level": "error" if status == "error" else "info",
                "msg": _text(
                    f"{entry.get('model') or '?'} {status or '?'} "
                    f"{entry.get('duration_ms') if entry.get('duration_ms') is not None else '?'}ms"
                    f"{(' ' + error) if error else ''}",
                    300,
                ),
            })
        return {"data": out}

    # ---------------------------------------------------------------- 写入
    async def checkin(self) -> Dict[str, Any]:
        """豆包每日签到。

        **两条路，按上游版本自动选**：

        1. **v1 上游**（`niyouqianma/doubaowork2api`）自带
           `POST /admin/checkin` —— 那是官方接口的封装，而且**不需要** qtg
           去读明文账号文件拿 cookie。优先用它。
        2. **v0.3.0** 的 25 条路由里没有签到 —— 退回到我们自己的纯 HTTP 实现
           （`doubao_checkin.py`，2026-11-16 真机签成功过）。

        `auto` 是唯一行为：能走上游就走上游。**不设开关**，因为两个分支的
        对外契约（`{ok, results[], message, succeeded, already, failed}`）
        完全一致，调用方不需要知道走的是哪条。

        **失败如实上报**：cookie 过期/风控/没有活动任务都会原样带出来，
        不会为了界面上有个绿勾而编造成功。
        """
        if self._supports_v1():
            return await self._checkin_via_upstream()

        from . import doubao_checkin

        accounts_dir = str(getattr(self.cfg, "doubaowork_accounts_dir", "") or "")
        if not accounts_dir:
            return {
                "ok": False, "unsupported": True,
                "results": [{"nickname": "豆包工作", "ok": False, "status": "unsupported",
                             "message": "未配置 QTG_DOUBAOWORK_ACCOUNTS_DIR，无法读取账号凭证"}],
            }
        # 历史落的独立文件：签到记录页读它。不混进 proxy_requests（那是推理流水）。
        history = str(Path(str(getattr(self.cfg, "data_dir", "") or "") or ".")
                      / "doubaowork_checkin.jsonl")
        return await doubao_checkin.checkin_all(accounts_dir, history_path=history)

    async def refresh_account(self, uid: str = "") -> Dict[str, Any]:
        """「刷新」在这里的语义是**清除上游冷却**（没有 token 可续期）。"""
        if not uid:
            return {"ok": False, "error": "缺少账号 id"}
        try:
            account_id = _safe_id(uid)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        data = await self._admin_data(
            "POST", f"/admin/accounts/{account_id}/clear-cooldown", retry=False,
        )
        return {"ok": True, "data": data}

    async def set_account(self, uid: str, enabled: bool) -> Dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        account_id = _safe_id(uid)
        async with self._write_lock:
            data = await self._admin_data(
                "PATCH", f"/admin/accounts/{account_id}",
                json_body={"enabled": enabled}, retry=False,
            )
        observed = data.get("enabled") if isinstance(data, dict) else None
        # 回读是「写入成功」的唯一证据。上游没回这个字段 = 没校验过，
        # 不能把「没校验」当成「校验通过」——那会让一次静默失效显示成成功。
        if observed is None:
            return {
                "ok": False,
                "state": "unknown",
                "changed": False,
                "error": "上游未返回启停状态，无法确认变更；未自动重试",
                "data": data,
            }
        if bool(observed) != enabled:
            return {
                "ok": False,
                "state": "unknown",
                "changed": False,
                "error": "上游回读的启停状态与请求不一致，请核对；未自动重试",
                "data": data,
            }
        return {"ok": True, "state": "completed", "changed": True, "data": data}

    async def add_account(self, name: str, jwt: str) -> Dict[str, Any]:
        """`jwt` 参数在这里承载**账号文档 JSON**（cookie + device_id 等）。

        上游只接受它自己的账号文档结构，qtg 不做「宽容猜测」：解析失败就
        明确报错，而不是把一个空账号塞进池子。
        """
        import json

        raw = (jwt or "").strip()
        if not raw:
            raise ValueError("缺少账号文档")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"账号文档不是合法 JSON: {exc}") from exc
        if isinstance(document, list):
            documents = document
        else:
            documents = [document]
        if name:
            for item in documents:
                if isinstance(item, dict) and not item.get("name"):
                    item["name"] = name
        async with self._write_lock:
            result = await self._admin(
                "POST", "/admin/accounts/import",
                json_body={"accounts": documents}, retry=False,
            )
        if not isinstance(result, dict):
            return {"ok": False, "error": "上游导入响应格式无效"}
        errors = result.get("errors") or []
        if errors:
            return {"ok": False, "error": "; ".join(_text(e) for e in errors[:3]),
                    "imported": result.get("imported") or []}
        return {"ok": True, "created": _int(result.get("created")),
                "updated": _int(result.get("updated")),
                "imported": result.get("imported") or []}

    async def save_keys(
        self, keys: List[Dict[str, Any]], auth_disabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """密钥维护**交给上游自己的面板**。

        理由与 trae2 相同：qtg 的密钥模型（平台/模型/额度/并发）与上游
        （模型白名单 + 账号白名单）不是同一套语义，静默改写会丢失约束。
        """
        return {
            "ok": False,
            "error": "豆包工作的 API Key 请在它自己的管理面板维护（qtg 只读展示）",
        }

    async def oauth_login_url(self) -> Dict[str, Any]:
        return {"ok": False, "error": "豆包工作使用浏览器 Cookie 导入，没有 OAuth 授权流程"}

    async def oauth_finish(self, callback_url: str, name: str = "") -> Dict[str, Any]:
        return {"ok": False, "error": "豆包工作使用浏览器 Cookie 导入，没有 OAuth 授权流程"}
