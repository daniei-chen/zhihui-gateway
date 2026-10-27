"""模型别名映射：把下游习惯的名字映射到上游真实模型。

数据文件：`<data_dir>/model_map.json`，形如 `{"gpt-4o": "Qwen3.8-Max"}`。
数据面（`chat/completions`）在路由前先查这张表；管理面（设置 → 模型映射）读写它。

设计要点：
* 读带 mtime 缓存——每个数据面请求都会查一次，不能每次都读盘；
* 写是原子的（tmp + rename），避免半截 JSON 让整条数据面 500；
* 校验在写入侧做（别名/目标非空、长度上限、条数上限），读取侧容错。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict

_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {"path": "", "mtime": 0.0, "data": {}}

MAX_ENTRIES = 200
MAX_KEY_LEN = 120
MAX_VALUE_LEN = 120


def path_of(rt) -> Path:
    return Path(rt.settings.data_dir) / "model_map.json"


def load(rt) -> Dict[str, str]:
    """读映射表（带 mtime 缓存；文件不存在/损坏都返回空表）。"""
    p = path_of(rt)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _CACHE["path"] == str(p) and _CACHE["mtime"] == mtime:
            return dict(_CACHE["data"])  # type: ignore[arg-type]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = {}
    data: Dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                data[k.strip()] = v.strip()
    with _LOCK:
        _CACHE.update(path=str(p), mtime=mtime, data=data)
    return dict(data)


def save(rt, data: Dict[str, str]) -> Dict[str, str]:
    """覆盖保存；非法条目直接跳过（返回实际落盘的内容）。"""
    clean: Dict[str, str] = {}
    for k, v in (data or {}).items():
        key = str(k).strip()[:MAX_KEY_LEN]
        val = str(v).strip()[:MAX_VALUE_LEN]
        if not key or not val or key == val:
            continue
        clean[key] = val
        if len(clean) >= MAX_ENTRIES:
            break
    p = path_of(rt)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    with _LOCK:
        _CACHE.update(path=str(p), mtime=0.0, data=clean)
    return clean


def resolve(rt, model: str) -> str:
    """别名 → 真实模型；没配就原样返回。"""
    if not model:
        return model
    return load(rt).get(model, model)
