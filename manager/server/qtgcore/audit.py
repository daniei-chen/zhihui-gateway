"""追加式审计日志（哈希链）。

为什么用哈希链：WorkBuddy Manager 的 `admin_events` 表用触发器禁止
UPDATE/DELETE，并据 hash 链证明历史未被改写。这里用更轻的 JSONL 实现同样
性质——每条记录带 `prev`（上一条的 sha256），任何中间记录被删/改都会在
`verify()` 处暴露。

注意：文件本身仍可被 root 删除；这是「防篡改证据链」而非「防删除」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_GENESIS = "0" * 64
_LOCK = threading.Lock()
_LAST_HASH: Dict[str, str] = {}  # Compatibility only; never authoritative.


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Read a bounded suffix, not the entire historical file."""
    maximum=16*1024*1024
    blocks=[]
    total=count=0
    with path.open('rb') as source:
        source.seek(0,os.SEEK_END)
        position=source.tell()
        while position and count<=limit and total<maximum:
            size=min(65536,position,maximum-total)
            position-=size
            source.seek(position)
            block=source.read(size)
            blocks.append(block);total+=len(block);count+=block.count(b'\n')
    data=b''.join(reversed(blocks))
    lines=data.splitlines()
    if position and lines:
        lines=lines[1:]  # The first line may start before the bounded suffix.
    return [line.decode('utf-8') for line in lines[-limit:]]


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._ensure()

    def _ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _last_hash(self) -> str:
        with self.path.open('rb') as source:
            source.seek(0,os.SEEK_END)
            if source.tell():
                source.seek(-1,os.SEEK_END)
                if source.read(1)!=b'\n':
                    raise ValueError('Audit tail is unterminated; append refused')
        lines=_tail_lines(self.path,1)
        if not lines:
            if self.path.stat().st_size:
                raise ValueError('Audit tail is unreadable; append refused')
            return _GENESIS
        record=json.loads(lines[-1])
        digest=record.get('hash')
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) or self._digest(record)!=digest:
            raise ValueError('Audit tail is corrupt; append refused')
        return digest

    @staticmethod
    def _digest(record: Dict[str, Any]) -> str:
        body = {k: v for k, v in record.items() if k != "hash"}
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def append(
        self,
        action: str,
        *,
        actor: str = "admin-token",
        target: str = "",
        result: str = "ok",
        detail: Optional[Dict[str, Any]] = None,
        request_id: str = "",
        ip: str = "",
    ) -> Dict[str, Any]:
        from ..services.filelock import file_lock
        with _LOCK, file_lock(self.path, writing=True, timeout=2):
            prev = self._last_hash()
            rec: Dict[str, Any] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "epoch": round(time.time(), 3),
                "action": action,
                "actor": actor,
                "target": target,
                "result": result,
                "request_id": request_id,
                "ip": ip,
                "detail": _redact(detail or {}),
                "prev": prev,
            }
            rec["hash"] = self._digest(rec)
            with self.path.open("a", encoding="utf-8") as fh:
                encoded=json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
                if len(encoded.encode('utf-8'))>1024*1024:
                    raise ValueError('Audit record exceeds the bounded line size')
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            _LAST_HASH[str(self.path)] = rec["hash"]
            return rec

    def tail(self, limit: int = 200) -> List[Dict[str, Any]]:
        if limit<=0:return []
        try:
            return [json.loads(line) for line in _tail_lines(self.path,min(1000,limit)) if line.strip()]
        except FileNotFoundError:
            return []

    def verify(self) -> Dict[str, Any]:
        """从第一条开始重算链，返回完整性报告。"""
        prev = _GENESIS
        count = 0
        broken_at: Optional[int] = None
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        broken_at = idx
                        break
                    if rec.get("prev") != prev:
                        broken_at = idx
                        break
                    if rec.get("hash") != self._digest(rec):
                        broken_at = idx
                        break
                    prev = rec["hash"]
                    count += 1
        except FileNotFoundError:
            return {"ok": True, "records": 0, "broken_at": None}
        return {"ok": broken_at is None, "records": count, "broken_at": broken_at}

    def count(self) -> int:
        n = 0
        try:
            with self.path.open("rb") as fh:
                for _ in fh:
                    n += 1
        except FileNotFoundError:
            return 0
        return n


_SENSITIVE = ("token", "password", "secret", "key", "credential", "jwt", "pat")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SENSITIVE):
                out[k] = "***" if v else v
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj
