#!/usr/bin/env python3
"""智汇网关宿主机健康检查：异常与恢复各告警一次（systemd timer 每 5 分钟）。

**这个文件的权威副本在仓库 `ops/healthcheck.py`，宿主机实际路径是
`/opt/notify/healthcheck.py`。**改完要手工同步过去（`ops/` 不进发行包），
否则下次重装机器就回到旧版本。

## 2026-11-24 的改动（每一条都有实测依据）

1. **摘掉三个早就下线的服务。** 原来探 `zhihui-qoder:8790`、`qoder2-engine:8799`、
   `zhihui-trae:8080`，这三个端口在 2026-10-28 三平台合并后就不存在了，于是脚本
   **每 5 分钟 exit 1 一次**，`systemctl status` 永远是 failed：
   `zhihui-qoder=DOWN qoder2-engine=DOWN zhihui-trae=DOWN ...`。
   后果不是"多几行日志"，而是**所有基于 unit 状态或 exit code 的告警全部失效** ——
   真出事时没人能从"一直红着"里看出新红。
2. **补上网关直连探针。** 原来只经 nginx 反代（7864）看，反代自己挂了就看不出来。
   现在直连 7866 的 `/readyz`（免鉴权，专为此设计）。
3. **补上磁盘水位。** 2026-11-15 那块盘到 94% 才被人发现，而巡检当时只会说"服务活着"。
   盘满不是接口报错，是 SQLite 静默写不进账本。阈值与告警脚本（R11）保持一致：
   ≥85% 记录、≥92% 判失败。
"""
from __future__ import annotations

import datetime as _dt
import gzip
import json
import os
import sqlite3 as _s
import subprocess
import sys
import urllib.request

#: 服务名 → 探活地址。只留**现在真的在跑**的；已下线的一律摘掉，别留在表里。
#: （2026-10-28 三平台合一后 qoder/trae 的 8790/8799/8080 全部不再监听。）
SERVICES = {
    "zhihui-workbuddy": "http://127.0.0.1:7863/healthz",
    # 经 nginx 反代的那条路径：能顺带验证反代本身是通的。
    "workbuddy-manager": "http://127.0.0.1:7864/healthz",
    # 直连网关：反代挂了也能知道网关本体活没活。
    "gateway-direct": "http://127.0.0.1:7866/readyz",
}
STATE = "/var/lib/notify/health.json"
#: 磁盘水位阈值（%）。与 ops/wb-gateway-alert.py 的 R11 保持一致。
DISK_WARN_PCT = 85
DISK_CRIT_PCT = 92


def notify(title: str, body: str, key: str) -> None:
    try:
        subprocess.run(["/opt/notify/notify.py", title, body, key], timeout=30, check=False)
    except Exception:
        pass


def check(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, STATE)


def disk_watermark() -> tuple[float | None, float | None, str]:
    """返回 (已用百分比, 剩余 GB, 说明)。

    拿不到读数时返回 (None, None, '')：**不能**因为读不到就判定失败 —— 那会把
    一次 `df` 抖动变成一次假告警，几次之后这条规则就没人信了。
    """
    try:
        import shutil
        usage = shutil.disk_usage("/")
        pct = round(100.0 * usage.used / usage.total, 1)
        free_gb = round(usage.free / (1024 ** 3), 1)
        return pct, free_gb, f"disk={pct}% free={free_gb}G"
    except Exception:
        return None, None, ""


def kaogong_backup() -> str:
    """kaogong-api 的 SQLite 每日备份（保留 14 天）。

    2026-10-23 审计：该库唯一一次备份停在 2026-09-05，无任何定时任务，RPO 47 天。
    挂在 healthcheck（每 5 分钟一次）里，用「当天文件已存在就跳过」保证一天只做一次。
    """
    src = "/var/lib/kaogong-api/kaogong.db"
    if not os.path.exists(src):
        return ""
    dst_dir = "/opt/backups/kaogong-api"
    day = _dt.date.today().isoformat()
    dst = os.path.join(dst_dir, "kaogong-%s.db.gz" % day)
    if os.path.exists(dst):
        return ""
    try:
        os.makedirs(dst_dir, exist_ok=True)
        tmp = os.path.join(dst_dir, ".tmp-%s.db" % day)
        s = _s.connect("file:%s?mode=ro" % src, uri=True)
        d = _s.connect(tmp)
        with d:
            s.backup(d)
        s.close(); d.close()
        with open(tmp, "rb") as fi, gzip.open(dst, "wb") as fo:
            fo.write(fi.read())
        os.remove(tmp)
        cutoff = (_dt.date.today() - _dt.timedelta(days=14)).isoformat()
        for name in os.listdir(dst_dir):
            if name.startswith("kaogong-") and name[8:18] < cutoff:
                os.remove(os.path.join(dst_dir, name))
        return os.path.basename(dst)
    except Exception:
        return ""


def main() -> int:
    state = load()
    all_ok = True
    lines = []
    for name, url in SERVICES.items():
        ok = check(url)
        prev = bool(state.get(name, True))
        if not ok:
            all_ok = False
            if prev:
                notify(f"服务异常: {name}", f"健康检查失败: {url}", f"health:{name}")
        elif not prev:
            notify(f"服务恢复: {name}", f"健康检查恢复: {url}", f"health:{name}")
        state[name] = ok
        lines.append(f"{name}={'OK' if ok else 'DOWN'}")
    save(state)

    # 磁盘水位：到严重档才判失败，因为 85%~92% 还有清理时间，不该让 unit 变 failed。
    pct, free_gb, disk_line = disk_watermark()
    if disk_line:
        lines.append(disk_line)
    if pct is not None:
        prev = bool(state.get("disk", True))
        if pct >= DISK_CRIT_PCT:
            all_ok = False
            if prev:
                notify("磁盘即将写满",
                       f"根分区已用 {pct}%（剩余 {free_gb}G），阈值 {DISK_CRIT_PCT}%。"
                       "写满后账本是静默丢记录，不是报错。查：du -sh /opt/workbuddy-manager/data/*",
                       "health:disk")
            state["disk"] = False
        elif pct >= DISK_WARN_PCT:
            if prev:
                notify("磁盘水位偏高",
                       f"根分区已用 {pct}%（剩余 {free_gb}G），阈值 {DISK_WARN_PCT}%。"
                       "还在清理窗口内，但请安排。",
                       "health:disk")
            state["disk"] = True
        else:
            if not prev:
                notify("磁盘水位恢复", f"根分区已用 {pct}%（剩余 {free_gb}G）。", "health:disk")
            state["disk"] = True
        save(state)

    try:
        _th = subprocess.run(["/opt/notify/trae_selfheal.py"], capture_output=True, text=True, timeout=300).stdout.strip()
        if _th:
            lines.append(_th)
    except Exception:
        pass
    _kb = kaogong_backup()
    if _kb:
        lines.append("kaogong-backup=%s" % _kb)
    print(" ".join(lines))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
