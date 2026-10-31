"""Public policy in SQLite; transport secrets remain operator-owned 0600 files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db

SETTING = 'steward.policy.v1'


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: int = Field(default=0, ge=0)
    enabled: bool = False
    recipients: list[str] = Field(default_factory=list, max_length=10)
    send_recovery: bool = True
    ai_analysis: bool = False
    model: str = 'cn:deepseek-v4.1-flash'
    interval_seconds: int = Field(default=60, ge=30, le=300)
    consecutive_failures: int = Field(default=2, ge=1, le=10)
    disk_warning_percent: int = Field(default=80, ge=60, le=95)
    disk_critical_percent: int = Field(default=90, ge=70, le=99)
    memory_warning_percent: int = Field(default=90, ge=60, le=99)
    failure_warning_percent: int = Field(default=5, ge=1, le=50)
    min_requests: int = Field(default=20, ge=5, le=1000)
    latency_warning_ms: int = Field(default=30000, ge=1000, le=300000)
    budget_unknown_warning: int = Field(default=10, ge=1, le=10000)

    @field_validator('recipients')
    @classmethod
    def emails(cls, value):
        out = []
        for email in value:
            if (len(email) > 254 or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}', email)
                    or '..' in email or email.startswith('.') or '.@' in email):
                raise ValueError('请输入有效的邮箱地址，不使用显示名或换行')
            email = email.rsplit('@', 1)[0] + '@' + email.rsplit('@', 1)[1].lower()
            if email.casefold() not in {item.casefold() for item in out}:
                out.append(email)
        return out

    @field_validator('model')
    @classmethod
    def deepseek_only(cls, value):
        if not re.fullmatch(r'(?:cn:|global:)?deepseek-[a-z0-9][a-z0-9.\-]{0,75}', value):
            raise ValueError('智汇管家只接受显式的 DeepSeek 模型标识')
        return value


def validate_policy(value):
    policy = Policy.model_validate(value)
    if policy.disk_warning_percent >= policy.disk_critical_percent:
        raise ValueError('磁盘严重阈值必须大于提醒阈值')
    if policy.enabled and not policy.recipients:
        raise ValueError('启用预警前请至少添加一个通知邮箱')
    return policy


def policy():
    value = db.get_setting(SETTING)
    return validate_policy({} if value is None else value)


def save(value):
    selected = validate_policy(value)
    with db.transaction():
        current = policy()
        if selected.revision != current.revision:
            raise ValueError('预警设置已被其他会话更新，请刷新后重试')
        selected.revision += 1
        db.set_setting(SETTING, selected.model_dump())
    return selected


def secret_file(path, *, limit=16384):
    """Read without following a final symlink, permitting only the owner and root."""
    if not path:
        raise ValueError('not_configured')
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('unsafe_secret_file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or (os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())))):
            raise ValueError('unsafe_secret_file')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as source:
            return source.read(limit + 1)
    finally:
        os.close(fd)


def smtp_settings():
    from . import config
    value = json.loads(secret_file(config.STEWARD_SMTP_FILE))
    return validate_smtp(value)


def validate_smtp(value):
    from .services.smtp_transport import validate_endpoint
    if not isinstance(value, dict) or set(value) - {'host', 'port', 'username', 'password'}:
        raise ValueError('invalid_smtp_settings')
    # Preserve the existing 163 sender and allow QQ without arbitrary UI targets.
    validate_endpoint(value.get('host'), value.get('port'))
    if not isinstance(value.get('username'), str):
        raise ValueError('invalid_smtp_identity')
    username = Policy.emails([value['username']])[0]
    if (not isinstance(value.get('password'), str) or not value['password'].strip()
            or len(value['password']) > 4096):
        raise ValueError('missing_smtp_password')
    return {**value, 'username': username}


def model_key():
    from . import config
    token = secret_file(config.STEWARD_KEY_FILE).strip()
    if not re.fullmatch(r'(?:sk-[0-9a-f]{32}|wbk_[A-Za-z0-9_-]{32,128})', token):
        raise ValueError('invalid_steward_key')
    return token


def capabilities():
    output = {}
    for name, read in (('smtp_configured', smtp_settings), ('model_configured', model_key)):
        try:
            read()
            output[name] = True
        except (OSError, ValueError, TypeError):
            output[name] = False
    return output
