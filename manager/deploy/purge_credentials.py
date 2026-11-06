#!/usr/bin/env python3
"""一次性凭据清理：轮换会话密钥、清空遗留 api_keys。

用途：站点被入侵后，即使代码已修复，**已泄露的凭据仍然有效**——
  - users.json.secret 可用来离线伪造任意角色的会话
  - users.json.api_keys 曾是管理端提权后门（已在 v1.0.23 移除，但残留值仍是隐患）

本脚本每次执行都会重新轮换密钥：
  1. 生成新的 secret  → 立即吊销所有既有 cookie（含攻击者伪造的）
  2. 清空 api_keys 数组 → 遗留后门钥匙作废

用法（在部署目录执行，建议先停服务）：
    python3 deploy/purge_credentials.py
    # 或用环境变量指定文件
    WB_USERS_FILE=/opt/workbuddy-manager/users.json python3 deploy/purge_credentials.py

执行后需重启管理端服务。**不会**改动用户与密码——改密码请在面板里做。
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path


def main() -> int:
    path = Path(os.environ.get('WB_USERS_FILE', '') or 'users.json')
    if path.is_symlink() or not path.is_file():
        print(f'未找到 users.json：{path}', file=sys.stderr)
        print('请用 WB_USERS_FILE 指定路径，例如：', file=sys.stderr)
        print('  WB_USERS_FILE=/opt/workbuddy-manager/users.json python3 purge_credentials.py',
              file=sys.stderr)
        return 1

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as handle:
            original_info = os.fstat(handle.fileno())
            if not stat.S_ISREG(original_info.st_mode):
                raise ValueError('凭据必须是常规文件')
            original = handle.read()
        cfg = json.loads(original)
    except Exception as exc:
        print(f'解析失败（未做任何修改）：{exc}', file=sys.stderr)
        return 1
    if not isinstance(cfg, dict):
        print('文件不是合法 JSON 对象（未做任何修改）', file=sys.stderr)
        return 1
    if not isinstance(cfg.get('users', []), list) or any(
            not isinstance(user, dict) for user in cfg.get('users', [])):
        print('users 必须是对象数组（未做任何修改）', file=sys.stderr)
        return 1

    changed = []

    # 1) 轮换 secret：一切既有会话立即失效
    old_secret = str(cfg.get('secret') or '')
    cfg['secret'] = secrets.token_urlsafe(48)
    changed.append(f'secret 已轮换（旧值长度 {len(old_secret)}）')

    # 2) 清空 legacy api_keys（曾用于换管理端权限）
    old_keys = cfg.get('api_keys')
    if old_keys:
        cfg['api_keys'] = []
        changed.append(f'已清空 {len(old_keys)} 个遗留 api_keys')
    else:
        changed.append('api_keys 本就为空')

    # 3) 顺带递增所有用户的会话版本，双保险
    n = 0
    for u in cfg.get('users', []):
        try:
            u['sv'] = int(u.get('sv') or 0) + 1
            n += 1
        except (TypeError, ValueError):
            u['sv'] = 1
            n += 1
    if n:
        changed.append(f'已递增 {n} 个用户的会话版本')

    # Preserve the ORIGINAL bytes, including the old secret. Both outputs are
    # private from creation; never follow/overwrite a predictable .bak path.
    temporary = None
    try:
        backup_fd, backup_name = tempfile.mkstemp(prefix=path.name + '.bak.', dir=path.parent)
        backup = Path(backup_name)
        with os.fdopen(backup_fd, 'wb') as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        output_fd, output_name = tempfile.mkstemp(prefix=path.name + '.rotate.', dir=path.parent)
        temporary = Path(output_name)
        with os.fdopen(output_fd, 'wb') as handle:
            if hasattr(os, 'fchown'):
                os.fchown(handle.fileno(), original_info.st_uid, original_info.st_gid)
            handle.write((json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        print(f'写入失败：{exc}（保留已生成的私有备份）', file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    print('完成：')
    for c in changed:
        print(f'  · {c}')
    print('')
    print(f'备份已存至 {backup}（含旧 secret，请勿外传；确认无误后可删除）')
    print('')
    print('接下来：')
    print('  1. 重启管理端服务（systemctl restart workbuddy-web）')
    print('  2. 用当前密码重新登录（旧登录态全部失效）')
    print('  3. 在「设置 → 管理用户」里改掉管理员密码，并删掉陌生账号')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
