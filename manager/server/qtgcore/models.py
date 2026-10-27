"""对外统一领域模型。

上游 BFF 的字段名各异且不稳定（Qoder 用 `credits.remain`、Trae 用
`remaining_credits`），对外必须收敛成一套稳定契约，否则前端每接一个新平台
就要写一套映射。这里用 pydantic 模型显式声明契约，FastAPI 据此生成 OpenAPI。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Platform = Literal["qoder", "trae", "workbuddy"]
AccountState = Literal["ready", "cooling", "disabled", "low", "expired", "unknown"]


class Account(BaseModel):
    platform: Platform
    uid: str
    name: str = ""
    realm: str = ""
    enabled: bool = True
    state: AccountState = "unknown"
    credits_remain: float = 0
    credits_total: float = 0
    credits_used: float = 0
    expires_at: Optional[int] = None
    expires_iso: Optional[str] = None
    days_left: Optional[int] = None
    checked_today: bool = False
    last_checkin: Optional[str] = None
    plan: str = ""
    in_flight: Optional[int] = None
    in_flight_cap: Optional[int] = None
    group: Optional[str] = None
    in_cooldown: bool = False
    # 是否真的在该上游的账号池里（None = 该平台没有池概念）
    in_pool: Optional[bool] = None
    cooldown_until: Optional[str] = None
    errors: int = 0
    note: str = ""


class AccountSummary(BaseModel):
    total: int = 0
    ready: int = 0
    cooling: int = 0
    disabled: int = 0
    low: int = 0
    expiring_soon: int = 0
    credits: float = 0


class AccountsResponse(BaseModel):
    generated_at: str
    qoder: List[Account] = Field(default_factory=list)
    trae: List[Account] = Field(default_factory=list)
    workbuddy: List[Account] = Field(default_factory=list)
    summary: Dict[str, AccountSummary] = Field(default_factory=dict)


class UsagePoint(BaseModel):
    date: str
    requests: int = 0
    ok: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class UsageByModel(BaseModel):
    platform: Platform
    model: str
    requests: int = 0
    tokens: int = 0
    credit: float = 0
    # 新引擎（qoder2 / aiwork）给得出输入输出拆分；旧 BFF 只给合计时保持 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class UsageResponse(BaseModel):
    generated_at: str
    days: int
    points: Dict[str, List[UsagePoint]] = Field(default_factory=dict)
    by_model: List[UsageByModel] = Field(default_factory=list)
    credits: List[Dict[str, Any]] = Field(default_factory=list)
    totals: Dict[str, Any] = Field(default_factory=dict)


class KeyInfo(BaseModel):
    platform: Platform
    id: str = ""
    name: str = ""
    masked: str = ""
    enabled: bool = True
    realm: str = ""
    source: str = ""
    created_at: str = ""
    daily_limit: Optional[int] = None
    note: str = ""


class KeysResponse(BaseModel):
    qoder: List[KeyInfo] = Field(default_factory=list)
    trae: List[KeyInfo] = Field(default_factory=list)


class ModelInfo(BaseModel):
    platform: Platform
    id: str
    name: str = ""
    context_length: Optional[int] = None
    max_tokens: Optional[int] = None
    vision: bool = False
    tool_calls: bool = False
    reasoning: bool = False
    enabled: bool = True


class ModelsResponse(BaseModel):
    qoder: List[ModelInfo] = Field(default_factory=list)
    trae: List[ModelInfo] = Field(default_factory=list)


class LogEntry(BaseModel):
    platform: Platform
    ts: str = ""
    level: str = ""
    message: str = ""
    raw: Dict[str, Any] = Field(default_factory=dict)


class LogsResponse(BaseModel):
    qoder: List[LogEntry] = Field(default_factory=list)
    trae: List[LogEntry] = Field(default_factory=list)


class Alert(BaseModel):
    id: str = ""
    ts: str = ""
    platform: Platform
    severity: str = "info"
    title: str = ""
    detail: str = ""
    acked: bool = False


class AlertsResponse(BaseModel):
    alerts: List[Alert] = Field(default_factory=list)


class SchedulerInfo(BaseModel):
    platform: Platform
    enabled: Optional[bool] = None
    mode: str = ""
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    tasks: List[Dict[str, Any]] = Field(default_factory=list)


class TaskInfo(BaseModel):
    name: str
    platform: str
    schedule: str = ""
    status: str = ""
    next_run: Optional[str] = None
    last_run: Optional[str] = None


class TasksResponse(BaseModel):
    qoder: SchedulerInfo = Field(default_factory=lambda: SchedulerInfo(platform="qoder"))
    trae: SchedulerInfo = Field(default_factory=lambda: SchedulerInfo(platform="trae"))
    workbuddy: SchedulerInfo = Field(
        default_factory=lambda: SchedulerInfo(platform="workbuddy"))
    systemd: List[TaskInfo] = Field(default_factory=list)


class RequestRow(BaseModel):
    ts: str = ""
    platform: Platform
    model: str = ""
    key_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    credit: Optional[float] = None
    elapsed_ms: Optional[int] = None
    ttft_ms: Optional[int] = None
    stream: bool = False


class RequestsResponse(BaseModel):
    rows: List[RequestRow] = Field(default_factory=list)


class HealthStatus(BaseModel):
    name: str
    url: str
    up: bool
    latency_ms: Optional[int] = None
    status_code: Optional[int] = None
    error: str = ""


class HealthResponse(BaseModel):
    generated_at: str
    self: str = "ok"
    upstreams: List[HealthStatus] = Field(default_factory=list)
    overall: bool = False


class BackupEntry(BaseModel):
    name: str
    size: int = 0
    mtime: str = ""


class OpsResponse(BaseModel):
    generated_at: str
    limits: Dict[str, Any] = Field(default_factory=dict)
    notify: Dict[str, Any] = Field(default_factory=dict)
    backups: Dict[str, List[BackupEntry]] = Field(default_factory=dict)
    timers: List[str] = Field(default_factory=list)
    audit: Dict[str, Any] = Field(default_factory=dict)


class SettingsResponse(BaseModel):
    generated_at: str
    qoder: Dict[str, Any] = Field(default_factory=dict)
    trae: Dict[str, Any] = Field(default_factory=dict)
    qtg: Dict[str, Any] = Field(default_factory=dict)


class StatCard(BaseModel):
    key: str
    label: str
    value: str
    sub: str = ""
    tone: str = ""  # "" | "ok" | "warn" | "bad"


class OverviewResponse(BaseModel):
    generated_at: str
    degraded: bool = False
    errors: List[str] = Field(default_factory=list)
    stats: List[StatCard] = Field(default_factory=list)
    accounts: AccountsResponse
    usage: UsageResponse
    tasks: TasksResponse
    health: HealthResponse
    qoder_scheduler: SchedulerInfo
    random_checkin: Dict[str, Any] = Field(default_factory=dict)
    # 签到明细（旧面板有、新面板之前缺失）
    checkin_log: List[Dict[str, Any]] = Field(default_factory=list)
    # 近 14 天失败数与失败趋势（供概览图例/虚线）
    failures: List[int] = Field(default_factory=list)
    failure_total: int = 0
    sticky_sessions: int = 0
    storage_mode: str = "local-file"


class ActionResult(BaseModel):
    ok: bool = True
    action: str
    target: str = ""
    message: str = "ok"
    data: Any = None


class AuditRecord(BaseModel):
    ts: str
    epoch: float
    action: str
    actor: str
    target: str = ""
    result: str = "ok"
    request_id: str = ""
    ip: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)
    prev: str
    hash: str


class AuditResponse(BaseModel):
    records: List[AuditRecord] = Field(default_factory=list)
    integrity: Dict[str, Any] = Field(default_factory=dict)
