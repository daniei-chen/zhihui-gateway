"""测试会话开始前的隔离清理。

这套测试共用工作区里的 `data/` 目录 —— `config.DB_PATH` 在 import 时就定了，
用例里改不了。于是**上一次会话留下的死信会让之后每个请求被 429 挡掉**
（`ledger_dead_letters_pending`），表现成"莫名其妙的随机回归"。这次我为它多跑
了三轮才定位到：`data/ledger-spool/dead/` 下躺着一个 1055 字节的
`ledger-2-conflict-*.json`。

只清 spool 的 dead 目录（未处置的死信）。`processed/` 与处置记录不动 —— 那是
审计线索，不是状态。

## 2026-11-15：清死信只是一半

审计时发现同一个问题还有另一半：**同一个会话内部**也会互相污染。前一个测试文件
留下的预算预留、限流计数、冷却状态会渗到后一个文件里。

实测：同一批 58 个测试文件，

| 数据目录 | 结果 |
| --- | --- |
| 共用工作区 `data/` | 25 个失败 |
| 每次运行独立的临时目录 | **9 个失败**（剩下的都是与改动无关的既有失败） |

那 25 个里有 16 个是纯粹被前一个文件污染的 —— 它们单独跑都是绿的。

**根治办法是让每次运行用独立目录**：`WB_DATA_DIR` 指向一个临时路径即可。
`select_tests.py` 默认就这么做；直接调 pytest 时请自己设，或者接受干扰的风险。

这里保留死信清理：它在**同一个数据目录内**仍然必要（比如连续两次用同一个目录跑）。
"""
from __future__ import annotations

import os
from pathlib import Path


def pytest_sessionstart(session):
    if not os.environ.get('WB_DATA_DIR'):
        print('\n[conftest] 提示：未设置 WB_DATA_DIR，本次运行共用工作区 data/ 目录；'
              '多个测试文件之间可能互相污染（预算预留、死信、限流计数）。\n'
              '           要隔离请设 WB_DATA_DIR 到临时目录，或改用 '
              'server/tests/select_tests.py --run。')
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
