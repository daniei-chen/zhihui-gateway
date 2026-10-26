"""集中配置：全部经环境变量注入，带类型校验与安全默认值。

设计原则（借鉴 WorkBuddy Manager 的 systemd drop-in 即 ADR 做法）：
* 每个开关的默认值都必须能在「什么都不配」的情况下安全启动；
* 路径类配置只允许绝对路径，避免 cwd 变化导致数据写错位置；
* 密钥默认从文件读取（`*_TOKEN_FILE`），不鼓励把密钥塞进环境变量。

实现说明：刻意不引入 pydantic-settings —— 本机 pip 有强哈希校验策略，
新增依赖不可靠；用 pydantic + 显式 env 解析即可满足需求，且更容易审计。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def _env_str(name: str, default: str) -> str:
    v = os.environ.get("QTG_" + name.upper())
    return default if v is None or v == "" else v


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get("QTG_" + name.upper())
    if v is None or v == "":
        return default
    v = v.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"QTG_{name.upper()} 不是合法布尔值: {v!r}")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    v = os.environ.get("QTG_" + name.upper())
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError as exc:
        raise ValueError(f"QTG_{name.upper()} 不是整数: {v!r}") from exc
    if not lo <= n <= hi:
        raise ValueError(f"QTG_{name.upper()} 超出范围 [{lo},{hi}]: {n}")
    return n


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    v = os.environ.get("QTG_" + name.upper())
    if v is None or v == "":
        return default
    try:
        n = float(v)
    except ValueError as exc:
        raise ValueError(f"QTG_{name.upper()} 不是数字: {v!r}") from exc
    if not lo <= n <= hi:
        raise ValueError(f"QTG_{name.upper()} 超出范围 [{lo},{hi}]: {n}")
    return n


def _abs(name: str, default: str) -> str:
    v = os.environ.get("QTG_" + name.upper()) or default
    p = Path(v)
    if not p.is_absolute():
        raise ValueError(f"QTG_{name.upper()} 必须是绝对路径: {v!r}")
    return str(p)


def _file_switch(path: str, allowed: tuple, default: str) -> str:
    """读一个「开关文件」，内容必须落在 allowed 里，否则回退 default。

    服务由 systemd 启动，改 unit 里的 Environment 需要写 systemd unit（本机 guard
    禁止），所以两个数据面引擎都用文件当开关：
        echo qoder2 | sudo tee /opt/qtg/var/qoder_engine && sudo systemctl restart qtg
    """
    try:
        raw = Path(path).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return default
    return raw if raw in allowed else default


def _wb_engine_default() -> str:
    """workbuddy 开关：QTG_WB_ENGINE > /opt/qtg/var/wb_engine > on。

    与 qoder/trae 同一套「开关文件」机制 —— 之前这里只写了硬编码默认值 "on"，
    导致文档里的回滚方式（echo off > var/wb_engine）根本不生效。
    """
    return _file_switch("/opt/qtg/var/wb_engine", ("off", "on"), "on")


def _engine_default() -> str:
    """qoder 数据面引擎默认值：QTG_QODER_ENGINE > /opt/qtg/var/qoder_engine > bff。"""
    return _file_switch("/opt/qtg/var/qoder_engine", ("bff", "qoder2"), "bff")


def _trae_engine_default() -> str:
    """trae 数据面引擎默认值：QTG_TRAE_ENGINE > /opt/qtg/var/trae_engine > bff。"""
    return _file_switch("/opt/qtg/var/trae_engine", ("bff", "aiwork"), "bff")


@dataclass(frozen=True)
class Settings:
    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8795
    docs_enabled: bool = False
    log_level: str = "INFO"

    # ---- 上游控制面 ----
    # qoder 数据面有两个实现，由 qoder_engine 选择（见 runtime.py）：
    #   bff    = 自研 Go 控制面（源码已丢失，不可升级，做回滚用）
    #   qoder2 = shuishuipingan/qoder2api-hub（MIT，可升级，默认影子端口 8799）
    qoder_engine: str = "bff"
    qoder_base: str = "http://127.0.0.1:8791"
    qoder_password_file: str = "/opt/qoder-hub/secrets/panel_password"
    qoder_data_dir: str = "/opt/qoder-hub"
    qoder2_base: str = "http://127.0.0.1:8799"
    qoder2_panel_password_file: str = "/opt/qtg/secrets/qoder2_panel_password"
    # trae 侧同样两个实现（见 runtime.py）：
    #   bff   = 旧 Go 薄封装（源码丢失，仅回滚）
    #   aiwork= 直连 aiwork-server(:8080)，Rust 实现、镜像可升级
    trae_engine: str = ""
    trae_base: str = "http://127.0.0.1:8792"
    trae_token_file: str = "/opt/trae-work/secrets/admin_token"
    trae_data_dir: str = "/opt/trae-work"
    aiwork_base: str = "http://127.0.0.1:8080"
    aiwork_admin_token_file: str = "/opt/qtg/secrets/aiwork_admin_token"
    aiwork_api_key_file: str = "/opt/qtg/secrets/aiwork_api_key"
    aiwork_db: str = "/opt/trae-work/data/data/aiwork.sqlite"
    aiwork_log_dir: str = "/opt/trae-work/data/logs"
    # workbuddy 侧（三合一第三平台）：两端都在跑，qtg **只读**，不改写
    #   :7863 = workbuddy2api（数据面反代，docker）
    #   :7864 = workbuddy-manager（面板，FastAPI + Next.js）
    wb_engine: str = "on"
    wb_api_base: str = "http://127.0.0.1:7863"
    wb_panel_base: str = "http://127.0.0.1:7864"
    # K01-S2：QTG 作为内部服务调用 Manager 的凭据（scope=keys 的 API Token）。
    # 缺失时创建密钥会明确报错，不会回退到"QTG 本地建 key"。
    # **只用于 /api/keys 的写与管理读**；不要拿它做 K26 的只读读取（最小权限）。
    manager_service_token_file: str = "/opt/qtg/secrets/manager_service_token"
    # K26（R1-A）：只读读取用的独立凭据（scope=read 的 API Token）。
    # 与 manager_service_token_file **分开配置、分开调用**：读端点只送这把，
    # 写端点只送 keys 令牌；两者缺失都抛 ConfigMissing，且互不回退。
    manager_read_token_file: str = "/opt/qtg/secrets/manager_read_token"
    manager_api_timeout_sec: float = 10.0
    wb_config_file: str = "/opt/workbuddy2api/config.json"
    wb_auths_dir: str = "/opt/workbuddy2api/auths"
    wb_state_file: str = "/opt/workbuddy2api/data/state.json"
    wb_db: str = "/opt/workbuddy-manager/data/manager.db"
    # K26：读取 Manager 数据的方式。
    #   "sqlite"（默认，现状）= 只读打开 manager.db，要求同机共享文件系统
    #   "http"             = 走 Manager 的只读 HTTP API（鉴权、版本化、分页）
    # 默认保持 sqlite 是为了「不改变现网行为」；迁移完成后再改默认值。
    # 无论选哪种，失败都不得回退到另一种（否则"共享文件系统"这个前提会悄悄复活）。
    wb_read_mode: str = "sqlite"

    # ---- 上游访问策略 ----
    upstream_timeout_sec: float = 20.0
    upstream_retries: int = 2
    upstream_backoff_sec: float = 0.25
    breaker_fail_threshold: int = 5
    breaker_cooldown_sec: float = 30.0

    # ---- 缓存 ----
    cache_overview_sec: float = 30.0
    cache_accounts_sec: float = 15.0
    cache_usage_sec: float = 60.0
    cache_models_sec: float = 300.0
    cache_settings_sec: float = 30.0

    # ---- 鉴权 ----
    admin_token_file: str = "/opt/qtg/secrets/admin_token"
    # 兼容旧面板令牌，避免升级后「不知道密码进不去」；逗号分隔的绝对路径列表
    extra_password_files: str = "/opt/preview/token,/opt/qoder-hub/secrets/panel_password"
    session_secret_file: str = "/opt/qtg/var/session_secret"
    session_ttl_sec: int = 8 * 3600
    read_requires_auth: bool = False
    cookie_secure: str = "auto"

    # ---- 身份与授权（K04/K05/K07）----
    # 边缘服务凭据：由 Nginx/边缘组件放在**专用头** `X-QTG-Internal-Auth` 里，
    # 与浏览器可携带的 Authorization 完全分离（见 identity.py 的设计说明）。
    # 文件不存在 = 内部服务身份关闭（默认安全：只认浏览器会话与运维令牌）。
    internal_service_token_file: str = "/opt/qtg/secrets/internal_service_token"
    # Dedicated Manager assertion key; never reuse the machine service token.
    # Empty disables signed assertions, without disabling standalone QTG sessions.
    identity_signing_key_file: str = ""
    # 可信边缘的对端地址（逗号分隔，支持 IP 与 CIDR）。只有这些对端送来的
    # 内部凭据 / 身份断言才被采信；其余一律按「外部伪造」处理。
    trusted_edge_peers: str = "127.0.0.1,::1"
    # 额外允许的浏览器来源（逗号分隔的 scheme://host[:port]），用于反代域名与
    # 面板域名不一致的部署；留空 = 只允许 Host 头自身。
    allowed_origins: str = ""
    # 内部身份断言的受众（aud）与本进程期望值比对；不一致直接拒绝。
    identity_audience: str = "qtg"
    # 身份断言的最大有效期：超过它的 exp 视为「无限期委派」，拒绝。
    identity_max_ttl_sec: int = 12 * 3600
    # 严格 Origin/Referer 校验（scheme/host/port 全等）。关闭只允许在排障时临时用，
    # 默认值必须为 True：这就是 K07 的修复本体。
    csrf_strict_origin: bool = True
    # 角色×动作×平台授权矩阵是否生效（K04）。关闭 = 退回「只分已登录/未登录」。
    authz_enforce: bool = True
    # 高危动作是否强制真人会话（Cookie）。打开后 API Token 不能再调用高危写操作，
    # 与 Manager 的 require_session_admin 对齐。默认 True（安全默认值）；
    # 需要临时放开运维脚本时用 QTG_HUMAN_SESSION_REQUIRED=0，并在审计里留痕。
    human_session_required: bool = True

    # ---- 运行时数据 ----
    data_dir: str = "/opt/qtg/var"
    checkin_scheduler_enabled: bool = True
    audit_file: str = ""
    static_dir: str = "/opt/qtg/web"

    # ---- 数据面（/v1 聚合网关）----
    proxy_enabled: bool = True
    qoder_api_key_file: str = "/opt/qtg/secrets/qoder_upstream_key"
    trae_api_key_file: str = "/opt/qtg/secrets/trae_upstream_key"
    proxy_timeout_sec: float = 600.0
    proxy_max_body_mb: int = 32
    proxy_model_cache_sec: float = 300.0

    # ---- 运维 ----
    backup_script_trae: str = "/opt/backup/trae-backup.sh"
    backup_script_qoder: str = "/opt/backup/qoder-offsite.sh"
    limits_file: str = "/opt/trae-audit/limits.json"
    notify_config_file: str = "/opt/notify/config.json"
    backup_glob_trae: str = "/opt/backups/trae/trae-backup-*"
    backup_glob_qoder: str = "/opt/qoder-hub/backups/qoder-backup-*"

    # ---- 派生 ----
    @property
    def audit_path(self) -> Path:
        return Path(self.audit_file or os.path.join(self.data_dir, "audit.jsonl"))

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "qtg.db"

    @property
    def state_path(self) -> Path:
        return Path(self.data_dir) / "state.json"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.static_dir):
            Path(d).mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    from .configcontract import validate_environment
    validate_environment()
    return Settings(
        host=_env_str("host", "127.0.0.1"),
        port=_env_int("port", 8795, 1, 65535),
        docs_enabled=_env_bool("docs_enabled", False),
        log_level=_env_str("log_level", "INFO"),
        qoder_engine=(_env_str("qoder_engine", _engine_default()) or "bff").strip().lower(),
        qoder_base=_env_str("qoder_base", "http://127.0.0.1:8791"),
        qoder_password_file=_abs(
            "qoder_password_file", "/opt/qoder-hub/secrets/panel_password"
        ),
        qoder_data_dir=_abs("qoder_data_dir", "/opt/qoder-hub"),
        qoder2_base=_env_str("qoder2_base", "http://127.0.0.1:8799"),
        qoder2_panel_password_file=_abs(
            "qoder2_panel_password_file", "/opt/qtg/secrets/qoder2_panel_password"
        ),
        trae_engine=(_env_str("trae_engine", _trae_engine_default()) or "bff").strip().lower(),
        aiwork_base=_env_str("aiwork_base", "http://127.0.0.1:8080"),
        aiwork_admin_token_file=_abs(
            "aiwork_admin_token_file", "/opt/qtg/secrets/aiwork_admin_token"
        ),
        aiwork_api_key_file=_abs("aiwork_api_key_file", "/opt/qtg/secrets/aiwork_api_key"),
        aiwork_db=_abs("aiwork_db", "/opt/trae-work/data/data/aiwork.sqlite"),
        aiwork_log_dir=_abs("aiwork_log_dir", "/opt/trae-work/data/logs"),
        wb_engine=(_env_str("wb_engine", _wb_engine_default()) or "on").strip().lower(),
        wb_api_base=_env_str("wb_api_base", "http://127.0.0.1:7863"),
        wb_panel_base=_env_str("wb_panel_base", "http://127.0.0.1:7864"),
        manager_service_token_file=_abs(
            "manager_service_token_file", "/opt/qtg/secrets/manager_service_token"
        ),
        manager_read_token_file=_abs(
            "manager_read_token_file", "/opt/qtg/secrets/manager_read_token"
        ),
        manager_api_timeout_sec=_env_float("manager_api_timeout_sec", 10.0, 0.5, 120.0),
        wb_config_file=_abs("wb_config_file", "/opt/workbuddy2api/config.json"),
        wb_auths_dir=_abs("wb_auths_dir", "/opt/workbuddy2api/auths"),
        wb_state_file=_abs("wb_state_file", "/opt/workbuddy2api/data/state.json"),
        wb_db=_abs("wb_db", "/opt/workbuddy-manager/data/manager.db"),
        wb_read_mode=_env_str("wb_read_mode", "sqlite"),
        trae_base=_env_str("trae_base", "http://127.0.0.1:8792"),
        trae_token_file=_abs("trae_token_file", "/opt/trae-work/secrets/admin_token"),
        trae_data_dir=_abs("trae_data_dir", "/opt/trae-work"),
        upstream_timeout_sec=_env_float("upstream_timeout_sec", 20.0, 1.0, 300.0),
        upstream_retries=_env_int("upstream_retries", 2, 0, 5),
        upstream_backoff_sec=_env_float("upstream_backoff_sec", 0.25, 0.0, 5.0),
        breaker_fail_threshold=_env_int("breaker_fail_threshold", 5, 1, 100),
        breaker_cooldown_sec=_env_float("breaker_cooldown_sec", 30.0, 1.0, 600.0),
        cache_overview_sec=_env_float("cache_overview_sec", 30.0, 0.0, 600.0),
        cache_accounts_sec=_env_float("cache_accounts_sec", 15.0, 0.0, 600.0),
        cache_usage_sec=_env_float("cache_usage_sec", 60.0, 0.0, 3600.0),
        cache_models_sec=_env_float("cache_models_sec", 300.0, 0.0, 3600.0),
        cache_settings_sec=_env_float("cache_settings_sec", 30.0, 0.0, 600.0),
        admin_token_file=_abs("admin_token_file", "/opt/qtg/secrets/admin_token"),
        proxy_enabled=_env_bool("proxy_enabled", True),
        qoder_api_key_file=_abs("qoder_api_key_file", "/opt/qtg/secrets/qoder_upstream_key"),
        trae_api_key_file=_abs("trae_api_key_file", "/opt/qtg/secrets/trae_upstream_key"),
        proxy_timeout_sec=_env_float("proxy_timeout_sec", 600.0, 10.0, 3600.0),
        proxy_max_body_mb=_env_int("proxy_max_body_mb", 32, 1, 512),
        proxy_model_cache_sec=_env_float("proxy_model_cache_sec", 300.0, 0.0, 3600.0),
        extra_password_files=_env_str(
            "extra_password_files", "/opt/preview/token,/opt/qoder-hub/secrets/panel_password"
        ),
        session_secret_file=_abs("session_secret_file", "/opt/qtg/var/session_secret"),
        session_ttl_sec=_env_int("session_ttl_sec", 8 * 3600, 60, 30 * 86400),
        read_requires_auth=_env_bool("read_requires_auth", False),
        cookie_secure=_env_str("cookie_secure", "auto"),
        # ---- 身份与授权（K04/K05/K07）----
        internal_service_token_file=_abs(
            "internal_service_token_file", "/opt/qtg/secrets/internal_service_token"
        ),
        identity_signing_key_file=(_abs("identity_signing_key_file", "")
                                   if _env_str("identity_signing_key_file", "") else ""),
        trusted_edge_peers=_env_str("trusted_edge_peers", "127.0.0.1,::1"),
        allowed_origins=_env_str("allowed_origins", ""),
        identity_audience=_env_str("identity_audience", "qtg"),
        identity_max_ttl_sec=_env_int("identity_max_ttl_sec", 12 * 3600, 60, 7 * 86400),
        csrf_strict_origin=_env_bool("csrf_strict_origin", True),
        authz_enforce=_env_bool("authz_enforce", True),
        human_session_required=_env_bool("human_session_required", True),
        data_dir=_abs("data_dir", "/opt/qtg/var"),
        checkin_scheduler_enabled=_env_bool("checkin_scheduler_enabled", True),
        static_dir=_abs("static_dir", "/opt/qtg/web"),
        audit_file=_env_str("audit_file", ""),
        backup_script_trae=_abs("backup_script_trae", "/opt/backup/trae-backup.sh"),
        backup_script_qoder=_abs("backup_script_qoder", "/opt/backup/qoder-offsite.sh"),
        backup_glob_trae=_env_str("backup_glob_trae", "/opt/backups/trae/trae-backup-*"),
        backup_glob_qoder=_env_str("backup_glob_qoder", "/opt/qoder-hub/backups/qoder-backup-*"),
        limits_file=_abs("limits_file", "/opt/trae-audit/limits.json"),
        notify_config_file=_abs("notify_config_file", "/opt/notify/config.json"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
