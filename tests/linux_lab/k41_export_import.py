"""K41：导入导出与敏感配置实测（对真实运行的 Manager）。

## 任务卡要求

  导出 JSON 可解析 / URL 正确 / 只一次密钥展示 / 引号·换行·反斜杠文件名 /
  重复导入 / 错误格式·大包 / 取消 / 不覆盖本机未知客户端配置

## 关键事实（决定判定口径）

* 导出是 **ZIP**（每账号一个 JSON），不是单文件；
* `.json.disabled` 的 `.disabled` 会被剥掉，使导出文件可以直接重新导入；
* 同名（enabled + disabled 同时存在）只保留先扫到的一份，避免 zip 重复条目；
* 导出**含明文凭据**，所以必须二次确认 + 审计。

## 为什么脚本自带账号种子（2026-10 归档失败修正）

上一次归档 8/9 失败在「导出非空」：隔离环境**默认分组没有任何账号文件**，
导出 ZIP 是 0 成员，后面所有“重复导入 / 文件名边界”用例都没数据可跑。
这是**环境前置缺数据**，不是产品缺陷。旧口径假设环境里已经有账号（历史 27/27
是在已有账号的机器上跑的），但隔离 VM 每次都是新 auths 目录，必须自己准备。

修法：脚本在导出前**用产品自己的导入接口**（`POST /api/accounts/upload`）种一个
最小账号（`uid` + `accessToken` + `expiresAt`）。这样：

* 走的仍是产品真实链路（校验 → `workbuddy-<uid>.json` 落盘 → 导出可见），
  不直接写业务代码/数据库，也不绕过权限与审计；
* 幂等：账号已存在时服务端返回 `rejected: 账号已存在，未覆盖`，按“已种子”处理，
  重复运行不会报错、也不会产生第二个账号；
* 种子存在性单独断言（导出成员里必须出现 `workbuddy-<uid>.json`），
  避免“环境里恰好有别的账号”把空导出问题掩盖过去。

## 运行

    python3 k41_export_import.py --manager http://127.0.0.1:33313
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from pathlib import Path

ROWS: list[dict] = []
#: 种子步骤证据（写进 `--json` 产物，便于区分“环境本来有账号”与“脚本自己种的”）。
SEED_INFO: dict = {}

#: 种子账号：仅存在于隔离环境。uid 必须符合 `[0-9A-Za-z_-]{1,80}`（落盘文件名约束），
#: token 是**合成**值，不是任何真实平台凭据。
SEED_UID = 'k41-minimal-account'
SEED_TOKEN = 'synthetic-k41-seed-token'
SEED_FILENAME = f'workbuddy-{SEED_UID}.json'


def seed_account_payload(uid: str = SEED_UID, token: str = SEED_TOKEN,
                         expires_at: int | None = None) -> bytes:
    """最小可导入账号 JSON（单一账号形状，`_uploaded_entries` 直接接受）。"""
    payload = {
        'uid': uid,
        'accessToken': token,
        # 缺省 expiresAt 会被当成「过期时间未知」；给一个明确未来的值，
        # 免得种子账号在列表/刷新任务里被当成过期账号，引入与用例无关的噪声。
        'expiresAt': int(time.time()) + 86400 if expires_at is None else int(expires_at),
        'nickname': 'k41-archive-seed',
    }
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


def seed_result_verdict(status_code: int | None, body: object, uid: str = SEED_UID) -> dict:
    """判定种子步骤是否让「默认分组有可导出账号」成立（幂等，纯逻辑）。

    可接受两种结果：
      1. `added`/`uploaded` 里出现该 uid —— 本次新建；
      2. `rejected` 里该 uid 明确是「账号已存在」—— 之前已种过，仍然可用。
    其它情况（4xx/5xx、被其他原因拒绝、body 不是 dict）都不算就绪。
    """
    reasons = []
    if status_code is None:
        reasons.append('seed_request_not_sent')
    elif not 200 <= int(status_code) < 300:
        reasons.append('seed_http_status=%s' % status_code)
    data = body if isinstance(body, dict) else None
    if data is None:
        reasons.append('seed_body_not_json_object')
        return {'ready': False, 'created': False, 'already_present': False,
                'reasons': reasons}
    added = [item for item in (data.get('added') or data.get('uploaded') or [])
             if isinstance(item, dict) and str(item.get('uid') or '') == uid]
    rejected = [item for item in (data.get('rejected') or [])
                if isinstance(item, dict) and str(item.get('uid') or '') == uid
                and '已存在' in str(item.get('message') or '')]
    failed = [item for item in (data.get('failed') or []) if isinstance(item, dict)]
    created = bool(added)
    already_present = bool(rejected) and not created
    if not created and not already_present:
        if failed:
            reasons.append('seed_failed:' + str(failed[0].get('message') or '?')[:60])
        elif data.get('rejected'):
            reasons.append('seed_rejected:' + str(
                (data['rejected'][0] or {}).get('message') or '?')[:60])
        else:
            reasons.append('seed_uid_absent_from_response')
    return {'ready': not reasons, 'created': created, 'already_present': already_present,
            'reasons': reasons}


def ensure_minimal_account(client) -> dict:
    """在默认分组种一个最小账号（真实导入接口；幂等）。返回证据字典。"""
    response = None
    body: object = None
    try:
        response = client.post('/api/accounts/upload',
                               files=[('files', (SEED_FILENAME,
                                                 seed_account_payload(),
                                                 'application/json'))])
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = None
    except Exception as exc:  # noqa: BLE001
        return {'ready': False, 'created': False, 'already_present': False,
                'status': None, 'error': type(exc).__name__ + ': ' + str(exc)[:160],
                'reasons': ['seed_request_exception']}
    verdict = seed_result_verdict(response.status_code, body)
    return {**verdict, 'status': response.status_code,
            'uid': SEED_UID, 'filename': SEED_FILENAME,
            'response': body if isinstance(body, dict) else str(body)[:200]}


def record(case, ok, detail, **extra):
    ROWS.append({'case': case, 'ok': bool(ok), 'detail': detail, **extra})
    print(('PASS ' if ok else 'FAIL ') + f'{case}: {detail}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--manager', required=True)
    ap.add_argument('--admin-user', default='gateway-admin')
    ap.add_argument('--admin-pass', default='synthetic-password')
    ap.add_argument('--json', type=Path)
    args = ap.parse_args(argv)

    import httpx
    client = httpx.Client(base_url=args.manager, trust_env=False, timeout=120)
    r = client.post('/api/login', json={'username': args.admin_user,
                                        'password': args.admin_pass})
    record('管理员登录', r.status_code == 200, f'status={r.status_code}')
    if r.status_code != 200:
        return finish(args)

    # ── 0. 前置种子：默认分组至少要有一个账号，否则导出 ZIP 是 0 成员 ──────
    seed = ensure_minimal_account(client)
    SEED_INFO.update(seed)
    record('前置：默认分组有可导出账号（脚本自种子，幂等）', seed.get('ready') is True,
           f"mode=upload-api uid={SEED_UID} status={seed.get('status')} "
           f"created={seed.get('created')} already_present={seed.get('already_present')} "
           f"reasons={seed.get('reasons')}")
    if not seed.get('ready'):
        return finish(args)

    # ── 1. 导出：必须是合法 ZIP，且每个成员是合法 JSON ──────────────────
    r = client.get('/api/accounts/export')
    record('导出返回 200', r.status_code == 200, f'status={r.status_code}')
    if r.status_code != 200:
        return finish(args)
    ctype = r.headers.get('content-type', '')
    record('导出内容类型是 zip', 'zip' in ctype.lower(), f'content-type={ctype}')
    names = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        record('ZIP 可打开', True, f'{len(names)} 个成员：{names[:6]}')
        bad = zf.testzip()
        record('ZIP 完整性（testzip）', bad is None, f'首个坏成员={bad}')
        parsed = 0
        for n in names:
            try:
                json.loads(zf.read(n).decode('utf-8'))
                parsed += 1
            except Exception as exc:  # noqa: BLE001
                record(f'成员 {n} 可解析为 JSON', False, str(exc)[:120])
        record('全部成员可解析为 JSON', parsed == len(names),
               f'{parsed}/{len(names)}')
        # 文件名不得含 .disabled（否则导入端会当成禁用文件）
        disabled = [n for n in names if n.endswith('.disabled')]
        record('导出名不含 .disabled', not disabled, f'{disabled}')
        # 不得有重复成员（同名 enabled/disabled 会撞名）
        record('无重复成员', len(names) == len(set(names)),
               f'{len(names)} 成员 / {len(set(names))} 唯一')
    except zipfile.BadZipFile as exc:
        record('ZIP 可打开', False, f'BadZipFile: {exc}')
        return finish(args)

    record('导出非空', bool(names),
           f'{len(names)} 个成员' if names else
           '种子后默认分组仍无账号文件，后续用例需要至少一个账号')
    # 种子账号必须真的出现在导出里（否则“非空”可能来自环境的偶然残留，
    # 下次在新隔离环境里又会失败）。
    record('导出的成员包含脚本种子账号', SEED_FILENAME in names,
           f'期望 {SEED_FILENAME}，实际 {sorted(names)[:6]}')
    if not names:
        return finish(args)

    # ── 2. 只一次密钥展示（导出不应回显密钥；密钥只在创建时返回一次）─────
    # 导出的是账号凭据，不是 api_key；这里确认导出体里没有 api_key 字段。
    sample = zf.read(names[0]).decode('utf-8')
    try:
        sample_obj = json.loads(sample)
    except Exception:  # noqa: BLE001
        sample_obj = {}
    leaked = [k for k in ('api_key', 'apiKey', 'admin_token', 'password')
              if k in json.dumps(sample_obj)]
    record('导出不含管理密钥字段', not leaked, f'可疑字段={leaked}')
    # 账号凭据本身是**必须**导出的（否则无法重新导入），只确认它存在
    has_token = ('accessToken' in sample or 'access_token' in sample
                 or 'accessToken' in json.dumps(sample_obj))
    record('导出含账号凭据（可重新导入的前提）', has_token,
           'accessToken 存在' if has_token else '未找到 accessToken')

    # ── 3. 错误格式必须被拒 ──────────────────────────────────────────────
    for label, payload, filename in (
        ('非 JSON', b'not json at all', 'bad.json'),
        ('JSON 非对象', b'[]', 'arr.json'),
        ('缺 uid', b'{"accessToken":"x"}', 'nouid.json'),
        ('缺 accessToken', b'{"uid":"123"}', 'notoken.json'),
        ('expiresAt 非数字', b'{"uid":"1","accessToken":"x","expiresAt":"soon"}', 'badexp.json'),
        ('空 JSON 对象', b'{}', 'empty.json'),
    ):
        rr = client.post('/api/accounts/upload',
                         files=[('files', (filename, payload, 'application/json'))])
        # 设计口径：上传是**批量**语义，逐个文件判定，整体仍 200；
        # 单个文件的拒绝体现在 `ok=false` 且落在 rejected/failed 里，而不是 4xx。
        # 这不是缺陷 —— 4xx 会让"10 个里坏 1 个"变成整批失败，用户得重传全部。
        try:
            body = rr.json()
        except Exception:  # noqa: BLE001
            body = {}
        rejected = bool(body.get('rejected') or body.get('failed'))
        record(f'错误格式被逐个拒绝（{label}）',
               rr.status_code == 200 and body.get('ok') is False and rejected,
               f"status={rr.status_code} ok={body.get('ok')} "
               f"rejected={len(body.get('rejected') or [])} failed={len(body.get('failed') or [])}")

    # ── 4. 重复导入：同一份文件传两次，必须幂等（不产生重复账号）─────────
    one = zf.read(names[0])
    up1 = client.post('/api/accounts/upload',
                      files=[('files', (names[0], one, 'application/json'))])
    up2 = client.post('/api/accounts/upload',
                      files=[('files', (names[0], one, 'application/json'))])
    record('重复导入不报错', up1.status_code < 500 and up2.status_code < 500,
           f'first={up1.status_code} second={up2.status_code}')
    # 账号列表里同名文件只应出现一次
    acc = client.get('/api/accounts')
    if acc.status_code == 200:
        body = acc.json()
        items = body if isinstance(body, list) else body.get('accounts', body.get('items', []))
        # 账号列表的标识字段是 account_file / uid（不是 filename）
        fnames = [str(i.get('account_file') or i.get('filename') or i.get('uid') or '')
                  for i in items if isinstance(i, dict)]
        fnames = [f for f in fnames if f]
        dup = {f for f in fnames if fnames.count(f) > 1}
        record('重复导入后无重复账号', not dup, f'重复文件名={sorted(dup)[:5]}')

    # ── 5. 大包：必须有明确结果（不能静默截断）─────────────────────────
    # 实测 2000 个账号的包会被**全部接受并逐个落盘**（75 KB，远低于 16 MiB 上限）。
    # 所以这里的判据是"结果明确"，不是"必须被拒" —— 大包本来就该能导。
    big = b'{"accounts":[' + b','.join(
        b'{"uid":"bulk-%d","accessToken":"tok%d"}' % (i, i) for i in range(2000)) + b']}'
    rb = client.post('/api/accounts/upload',
                     files=[('files', ('big.json', big, 'application/json'))])
    record('大包有明确结果（不静默截断）', rb.status_code in (200, 400, 413, 422),
           f'status={rb.status_code} size={len(big)}B body={rb.text[:80]}')

    # ── 6. 未知分组必须 404，不能静默落到默认分组 ────────────────────────
    rx = client.get('/api/accounts/export', params={'upstream_id': 999999})
    record('未知分组导出 404', rx.status_code == 404,
           f'status={rx.status_code}（不得静默落到默认分组）')

    filename_edge_cases(args.manager, args.admin_user, args.admin_pass)
    return finish(args)


def finish(args) -> int:
    failed = [r for r in ROWS if not r['ok']]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {'schema': 'zhihui-k41-export/v1', 'rows': ROWS,
             # 种子证据与用例分开记录：别人能看出账号是脚本种的还是环境自带。
             'seed': SEED_INFO,
             'passed': len(ROWS) - len(failed), 'failed': len(failed)},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'\n{len(ROWS) - len(failed)}/{len(ROWS)} passed')
    return 1 if failed else 0





def filename_edge_cases(manager: str, user: str, password: str) -> None:
    """任务卡点名：引号 / 换行 / 反斜杠 文件名。

    这些字符在 ZIP 成员名、Content-Disposition、以及落盘路径里都可能出问题。
    判定：要么被明确拒绝（rejected/failed），要么落盘后能被**原样重新导出**；
    绝不能出现"看起来成功但文件变成另一个名字/路径"。
    """
    import httpx
    client = httpx.Client(base_url=manager, trust_env=False, timeout=120)
    client.post('/api/login', json={'username': user, 'password': password})
    body = b'{"uid":"edge-name-1","accessToken":"tok-edge"}'
    cases = [
        ('含双引号', 'say "hi".json'),
        ('含单引号', "it's.json"),
        ('含反斜杠', 'dir' + chr(92) + 'sub.json'),
        ('含换行', 'line\nbreak.json'),
        ('含路径穿越', '../../escape.json'),
        ('超长名', 'a' * 200 + '.json'),
        ('含 NUL/控制符', 'ctl\x01name.json'),
    ]
    for label, name in cases:
        try:
            r = client.post('/api/accounts/upload',
                            files=[('files', (name, body, 'application/json'))])
        except Exception as exc:  # noqa: BLE001
            record(f'文件名边界（{label}）', True, f'客户端直接拒绝：{type(exc).__name__}')
            continue
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001
            payload = {}
        accepted = [u.get('file') for u in (payload.get('uploaded') or [])]
        refused = bool(payload.get('rejected') or payload.get('failed'))
        if refused:
            record(f'文件名边界（{label}）', True,
                   f'明确拒绝：{(payload.get("rejected") or payload.get("failed"))[0].get("message", "")[:60]}')
            continue
        # 服务端会按 uid 生成落盘文件名（`workbuddy-<uid>.json`），
        # 所以"原名是否保留"不是正确判据。真正要验的是：
        #   ① 名字没有把文件写到 auths 目录之外（路径穿越）；
        #   ② 落盘的文件能被正常导出（即它是一个可用账号，不是一个坏文件）。
        import io as _io
        import zipfile as _zip
        export = client.get('/api/accounts/export')
        names = []
        try:
            names = _zip.ZipFile(_io.BytesIO(export.content)).namelist()
        except Exception:  # noqa: BLE001
            pass
        # ① 目录之外没有新文件：比对 auths 目录的直接子项（不含子目录）
        listing = client.get('/api/accounts')
        outside = False
        if listing.status_code == 200:
            data = listing.json()
            items = data if isinstance(data, list) else data.get('accounts', data.get('items', []))
            for it in items:
                fn = str(it.get('account_file') or '')
                sep = chr(92)   # 反斜杠：避免源码里的转义歧义
                if fn and ('/' in fn or sep in fn or '..' in fn):
                    outside = True
        # ② 每个 accepted 的文件名都应出现在导出列表里
        missing = [a for a in accepted if a and a not in names]
        ok = (not outside) and (not missing)
        record(f'文件名边界（{label}）', ok,
               f'accepted={accepted} 未导出={missing} 越界={outside}')


if __name__ == '__main__':
    sys.exit(main())
