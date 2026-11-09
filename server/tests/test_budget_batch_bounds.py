"""分批扫描的边界守卫（0.1.22）。

`unresolved_batches` 依赖"keyset 分页 + LIMIT"保证内存有界，否则 17 万行
的账务表会被一次性读进内存。这个性质此前只写在 docstring 里，没有任何测试
守着 —— 一旦有人把分页改回一次性查询，门禁不会报警。
"""
import pytest


def test_batch_query_carries_limit(monkeypatch):
    """查询必须带 LIMIT，且参数就是 batch_size。"""
    from server import budget

    calls = []

    def fake_query_one(sql, args=()):
        return {'created_at': 1, 'request_id': 'r1'}

    def fake_query(sql, args=()):
        calls.append((sql, args))
        return []          # 第一批就空，循环立刻结束

    monkeypatch.setattr(budget.db, 'query_one', fake_query_one)
    monkeypatch.setattr(budget.db, 'query', fake_query)
    list(budget.unresolved_batches(batch_size=7))

    assert calls, '应当至少执行一次分页查询'
    sql, args = calls[0]
    assert 'LIMIT ?' in sql, '分页查询必须带 LIMIT 占位'
    assert 7 in args, 'LIMIT 的参数必须等于 batch_size'


def test_batch_size_is_clamped(monkeypatch):
    """batch_size 被限制在 [1, 500]，极端值不会退化成无界。"""
    from server import budget

    seen = []

    def fake_query_one(sql, args=()):
        return {'created_at': 1, 'request_id': 'r1'}

    def fake_query(sql, args=()):
        seen.append(args)
        return []

    monkeypatch.setattr(budget.db, 'query_one', fake_query_one)
    monkeypatch.setattr(budget.db, 'query', fake_query)

    list(budget.unresolved_batches(batch_size=10 ** 9))
    list(budget.unresolved_batches(batch_size=0))
    list(budget.unresolved_batches(batch_size=-5))

    limits = [a[-1] for a in seen if a]
    assert limits, '应至少记录到一次查询参数'
    assert max(limits) <= 500, 'batch_size 上限必须是 500'
    assert min(limits) >= 1, 'batch_size 下限必须是 1'


def test_empty_table_returns_no_batches(monkeypatch):
    """没有任何未解决行时不产出空批次。"""
    from server import budget
    monkeypatch.setattr(budget.db, 'query_one', lambda sql, args=(): None)
    assert list(budget.unresolved_batches()) == []
