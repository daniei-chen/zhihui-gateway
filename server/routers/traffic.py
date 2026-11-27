"""调用轨迹（抓包）的读取与开关接口。

全部要求面板读者权限：正文是用户内容，能看的人应当与「能看日志」的人一致。
写开关要求管理员 —— 打开它意味着开始把正文落盘，这个决定不该由只读者做出。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from .. import db, security, traffic

router = APIRouter(prefix='/api/traffic', tags=['traffic'])

SETTING_KEY = 'traffic.tap'


@router.get('')
def list_records(limit: int = Query(50, ge=1, le=500),
                 status: str | None = Query(None, max_length=16),
                 model: str | None = Query(None, max_length=64),
                 key: str | None = Query(None, max_length=64),
                 stream: bool | None = Query(None),
                 truncated: bool | None = Query(None),
                 since: str | None = Query(None, max_length=8),
                 user: dict = Depends(security.require_panel_reader)) -> dict:
    """最近若干条轨迹的摘要，可按状态/模型/密钥/时间筛。

    只给元数据（时间、模型、状态、大小）：正文要逐条点开，避免一次请求把几十份
    prompt 全读进内存。筛选只读索引，所以「只看失败的」这类查询很便宜。
    """
    return {
        'enabled': traffic.enabled(),
        'records': traffic.list_records(
            limit, status=status, model=model, key=key,
            stream=stream, truncated=truncated, since=since),
    }


@router.get('/status')
def status(user: dict = Depends(security.require_panel_reader)) -> dict:
    """开关、计数、保留策略。排查「为什么没有记录」时先看这里。"""
    payload = traffic.stats()
    payload.update(traffic.setting_status())
    return payload


@router.get('/{request_id}')
def read_one(request_id: str, user: dict = Depends(security.require_panel_reader)) -> dict:
    record = traffic.read_record(request_id)
    if record is None:
        # 过期清理后旧链接会失效，这是预期行为，所以用 404 + 明确说明。
        raise HTTPException(status_code=404,
                            detail='轨迹不存在或已过期（默认保留 24 小时）')
    return record


@router.get('/{request_id}/export', response_class=HTMLResponse)
def export_one(request_id: str, user: dict = Depends(security.require_panel_reader)) -> HTMLResponse:
    """导出单文件 HTML；已定义字段脱敏后仍应按用户内容保管。"""
    record = traffic.read_record(request_id)
    if record is None:
        raise HTTPException(status_code=404,
                            detail='轨迹不存在或已过期（默认保留 24 小时）')
    return HTMLResponse(traffic.render_html(record),
                        headers={'Content-Disposition': f'inline; filename="traffic-{request_id}.html"'})


@router.post('/setting')
def set_enabled(body: dict, user: dict = Depends(security.require_admin)) -> dict:
    """开关抓包。

    两件事说清楚，避免「开了却没记录」或「以为关了还在记」：

    · 环境变量是**总开关**。总开关关闭时这里写成什么都不生效 —— 返回 `blocked_by`
      让调用方知道原因，而不是静默成功。
    · 当前实例写入后立即刷新；其他实例按 5 秒缓存周期刷新。effective 是当前实例
      的状态，不假称已收到所有实例的生效确认。关闭也不删除在途或已存内容。
    """
    if 'enabled' not in body:
        raise HTTPException(status_code=400, detail='缺少 enabled 字段')
    if type(body['enabled']) is not bool:
        raise HTTPException(status_code=400, detail='enabled 必须为布尔值')
    enabled = body['enabled']
    db.execute('INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)',
               (SETTING_KEY, 'true' if enabled else 'false'))
    traffic.invalidate_flag()
    payload = traffic.setting_status()
    if payload.get('blocked_by') == 'WB_TRAFFIC_TAP=0':
        payload['detail'] = '抓包总开关由部署环境控制，当前关闭；改环境变量并重启后才会生效'
    elif payload['blocked_reason'] == 'settings_unavailable':
        payload['detail'] = '开关已保存，但读取状态失败；当前实例暂不记录新请求'
    elif not payload['effective']:
        payload['detail'] = ('当前实例已停止记录新请求；其他实例按 5 秒缓存周期刷新。'
                             '在途请求可能继续收尾，已落盘记录按保留期清理')
    else:
        payload['detail'] = '当前实例已开启记录新请求；其他实例按 5 秒缓存周期刷新'
    return payload
