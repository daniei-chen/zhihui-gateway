"""系统维护：一键更新与进度查询。

安全说明：仅管理员可用；更新目标限定为固定枚举，路径全部取自服务端环境，
不接受客户端传入的命令或路径，避免变成任意命令执行入口。离线升级包同理：
客户端只上传**文件内容**，落盘位置与文件名都由服务端固定。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from .. import config, drain, security
from ..iputil import client_ip
from ..services import changelog, updater, wb2api

router = APIRouter(prefix='/api/system', tags=['system'])

#: U16：systemd 单元文件所在目录。测试可替换（不碰真实 /etc）。
SYSTEMD_UNIT_DIR = Path('/etc/systemd/system')
#: 服务名兜底（`WB_SERVICE_NAME` 已登记在 schema，未设置时用它）。
DEFAULT_SERVICE = 'workbuddy-web.service'


def _service_name() -> str:
    raw = (os.environ.get('WB_SERVICE_NAME') or '').strip() or DEFAULT_SERVICE
    return raw if raw.endswith('.service') else raw + '.service'


def _parse_environment(text: str) -> dict:
    """解析 unit/drop-in 的 `Environment=`（同一文件内后写的覆盖前者）。"""
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith('Environment='):
            continue
        for item in line[len('Environment='):].strip().split():
            if '=' not in item:
                continue
            key, value = item.split('=', 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def read_unit_environment(service: str | None = None,
                          unit_dir: Path | None = None) -> dict:
    """只读采集 unit + drop-in 的 `Environment=`（systemd 合并语义）。

    **不修改任何 systemd 文件**，只读；读不到（容器内没有 systemd、权限不足）时
    如实返回 `available=false` + 原因，而不是假装「没有覆盖」。
    """
    directory = Path(unit_dir) if unit_dir else SYSTEMD_UNIT_DIR
    name = service or _service_name()
    base = directory / name
    files: list[dict] = []
    errors: list[str] = []
    candidates = [base] if base.is_file() else []
    dropin = directory / (name + '.d')
    if dropin.is_dir():
        try:
            candidates += sorted(dropin.glob('*.conf'))
        except OSError as exc:                       # pragma: no cover - 目录竞态
            errors.append('%s: %s' % (dropin, exc))
    for path in candidates:
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            errors.append('%s: %s' % (path, exc))
            continue
        files.append({'path': str(path), 'environment': _parse_environment(text)})
    declared: dict = {}
    owners: dict = {}
    for entry in files:
        declared.update(entry['environment'])
        for key, value in entry['environment'].items():
            owners.setdefault(key, []).append({'file': Path(entry['path']).name,
                                               'value': value})
    duplicates = {key: value for key, value in owners.items() if len(value) > 1}
    return {
        'available': bool(files),
        'service': name,
        'unit_dir': str(directory),
        'files': [{'path': e['path'], 'keys': sorted(e['environment'])} for e in files],
        'declared': declared,
        'duplicates': duplicates,
        'errors': errors,
        'reason': None if files else 'unit 文件不存在或不可读（容器内通常没有 systemd）',
    }


def _release_id() -> str:
    try:
        return (config.ROOT / '.version').read_text(encoding='utf-8').strip() or 'unknown'
    except OSError:
        return 'unknown'


@router.get('/config')
def configuration_drift(request: Request, refresh: bool = False,
                        user: dict = Depends(security.require_admin)) -> dict:
    """U16：**配置四源对照 + 漂移标记**（只读，仅管理员）。

    四个来源（缺一个就看不出漂移在哪）：

    1. `schema_default`：`server/config-schema.json` 声明的默认值 —— 唯一配置模型；
    2. `source_default`：源码里 `_env*('NAME', <默认>)` 的第二参数（AST 扫描，
       跨模块），即"未设置时真正生效的值"；
    3. `declared`：systemd unit + drop-in 的 `Environment=`（后加载覆盖前者）；
    4. `process`：本进程实际拿到的环境。

    3 与 4 必须分开看：改了 drop-in 却没重启，`declared != process` —— 这正是最容易
    被误读成"配置已生效"的漂移。`risk` 为 `high` 的项是真缺陷（未登记变量、改了
    没重启、敏感值明文进 unit 等）；`override`（覆盖 schema 默认）是正常运维动作，
    只记录不告警。

    敏感字段（schema `sensitive: true`）只返回"是否设置 + 指纹 + 长度"；未登记变量
    的值一律不返回（模型不知道它是否敏感）。`?refresh=true` 重新扫描源码默认值。
    """
    from ..configcontract import (compare_sources, cross_checks, load_schema,
                                  parse_source_defaults)

    schema = load_schema()
    specs = schema['settings']
    source_defaults = parse_source_defaults(config.ROOT / 'server', refresh=bool(refresh))
    unit = read_unit_environment()
    comparison = compare_sources(specs, source_defaults=source_defaults,
                                 process_env=dict(os.environ),
                                 declared_env=unit['declared'],
                                 unit_duplicates=unit['duplicates'],
                                 allowed=dict(getattr(config, 'SCHEMA_DRIFT_ALLOWED', {})))
    checks = cross_checks(specs, dict(os.environ))
    summary = comparison['summary']
    summary['cross_checks_ok'] = all(item['ok'] for item in checks)
    summary['unit_files_read'] = len(unit['files'])
    summary['unit_readable'] = unit['available']
    snapshot = getattr(request.app.state, 'configuration_snapshot', None)
    return {
        'ok': True,
        'release': _release_id(),
        'generated_at': int(time.time()),
        'sources': {
            'schema': {'authority': '配置模型与默认值的唯一权威',
                       'path': 'server/config-schema.json',
                       'schema': schema['schema'],
                       'settings': len(specs)},
            'source_default': {'note': '源码默认值必须等于 schema 默认值（例外见 allowed）',
                               'scanned': 'server/**/*.py（不含 tests）',
                               'modules_with_defaults': len(source_defaults)},
            'declared': {'note': 'unit + drop-in 的 Environment=，只应写与 schema 默认不同的项',
                         'service': unit['service'], 'files': unit['files'],
                         'available': unit['available'], 'reason': unit['reason'],
                         'errors': unit['errors']},
            'process': {'note': '本进程启动时拿到的环境（/proc/self/environ）',
                        'wb_keys': sum(1 for k in os.environ if k.startswith(('WB_', 'WB2API_')))},
        },
        'summary': summary,
        'settings': comparison['rows'],
        'duplicate_keys': comparison['duplicate_keys'],
        'cross_checks': checks,
        'policy': {
            'order': ['config-schema.json', 'server/config.py 源码默认值',
                      'unit/drop-in 覆盖（仅差异）', '进程实际值'],
            'allowed_differences': dict(getattr(config, 'SCHEMA_DRIFT_ALLOWED', {})),
            'rules': [
                'schema 未登记的 WB_* 会让服务启动失败（validate_environment）',
                '源码默认值必须等于 schema 默认值（表达式型按文本比较）',
                '同一个键只允许出现在一个 unit 文件里',
                '敏感值不得以明文写进 unit/drop-in',
            ],
        },
        'startup_snapshot_present': snapshot is not None,
        'related': {'configuration': '/api/system/configuration',
                    'note': '那边是启动时 validate_environment() 的快照，这里是四源对照'},
    }


@router.get('/configuration')
def configuration_snapshot(request: Request,
                           user: dict = Depends(security.require_admin)) -> dict:
    from ..configcontract import validate_environment
    loaded = getattr(request.app.state, 'configuration_snapshot', None)
    return {'ok': True, 'configuration': loaded or validate_environment(),
            'active': {'upstream_timeout_s':config.UPSTREAM_TIMEOUT,
                       'drain_timeout_s':config.DRAIN_TIMEOUT_SEC,
                       'http_pool_connections':config.HTTP_POOL_CONNECTIONS,
                       'http_pool_keepalive':config.HTTP_POOL_KEEPALIVE,
                       'managed_launch':config.LAUNCH_ARGUMENTS},
            'note':'Environment snapshot is captured at startup; listener CLI and panel/upstream overrides are separate sources.'}


class DrainIn(BaseModel):
    action: Literal['begin', 'resume'] = 'begin'


@router.get('/drain')
def drain_status(user: dict = Depends(security.require_admin)) -> dict:
    return drain.snapshot()


@router.post('/drain')
def change_drain(body: DrainIn,
                 user: dict = Depends(security.require_session_admin)) -> dict:
    """Pre-stop control: wait for zero in-flight before terminating this process."""
    if body.action == 'begin':
        drain.begin()
    elif not drain.resume():
        raise HTTPException(status_code=409, detail='Process shutdown has already started')
    security.audit(user, 'gateway_drain', body.action)
    return drain.snapshot()


class UpdateIn(BaseModel):
    target: str = Field(pattern='^(manager|upstream|both)$')
    # 包从哪来：GitHub 最新发布（默认），还是面板里已上传的离线升级包
    source: str = Field('release', pattern='^(release|uploaded)$')


@router.get('/update-status')
def update_status(user: dict = Depends(security.current_user)) -> dict:
    """更新进度与当前版本（含运行中的日志）。"""
    status = updater.read_status()
    is_admin = str((user or {}).get('role') or '') == 'admin'
    # 日志里可能有代理地址、绝对路径与命令输出：只给管理员。只读账号仍能看到
    # 版本与「是否在更新」，但拿不到实现细节（旧版对 viewer 是全量返回的）。
    status['log_tail'] = updater.tail_log(80) if is_admin else ''
    if not is_admin:
        status['logs'] = []
    return status


@router.post('/update')
def start_update(body: UpdateIn, user: dict = Depends(security.require_session_admin)) -> dict:
    """启动一键更新。

    更新会在后台执行并可能重启本服务，因此立即返回，由前端轮询
    `/api/system/update-status` 查看进度。
    """
    ok, message = updater.start_update(body.target, source=body.source)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    security.audit(user,'start_update',body.target,message)
    return {'ok': True, 'message': message}


# ── 离线升级包（issue #153）──────────────────────────────────
# 内网 / 拿不到 GitHub 的机器：在别处下载发布包，拿到这里上传，再走同一套更新流程。
# 这些端点只碰服务端固定路径（DATA_DIR/update-package.tar.gz），不吃客户端给的路径。

_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_CHUNK = 1024 * 1024


@router.get('/update-package')
def get_update_package(user: dict = Depends(security.current_user)) -> dict:
    """已上传升级包的元信息（文件名 / 大小 / 摘要 / 包内版本 / 上传时间）。"""
    return updater.package_meta()


@router.post('/update-package')
async def upload_update_package(
    request: Request,
    file: UploadFile = File(...),
    expected_sha256: str = Form(''),
    user: dict = Depends(security.require_session_admin),
) -> dict:
    """上传离线升级包（发布页下载的那个 .tar.gz）。

    先写 `.part`、校验通过后再原子替换到最终位置：中途失败不会留下一个「看起来
    能用」的半截包。校验四件事——大小上限、能不能当 tar.gz 解开、包内有没有
    `.version`（不是本项目的包当场拒掉）、发布页 sha256。

    **摘要默认必填**：安装离线包会让包内的代码与依赖在本机执行（见
    updater.allow_unverified_package 的说明），不核对摘要等于把「面板会话」
    直接升级成「主机代码执行」。确实拿不到摘要的离线环境请显式设
    `WB_ALLOW_UNVERIFIED_PACKAGE=1`。
    """
    want = (expected_sha256 or '').strip().lower()
    if want and not _SHA256_RE.match(want):
        raise HTTPException(status_code=400, detail='sha256 要填发布页上那串 64 位十六进制')
    if not want and not updater.allow_unverified_package():
        raise HTTPException(
            status_code=400,
            detail='请填写发布页上的 sha256：安装升级包会在本机执行包内代码，'
                   '没有摘要就无法确认包没被替换。确实拿不到摘要的离线环境，'
                   '可在服务端设 WB_ALLOW_UNVERIFIED_PACKAGE=1 后重试。',
        )

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = updater.PACKAGE_FILE.with_suffix('.part')
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, 'wb') as fh:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > updater.PACKAGE_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f'升级包超过 {updater.PACKAGE_MAX_BYTES // 1024 // 1024} MB 上限',
                    )
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f'写入升级包失败：{exc}') from exc

    actual = digest.hexdigest()
    try:
        if size < 100_000:
            raise HTTPException(status_code=400, detail='文件太小，不像是发布包（下载可能中断了）')
        if want and actual != want:
            raise HTTPException(
                status_code=400,
                detail=f'sha256 与填的不一致：包是 {actual}，你填的是 {want}'
                       '（多半是下载不完整，重新下载一次再传）',
            )
        # 结构不对（不是 gzip / 不是 tar）时它抛 ValueError —— 那是「用户传错了文件」，
        # 该当场 400 说清楚，而不是漏成 500。
        try:
            version = updater._read_package_version(tmp_path)   # noqa: SLF001 —— 同模块服务层
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not version:
            raise HTTPException(
                status_code=400,
                detail='包里没有 .version —— 这不是本项目的发布包（发布页下载的是 '
                       'zhihui-gateway-vX.Y.Z.tar.gz）',
            )
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_path, updater.PACKAGE_FILE)
    try:
        os.chmod(updater.PACKAGE_FILE, 0o600)
    except OSError:
        pass
    meta = updater.record_package(
        name=os.path.basename(file.filename or 'update-package.tar.gz'),
        size=size, sha256=actual, version=version, expected_sha256=want,
        uploaded_by=str(user.get('username') or ''),
    )
    security.audit(user, 'upload_update_package', version,
                   f'{size // 1024 // 1024} MB，sha256 {actual[:12]}…，'
                   f'{"已核对发布页摘要" if want else "未填发布页摘要"}；'
                   f'来源 {client_ip(request)}')
    return meta


@router.delete('/update-package')
def delete_update_package(user: dict = Depends(security.require_session_admin)) -> dict:
    """删掉已上传的升级包（更新完成后界面会提示清理）。"""
    updater.clear_package()
    security.audit(user, 'delete_update_package', '', '清掉离线升级包')
    return {'ok': True}


@router.delete('/update-status')
def clear_update_status(user: dict = Depends(security.require_session_admin)) -> dict:
    """清除上次更新的结果与日志（issue #105：失败记录此前在面板里消除不掉）。"""
    ok, message = updater.clear_status()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    security.audit(user,'clear_update_status','','cleared update status')
    return {'ok': True, 'message': message}


@router.get('/check-update')
def check_update(force: bool = False, user: dict = Depends(security.current_user)) -> dict:
    """检测是否有新版本（管理端与上游）。

    结果缓存 6 小时以免触发 GitHub 限流；`force=true` 可强制刷新，
    但会打 GitHub API（未认证配额有限），因此只允许管理员强制刷新。
    """
    if force and user.get('role') != 'admin':
        force = False
    return updater.check_updates(force=force)


@router.get('/versions')
async def versions(user: dict = Depends(security.current_user)) -> dict:
    """展示当前版本，便于确认更新是否生效。"""
    try:
        status = await wb2api.get_status()
        upstream_ok = bool(status.get('connected'))
        upstream_total = status.get('total')
    except Exception:  # noqa: BLE001
        upstream_ok, upstream_total = False, None

    return {
        'manager': updater.current_version(),
        'product': updater.product.metadata(
            config.ROOT, managed=(config.ROOT / 'deploy/managed-release.json').is_file()),
        'upstream_connected': upstream_ok,
        'upstream_accounts': upstream_total,
        # 部署绝对路径只对管理员展示（2026-10-27 审计 P2-13：viewer 无需知晓）。
        'upstream_dir': (str(updater._upstream_dir())
                         if str(user.get('role') or '') == 'admin' else None),
    }


@router.get('/changelog')
def get_changelog(user: dict = Depends(security.current_user)) -> dict:
    """更新日志（解析仓库根目录的 CHANGELOG.md）。

    离线环境同样可用：文件随发布包一起分发，无需访问 GitHub。
    """
    data = changelog.load_changelog()
    data['current'] = updater.current_version()
    return data


@router.get('/internal-session-verify')
def internal_session_verify(request: Request,
                            user: dict = Depends(security.current_user)):
    """为 nginx auth_request 签发 qtg 身份断言（审计 P1-01）。

    - 仅本机（nginx 127.0.0.1）可调用；nginx 的 /__zh_session 是 internal location；
    - 复用 Manager 会话校验（Cookie / wbt_ token），通过后签发 5 分钟有效的
      HMAC 断言（qtg `X-QTG-Identity` 契约），qtg 据此识别真实用户与角色，
      不再依赖静态 admin Bearer；
    - 无会话时 current_user 抛 401，nginx auth_request 直接拒绝。
    """
    peer = request.client.host if request.client else ''
    if peer not in ('127.0.0.1', '::1'):
        raise HTTPException(status_code=403, detail='仅允许本机调用')
    try:
        secret = open('/opt/workbuddy-manager/engine/secrets/identity_signing.key', encoding='utf-8').read().strip()
    except OSError:
        raise HTTPException(status_code=503, detail='身份签名密钥不可用')
    if not secret:
        raise HTTPException(status_code=503, detail='身份签名密钥为空')
    import base64 as _b64
    import hmac as _hmac
    import json as _json
    import time as _time
    import uuid as _uuid
    role = 'admin' if user.get('role') == 'admin' else 'viewer'
    scopes = (['accounts:add'] if user.get('role') == 'account_admin' else
              ['*'] if role == 'admin' else ['read'])
    now = int(_time.time())
    payload = {
        'sub': str(user.get('username') or 'unknown'),
        'role': role,
        'scopes': scopes,
        'auth_method': 'edge_session',
        'aud': 'qtg',
        'exp': now + 300,
        'iat': now,
        'iss': 'manager',
        'jti': _uuid.uuid4().hex,
    }
    body = _b64.urlsafe_b64encode(
        _json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    ).decode().rstrip('=')
    derived = hashlib.sha256(('qtg-identity-v1\x00' + secret).encode()).digest()
    sig = _b64.urlsafe_b64encode(
        _hmac.new(derived, body.encode(), hashlib.sha256).digest()
    ).decode().rstrip('=')
    return Response(status_code=204, headers={'X-QTG-Identity': 'v1.%s.%s' % (body, sig)})
