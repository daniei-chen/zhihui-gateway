"""DAN-13：红包的「零份额」不能被当成「不限额度」。

两条不变式，全部断言**行为**（不看源码里有没有某段文本）：

  1. 拆包绝不产出 0 / 负数 / 小于最小单位的份额；总额不够每份一个最小单位
     时显式抛 `RedPacketError`，而不是静默发一份「0 额度」出去。
  2. 「0 额度」只有在**调用方显式要求不限额**时才是「不限」（keylimits 的
     契约）。红包拆出来的 0 份额 —— 包括库里已经存在的存量份额 —— 必须在
     消费侧被拒绝，而不是被当成无限额度的密钥。

第 2 条为什么必须落在消费侧：`redpacket.create_packet` 把每份金额原样当额度
（`quota_credit=amount`），而 `keylimits.validate('quota_credit', 0)` 返回
`0.0`（合法值 = 不限），`budget.prepare` 里 `if qc:` 把 0 读成「没有上限」。
于是「这份没分到钱」与「这把钥匙随便花」是同一个状态。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.redpacket import (  # noqa: E402
    KIND_CREDIT, KIND_TOKEN, MIN_UNIT, MODE_EVEN, MODE_LUCKY,
    RedPacketError, split_amount,
)


@pytest.fixture()
def dataset(monkeypatch):
    """隔离数据目录 + 重新打开的连接（conftest 的说明：共用目录会互相污染）。"""
    from server import budgetowner, config, db
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setattr(config, 'DB_PATH', Path(tmp.name) / 'dan13.db')
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    db.connect()
    try:
        yield db
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        # 预算归属锁的句柄是模块级全局的：不关掉，Windows 上临时目录删不掉
        # （WinError 32，文件仍被本进程持有），POSIX 允许删打开中的文件所以看不出来。
        budgetowner.close()
        tmp.cleanup()


# ── 1. 拆包：每份都是最小单位的正整数倍，总额守恒 ──────────────────────

def test_even_split_never_yields_zero_share():
    """0.03 分 5 份（每份至少 0.01）数学上不够分 —— 必须显式拒绝。

    main 上的实际行为是 `[0.01, 0.01, 0.01, 0.0, 0.0]`：三份钱、两份无限额度。
    """
    with pytest.raises(RedPacketError):
        split_amount(0.03, 5, KIND_CREDIT, MODE_EVEN)

    # 够分的情形：每份 ≥ 最小单位，且各份之和精确等于总额。
    parts = split_amount(0.07, 5, KIND_CREDIT, MODE_EVEN)
    assert len(parts) == 5
    assert all(p >= MIN_UNIT[KIND_CREDIT] for p in parts), parts
    assert round(sum(parts), 2) == 0.07


@pytest.mark.parametrize('total,shares,kind,mode', [
    (0.03, 5, KIND_CREDIT, MODE_EVEN),
    (0.05, 7, KIND_CREDIT, MODE_EVEN),
    (0.02, 3, KIND_CREDIT, MODE_LUCKY),
    (2.5, 2, KIND_TOKEN, MODE_LUCKY),
    (1.0, 3, KIND_TOKEN, MODE_LUCKY),
    (3, 4, KIND_TOKEN, MODE_EVEN),
])
def test_split_refuses_total_without_one_unit_per_share(total, shares, kind, mode):
    """`shares * unit > total` 必须抛错，不能靠钳位凑出 0/负数。"""
    with pytest.raises(RedPacketError):
        split_amount(total, shares, kind, mode)


def test_split_refuses_total_that_cannot_be_conserved():
    """总额不是最小单位的整数倍时没有精确解 —— 拒绝，而不是悄悄少发。

    main 上 `split_amount(0.045, 4, 'credit', MODE_EVEN)` 返回
    `[0.01, 0.01, 0.01, 0.01]`：合计 0.04，凭空少了 0.005。
    """
    with pytest.raises(RedPacketError):
        split_amount(0.045, 4, KIND_CREDIT, MODE_EVEN)


@pytest.mark.parametrize('total,shares,kind', [
    (0.05, 5, KIND_CREDIT),
    (0.07, 5, KIND_CREDIT),
    (0.1, 10, KIND_CREDIT),
    (3, 3, KIND_TOKEN),
    (4, 3, KIND_TOKEN),
    (100, 7, KIND_TOKEN),
])
@pytest.mark.parametrize('mode', [MODE_LUCKY, MODE_EVEN])
def test_200_rounds_never_yield_zero_or_negative_share(total, shares, kind, mode):
    """拼手气/均分各跑 200 轮：没有 0、没有负数、没有小于最小单位的份额。"""
    unit = MIN_UNIT[kind]
    decimals = 2 if kind == KIND_CREDIT else 0
    for _ in range(200):
        parts = split_amount(total, shares, kind, mode)
        assert len(parts) == shares
        assert all(p >= unit for p in parts), parts
        assert all(round(p / unit) * unit == pytest.approx(p) for p in parts), parts
        assert round(sum(parts), decimals) == pytest.approx(round(float(total), decimals))


# ── 2. 写侧：算出来的 0 份额绝不能变成密钥 ──────────────────────────────

def test_packet_creation_refuses_a_computed_zero_share(dataset, monkeypatch):
    """拆包结果里出现 0 就必须整批失败，而不是建出一把不限额度的密钥。

    main 上 `create_packet` 会照单全收：`quota_credit=0` 被 keylimits 读成
    「不限」，那把密钥于是不限额度。
    """
    from server import redpacket

    keys_before = dataset.query_one('SELECT COUNT(*) AS n FROM api_keys')['n']
    # 直接让拆包返回一份 0（模拟修复前的输出）；create_packet 必须拒绝。
    monkeypatch.setattr(redpacket, 'split_amount', lambda *a, **k: [0.0, 0.05])

    with pytest.raises(RedPacketError):
        redpacket.create_packet(name='zero-share', kind=KIND_CREDIT, total=0.05,
                                shares=2, mode=MODE_EVEN, ttl_days=1, actor='tester')

    assert dataset.query_one('SELECT COUNT(*) AS n FROM api_keys')['n'] == keys_before
    assert dataset.query_one('SELECT COUNT(*) AS n FROM red_packets')['n'] == 0


# ── 3. 消费侧：0 额度只有「显式要求」才等于不限 ─────────────────────────

def _prepare(key, audit):
    """走一遍 budget.prepare（额度判定处）。"""
    from server import observability
    token = observability.current.set(audit)
    try:
        from server import budget
        budget.prepare(key, {'model': 'deepseek-v4-flash', 'messages': []})
    finally:
        observability.current.reset(token)


def _audit(request_id: str):
    from server import observability
    return observability.RequestAudit(request_id, '/v1/chat/completions', 'POST',
                                      'openai', time.monotonic())


def test_zero_quota_is_not_unlimited(dataset):
    """存量红包里的 0 额度份额：只读拦截，绝不放行成「不限额度」。"""
    from server import budget, keysvc, observability  # noqa: F401

    # 历史数据形态：一份 0 额度的红包份额 + 一把 quota/quota_credit 全 0 的密钥。
    made = keysvc.create_key(name='legacy-zero-share', quota=0, quota_credit=0)
    packet_id = dataset.execute(
        'INSERT INTO red_packets(title, quota_kind, total_amount, shares, mode, '
        'models, code, created_by, created_at, expires_at) '
        "VALUES('legacy', 'credit', 0.05, 2, 'even', '[]', 'code-legacy', 'tester', ?, ?)",
        (int(time.time()), int(time.time()) + 86400),
    )
    dataset.execute(
        'INSERT INTO red_packet_shares(packet_id, key_id, amount, token) VALUES(?, ?, ?, ?)',
        (int(packet_id), made['id'], 0.0, made['key']),
    )

    key = keysvc.resolve(made['key'])
    assert key is not None
    with pytest.raises(budget.BudgetRejected) as rejected:
        _prepare(key, _audit('dan13-zero-share'))
    assert rejected.value.code == 'zero_share_quota'
    assert '0' in str(rejected.value)  # 文案要看得出来是「这一份是 0」


def test_explicit_unlimited_key_still_works(dataset):
    """最关键的对照：管理员**显式**建的不限额密钥（不属于任何红包）照旧放行。

    修复不能把「0 = 不限」这条合法契约一起打死 —— 没有配额的密钥一直是
    项目的正常形态。
    """
    from server import keylimits, keysvc

    assert keylimits.validate('quota_credit', 0) == 0.0  # 显式 0 仍是合法值
    made = keysvc.create_key(name='admin-unlimited', quota=0, quota_credit=0)
    key = keysvc.resolve(made['key'])
    _prepare(key, _audit('dan13-explicit-unlimited'))  # 不抛异常 = 放行


def test_positive_share_key_is_not_blocked(dataset):
    """正常红包份额（> 0）不受影响。"""
    from server import keysvc

    made = keysvc.create_key(name='normal-share', quota=0, quota_credit=0.05)
    packet_id = dataset.execute(
        'INSERT INTO red_packets(title, quota_kind, total_amount, shares, mode, '
        'models, code, created_by, created_at, expires_at) '
        "VALUES('normal', 'credit', 0.05, 2, 'even', '[]', 'code-normal', 'tester', ?, ?)",
        (int(time.time()), int(time.time()) + 86400),
    )
    dataset.execute(
        'INSERT INTO red_packet_shares(packet_id, key_id, amount, token) VALUES(?, ?, ?, ?)',
        (int(packet_id), made['id'], 0.05, made['key']),
    )
    _prepare(keysvc.resolve(made['key']), _audit('dan13-positive-share'))
