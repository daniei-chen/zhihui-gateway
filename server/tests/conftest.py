"""测试会话开始前的隔离清理。

这套测试共用工作区里的 `data/` 目录 —— `config.DB_PATH` 在 import 时就定了，
用例里改不了。于是**上一次会话留下的死信会让之后每个请求被 429 挡掉**
（`ledger_dead_letters_pending`），表现成"莫名其妙的随机回归"。这次我为它多跑
了三轮才定位到：`data/ledger-spool/dead/` 下躺着一个 1055 字节的
`ledger-2-conflict-*.json`。

只清 spool 的 dead 目录（未处置的死信）。`processed/` 与处置记录不动 —— 那是
审计线索，不是状态。
"""
from __future__ import annotations

from pathlib import Path


def pytest_sessionstart(session):
    try:
        from server import ledgerreplay
        dead = Path(ledgerreplay._dead_dir())
    except Exception:            # noqa: BLE001 —— 清理失败不该让整个测试会话起不来
        return
    removed = 0
    for path in sorted(dead.glob('*.json')):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        print(f'\n[conftest] 清掉上一次测试留下的 {removed} 条死信（否则请求会被 429 挡掉）')
