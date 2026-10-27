#!/usr/bin/env python3
"""QTG 凭据原子轮换工具（R3-L5）。

用途：把「写新值 → 验证 → 提升为正式文件 → 保留回滚点」做成**可审计的原子操作**，
而不是运维手写 `echo > file`（那会出现"内容写一半"的窗口，且没有权限/回滚保证）。

支持的凭据类型
--------------
1. **QTG → Manager 服务令牌**（`manager_read_token_file` / `manager_service_token_file`）
   - `stage`   写 `<file>.next`（临时文件 + fsync + rename，0600），可选 `--verify-cmd`
   - `promote`  `<file>` → `<file>.prev`，`<file>.next` → `<file>`，失败自动回滚
   - `rollback` `<file>.prev` → `<file>`
   - QTG 侧读取顺序是 **next 优先、current 兜底**（见 manager_api.token_candidates），
     所以 stage 之后 QTG **不用重启**就会开始用新值；401/403 时自动回退旧值。
2. **会话签名密钥**（QTG `/opt/qtg/var/session_secret`）
   - `rotate-session` 当前值 → `<file>.previous`，新值写入 `<file>`；
     QTG `verify_session` 先当前、再 `.previous`，所以过渡期内旧 Cookie 仍有效，
     删掉 `.previous` 即完成收口。

安全约束（全部内建）
--------------------
* 原子写：同目录临时文件 + `fsync(file)` + `os.replace` + `fsync(dir)`；
* 权限：写入后 `chmod 600`（POSIX 断言；Windows 只做尽力而为，见 `--json` 输出）；
* **绝不打印凭据明文**：只输出长度 + `sha256[:16]` 指纹；
* 校验失败/提升后读回不一致 → 自动回滚并返回非零退出码；
* `stage` 失败会删掉未验证通过的 `.next`，避免 QTG 读到坏值。

用法
----
    python tests/linux_lab/rotate_qtg_token.py status --file /opt/qtg/secrets/manager_read_token
    python tests/linux_lab/rotate_qtg_token.py stage --file … --new-token-file /root/new.token \
        --verify-cmd 'curl -fsS -H "Authorization: Bearer $(cat /root/new.token)" https://127.0.0.1/api/read-api/health'
    python tests/linux_lab/rotate_qtg_token.py promote --file … --verify-cmd '…'
    python tests/linux_lab/rotate_qtg_token.py rollback --file …
    python tests/linux_lab/rotate_qtg_token.py rotate-session --file /opt/qtg/var/session_secret \
        --new-secret-file /root/new.session.secret
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

NEXT = ".next"
PREV = ".prev"
PREVIOUS = ".previous"


class RotationError(RuntimeError):
    """参数/前置条件不满足（退出码 2）。"""


class VerificationError(RotationError):
    """校验失败，且已回滚（退出码 3）。"""


def fingerprint(value: str) -> dict:
    """只输出长度与 sha256 前 16 位，绝不输出明文（含调试日志也只用这个）。"""
    raw = value.encode("utf-8")
    return {"length": len(raw), "sha256_16": hashlib.sha256(raw).hexdigest()[:16]}


def _mode_of(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, value: str, *, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + rename；绝不直接覆盖目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    data = value if value.endswith("\n") else value + "\n"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_value(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _validate_secret_value(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise RotationError(f"{what} 为空，拒绝写入")
    if any(c.isspace() for c in value):
        raise RotationError(f"{what} 含空白字符（多半是复制串行），拒绝写入")
    if len(value) < 16:
        raise RotationError(f"{what} 过短（{len(value)} 字节），拒绝写入")
    return value


def _verify(cmd: str | None) -> dict:
    if not cmd:
        return {"skipped": True}
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    result = {"command": cmd, "returncode": proc.returncode,
              "stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:]}
    if proc.returncode != 0:
        raise VerificationError(f"验证命令失败（exit={proc.returncode}）：{cmd}\n"
                                f"stderr: {proc.stderr[-300:]}")
    return result


def _check_after_write(path: Path, expected: str) -> None:
    actual = read_value(path)
    if actual != expected.strip():
        raise VerificationError(f"写入后读回不一致：{path}")
    mode = _mode_of(path)
    if os.name == "posix" and mode is not None and mode & 0o077:
        raise VerificationError(f"权限过宽：{path} mode={oct(mode)}（应为 600）")


def status(token_file: Path) -> dict:
    slots = {}
    for label, path in (("current", token_file), ("next", token_file.with_name(token_file.name + NEXT)),
                        ("prev", token_file.with_name(token_file.name + PREV)),
                        ("previous", token_file.with_name(token_file.name + PREVIOUS))):
        if not path.exists():
            slots[label] = {"present": False, "path": str(path)}
            continue
        try:
            value = read_value(path)
            slots[label] = {"present": True, "path": str(path),
                            "mode": oct(_mode_of(path) or 0),
                            "mtime": path.stat().st_mtime,
                            **fingerprint(value)}
        except OSError as exc:
            slots[label] = {"present": True, "path": str(path), "error": str(exc)}
    return {"file": str(token_file), "slots": slots}


def stage(token_file: Path, new_value: str, *, verify_cmd: str | None = None) -> dict:
    value = _validate_secret_value(new_value, "新令牌")
    next_path = token_file.with_name(token_file.name + NEXT)
    _atomic_write(next_path, value)
    try:
        _check_after_write(next_path, value)
        verify = _verify(verify_cmd)
    except BaseException:
        # 未通过验证的 next 不能留给 QTG 读到（读序 next 优先）。
        try:
            next_path.unlink()
        except OSError:
            pass
        raise
    return {"action": "stage", "staged": str(next_path), "verify": verify,
            **fingerprint(value), "current": status(token_file)["slots"]["current"]}


def promote(token_file: Path, *, verify_cmd: str | None = None) -> dict:
    next_path = token_file.with_name(token_file.name + NEXT)
    prev_path = token_file.with_name(token_file.name + PREV)
    if not next_path.exists():
        raise RotationError(f"没有待提升的 {next_path}（先执行 stage）")
    staged = read_value(next_path)
    _validate_secret_value(staged, "staged 令牌")
    verify = _verify(verify_cmd)
    backup_made = False
    if token_file.exists():
        old = read_value(token_file)
        _atomic_write(prev_path, old)
        _check_after_write(prev_path, old)
        backup_made = True
    try:
        os.replace(next_path, token_file)
        _fsync_dir(token_file.parent)
        _check_after_write(token_file, staged)
    except BaseException:
        # 提升失败：把 .prev 恢复成 current，保证消费者拿到的是"上一个已知可用值"。
        if backup_made:
            _atomic_write(token_file, read_value(prev_path))
            _check_after_write(token_file, read_value(prev_path))
        raise VerificationError(
            f"提升失败，已回滚到 {prev_path if backup_made else '（无备份：原文件本不存在）'}")
    return {"action": "promote", "current": str(token_file), "prev": str(prev_path),
            "verify": verify, **fingerprint(staged)}


def rollback(token_file: Path) -> dict:
    prev_path = token_file.with_name(token_file.name + PREV)
    if not prev_path.exists():
        raise RotationError(f"没有可回滚的 {prev_path}")
    value = read_value(prev_path)
    _atomic_write(token_file, value)
    _check_after_write(token_file, value)
    prev_path.unlink()
    return {"action": "rollback", "restored": str(token_file), **fingerprint(value),
            "note": "已恢复上一版；若仍存在 .next 会保留，可再次 promote"}


def rotate_session(secret_file: Path, new_value: str, *,
                   verify_cmd: str | None = None) -> dict:
    """会话签名密钥轮换：当前值 → `.previous`，新值 → 当前文件。

    QTG 的 `verify_session` 先用当前 secret、失败再用 `.previous`，所以过渡期内
    旧 Cookie 仍有效；删掉 `.previous` 即完成收口（旧会话全部失效）。
    """
    value = _validate_secret_value(new_value, "新会话密钥")
    previous_path = secret_file.with_name(secret_file.name + PREVIOUS)
    had_current = secret_file.exists()
    if had_current:
        old = read_value(secret_file)
        _atomic_write(previous_path, old)
        _check_after_write(previous_path, old)
    try:
        _atomic_write(secret_file, value)
        _check_after_write(secret_file, value)
        verify = _verify(verify_cmd)
    except BaseException:
        if had_current:
            _atomic_write(secret_file, read_value(previous_path))
            _check_after_write(secret_file, read_value(previous_path))
        raise VerificationError("会话密钥轮换失败，已回滚到旧 secret")
    return {"action": "rotate-session", "current": str(secret_file),
            "previous": str(previous_path), "verify": verify, **fingerprint(value),
            "note": "过渡期结束删除 .previous 即完成收口（旧 Cookie 立即失效）"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QTG 凭据原子轮换（R3-L5）")
    sub = ap.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="查看 current/next/prev 槽位（只输出指纹）")
    p_status.add_argument("--file", required=True, type=Path)

    p_stage = sub.add_parser("stage", help="写 <file>.next 并校验（不改变 current）")
    p_stage.add_argument("--file", required=True, type=Path)
    p_stage.add_argument("--new-token-file", required=True, type=Path,
                         help="新令牌文件（'-' 表示从 stdin 读）")
    p_stage.add_argument("--verify-cmd", default=None, help="可选验证命令（exit 0 = 通过）")

    p_promote = sub.add_parser("promote", help="<file>.next → <file>，旧值备份为 .prev")
    p_promote.add_argument("--file", required=True, type=Path)
    p_promote.add_argument("--verify-cmd", default=None)

    p_rollback = sub.add_parser("rollback", help="<file>.prev → <file>")
    p_rollback.add_argument("--file", required=True, type=Path)

    p_session = sub.add_parser("rotate-session",
                               help="会话密钥：当前 → .previous，新值 → 当前")
    p_session.add_argument("--file", required=True, type=Path)
    p_session.add_argument("--new-secret-file", required=True, type=Path)
    p_session.add_argument("--verify-cmd", default=None)

    for parser in (p_status, p_stage, p_promote, p_rollback, p_session):
        parser.add_argument("--json", action="store_true", help="JSON 输出（默认也是 JSON）")

    args = ap.parse_args(argv)
    try:
        if args.command == "status":
            result = status(args.file)
        elif args.command == "stage":
            if str(args.new_token_file) == "-":
                raw = sys.stdin.read()
            else:
                raw = args.new_token_file.read_text(encoding="utf-8")
            result = stage(args.file, raw, verify_cmd=args.verify_cmd)
        elif args.command == "promote":
            result = promote(args.file, verify_cmd=args.verify_cmd)
        elif args.command == "rollback":
            result = rollback(args.file)
        else:
            raw = args.new_secret_file.read_text(encoding="utf-8")
            result = rotate_session(args.file, raw, verify_cmd=args.verify_cmd)
    except VerificationError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "kind": "verification"},
                         ensure_ascii=False), file=sys.stderr)
        return 3
    except RotationError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "kind": "precondition"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "kind": "filesystem"},
                         ensure_ascii=False), file=sys.stderr)
        return 4
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
