"""Human-session-only steward and alert settings, independent of platform tabs."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import db, security, steward_ai, steward_config, steward_runtime, steward_store
from ..iputil import client_ip
from ..services.asyncwork import joined_thread

router = APIRouter(prefix='/api/steward', tags=['steward'])
_chat_active = False
_last_call = OrderedDict()


@router.get('/status')
def status(user: dict = Depends(security.require_session_reader)):
    return {**steward_runtime.snapshot(), 'capabilities': steward_config.capabilities(),
            'monitor': db.get_setting(steward_runtime.HEARTBEAT_KEY), 'read_only_tools': True,
            'can_chat': user['role'] == 'admin', 'write_tools_available': False}


@router.get('/alerts')
def alerts(user: dict = Depends(security.require_session_reader)):
    return {'incidents': steward_store.incidents(),
            'deliveries': steward_store.deliveries(include_recipients=user['role'] == 'admin'),
            'analyses': steward_store.analyses(), 'queue': steward_store.queue_state()}


@router.get('/settings')
def settings(user: dict = Depends(security.require_session_reader)):
    selected = steward_config.policy().model_dump()
    if user['role'] != 'admin':
        selected['recipients'] = []
    return {'policy': selected, 'can_edit': user['role'] == 'admin',
            'recipient_count': len(steward_config.policy().recipients),
            'capabilities': steward_config.capabilities()}


@router.put('/settings')
def save_settings(body: steward_config.Policy, request: Request,
                  user: dict = Depends(security.require_session_admin)):
    try:
        with security.authorization_guard(user):
            selected = steward_config.save(body.model_dump())
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    security.audit(user, 'steward.settings', '',
                   f'revision={selected.revision};recipients={len(selected.recipients)};enabled={selected.enabled}',
                   client_ip(request))
    return {'policy': selected.model_dump(), 'ok': True}


@router.post('/test-mail')
def test_mail(request: Request, user: dict = Depends(security.require_session_admin)):
    with security.authorization_guard(user):
        selected = steward_config.policy()
        if not selected.recipients or not steward_config.capabilities()['smtp_configured']:
            raise HTTPException(409, '请先添加通知邮箱并由服务器配置邮件通道')
        with db.transaction():
            last = db.get_setting('steward.test-last-at.v1', 0)
            if type(last) is not int or time.time() - last < 60:
                raise HTTPException(429, '测试邮件每分钟最多发送一轮')
            db.set_setting('steward.test-last-at.v1', int(time.time()))
            ids = [steward_store.queue_test(recipient, actor=user['username']) for recipient in selected.recipients]
    security.audit(user, 'steward.test-mail', '', f'queued={len(ids)}', client_ip(request))
    return {'queued': ids, 'accepted_by_smtp': False,
            'note': '仅已进入待发送队列，请以投递状态和实际收件箱验收为准'}


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=2000)


class Chat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=2000)
    history: list[Message] = Field(default_factory=list, max_length=8)


@router.post('/chat')
async def chat(body: Chat, request: Request, user: dict = Depends(security.require_session_admin)):
    global _chat_active
    # Bounded, non-queued inference. Each model call is additionally metered and
    # limited by the steward's ordinary gateway key; no admin billing bypass.
    identity = security.authorization_owner(user)
    now = time.monotonic()
    if _chat_active or now - _last_call.get(user['username'], -100) < 10:
        raise HTTPException(429, '管家正在研判，请稍后再试')
    _chat_active = True
    try:
        evidence = await joined_thread(steward_runtime.snapshot)
        if not evidence['available'] or evidence['stale']:
            raise HTTPException(503, '管家状态尚未更新，暂不依据过期数据进行研判')
        capabilities = await joined_thread(steward_config.capabilities)
        if not capabilities['model_configured']:
            raise HTTPException(409, '尚未配置管家专用 DeepSeek 调用密钥')
        # A demotion/reset while the snapshot or secret capability was being
        # read must not authorize a new billable model request.
        security.authorization_owner(identity)
        _last_call[user['username']] = now
        while len(_last_call) > 128:
            _last_call.popitem(last=False)
        security.audit(user, 'steward.chat', '', 'read-only;question-not-retained', client_ip(request))
        def permitted():
            security.authorization_owner(identity)
            if steward_runtime.snapshot()['stale']:
                raise HTTPException(503, '管家状态尚未更新，暂不依据过期数据进行研判')
        result = await steward_ai.answer(body.question, evidence['snapshot'],
                                          history=[row.model_dump() for row in body.history],
                                          before_dispatch=permitted)
        # Do not return collected data to a session revoked while inference ran.
        security.authorization_owner(identity)
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, 'DeepSeek 研判暂不可用；规则预警不受影响，请勿将未完成分析当作诊断结论') from None
    finally:
        _chat_active = False
