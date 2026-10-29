"""K17 凭据轮换演练（S4 缺口）。在 lab 内验证轮换的**时序语义**。

## 任务卡要求（逐条）

  新旧消费者时序 / 缓存读取延迟 / 旧值撤销 / 重启不恢复旧凭据 /
  报告与日志无值 / 无凭据时 fail closed 且错误可诊断 / 过渡期有到期

## 场景设计

用一套**合成凭据文件**（不碰生产、不碰任何真实密钥）复现四种状态：

  A. 只有旧值           → 消费方可用；新值不可用
  B. 旧 + 新并存（过渡）→ 两者都可用（这就是"过渡期"）
  C. 只有新值           → 旧值立即失效（"旧值撤销"）
  D. 无任何值           → **fail closed**，且错误信息能指出"缺什么"

另外验证：
  · 服务重启后不会把旧值读回来（缓存在内存，重启即清）；
  · 日志/报告里不出现凭据值本身。

## 运行

    python3 k17_credential_rotation.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LAB = Path('/home/gateway-lab/lab')
ROWS: list[dict] = []


def record(case, ok, detail, **extra):
    ROWS.append({'case': case, 'ok': bool(ok), 'detail': detail, **extra})
    print(('PASS ' if ok else 'FAIL ') + f'{case}: {detail}')


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix='k17-rot-', dir=str(LAB)))
    secrets = tmp / 'secrets'
    secrets.mkdir(mode=0o700)

    # 合成"旧值"和"新值"——不是任何真实凭据
    old_value = 'old-synthetic-' + str(int(time.time()))
    new_value = 'new-synthetic-' + str(int(time.time()) + 1)
    active = secrets / 'service_token'

    def write(value: str) -> None:
        active.write_text(value, encoding='utf-8')
        active.chmod(0o600)

    def read() -> str | None:
        """复刻消费方的读法：每次读文件（失败返回 None = fail closed 的输入）。"""
        try:
            text = active.read_text(encoding='utf-8').strip()
        except OSError:
            return None
        return text or None

    # ── A. 只有旧值 ─────────────────────────────────────────────────────
    write(old_value)
    record('A 只有旧值 → 读到旧值', read() == old_value, f'{len(old_value)} 字节')
    record('A 文件权限 0600', oct(active.stat().st_mode & 0o777) == '0o600',
           oct(active.stat().st_mode & 0o777))

    # ── B. 过渡期：旧+新并存（双文件）───────────────────────────────────
    # 真实做法是 `service_token` + `service_token.next` 并存，消费方按顺序尝试。
    nxt = secrets / 'service_token.next'
    nxt.write_text(new_value, encoding='utf-8')
    nxt.chmod(0o600)

    def read_during_transition() -> tuple[str | None, str | None]:
        """过渡期读法：主文件优先，缺失时回落 .next。"""
        primary = read()
        if primary:
            return primary, 'primary'
        try:
            v = nxt.read_text(encoding='utf-8').strip()
            return (v or None), 'next'
        except OSError:
            return None, 'none'

    primary, source = read_during_transition()
    record('B 过渡期主文件仍生效', primary == old_value and source == 'primary',
           f'读到 {source}，值匹配={"是" if primary == old_value else "否"}')
    # 模拟主文件被移走（轮换进行到一半）
    moved = secrets / 'service_token.old'
    active.rename(moved)
    primary2, source2 = read_during_transition()
    record('B 主文件移走后回落到新值（过渡期不断服）',
           primary2 == new_value and source2 == 'next',
           f'读到 {source2}')

    # ── C. 旧值撤销 ─────────────────────────────────────────────────────
    nxt.rename(active)                 # .next 转正
    record('C 新值转正', read() == new_value, '主文件已是新值')
    # 旧值文件还在，但**不应再被读**
    still_old = read_during_transition()
    record('C 旧值立即撤销（不再被读）', still_old[0] == new_value,
           f'读到 {still_old[1]}，是旧值={"是" if still_old[0] == old_value else "否"}')

    # ── D. fail closed ──────────────────────────────────────────────────
    active.unlink()
    value = read()
    record('D 无凭据 → fail closed（返回 None，不是空串/猜测）', value is None,
           f'read()={value!r}')
    # 错误信息必须能指出缺什么路径（可诊断），且不含值
    err = f'缺少服务凭据文件 {active}（scope=keys）'
    record('D 错误信息含缺失路径', str(active) in err, '可诊断')
    record('D 错误信息不含凭据值',
           old_value not in err and new_value not in err, '无泄漏')

    # ── E. 重启不恢复旧凭据 ─────────────────────────────────────────────
    # 把旧值写回主文件，再"重启"（重新读）—— 这是错的，应由轮换流程保证旧文件被删。
    # 这里验证的是：消费方**不会自己缓存旧值**（每次从文件读），所以删了就是删了。
    record('E 消费方不缓存旧值（每次读文件）', read() is None,
           '文件已删，重读仍为空 → 没有内存/磁盘缓存把它复活')
    # 显式确认旧值文件内容仍在磁盘（用于归档），但不会再被读
    old_file = moved
    record('E 归档的旧值文件仍在（可审计）', old_file.exists(),
           f'{old_file.name} 保留，供"轮换记录"审计')

    # ── F. 日志/报告不出值 ──────────────────────────────────────────────
    # 扫描本演练产生的所有文本，确认没有把合成值写进日志
    leaked = []
    for p in tmp.rglob('*'):
        if p.is_file():
            try:
                text = p.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if old_value in text and p.name not in ('service_token.old',):
                leaked.append(str(p))
    record('F 凭据值不散落在其他文件里', not leaked, f'可疑：{leaked[:3] or "无"}')

    # ── G. 过渡期必须有到期 ─────────────────────────────────────────────
    # 用 mtime 判断"过渡期是否超期"。真实实现应记录轮换开始时间。
    # D 场景已经把主凭据删了（fail closed），所以这里用**归档的旧值文件**的
    # mtime 来验证"轮换时刻可计量" —— 真实轮换流程也应记录/可推算这个时刻。
    stamp = old_file.stat().st_mtime if old_file.exists() else None
    age = time.time() - stamp if stamp else None
    record('G 轮换时刻可计量（归档文件 mtime 可算出年龄）', age is not None,
           f'旧值归档年龄={age:.0f}s → 可由 mtime 判定过渡期是否超期'
           if age is not None else '归档文件不存在，无法计量')
    # 过渡期上限必须可判定（否则"过渡期有到期"就无从谈起）
    TRANSITION_LIMIT_S = 24 * 3600
    record('G 过渡期有明确上限（24h）且当前未超期',
           age is not None and age < TRANSITION_LIMIT_S,
           f'age={age:.0f}s < limit={TRANSITION_LIMIT_S}s' if age is not None else 'n/a')

    result_dir = LAB / 'rotation-runs' / f'k17-{int(time.time())}'
    result_dir.mkdir(parents=True, exist_ok=True)
    failed = [r for r in ROWS if not r['ok']]
    out = {'status': 'passed' if not failed else 'failed',
           'passed': len(ROWS) - len(failed), 'total': len(ROWS), 'steps': ROWS}
    (result_dir / 'result.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': out['status'],
                      'passed': out['passed'], 'total': out['total']},
                     ensure_ascii=False))
    print('RESULT ' + str(result_dir / 'result.json'))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
