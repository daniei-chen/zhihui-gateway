"""按账号的首字延迟指标（TTFT）。

全局 p99 只能说"慢"，说不了"谁慢"。2026-11-15 排障时发现两个账号的首字延迟接近
40 秒、其他账号 6–13 秒 —— 少量账号把整体长尾拉高，而账号池的调度/权重/下线全是
按账号做的决策。这组用例守的是四件事：

1. 样本不足的账号**不出现**（否则"p99"只是该账号最大值）；
2. 分位数算对（分组、排序、取值边界）；
3. 序列数量上限生效（防止账号数增长把指标基数撑爆）；
4. 标签值经过转义（账号名带引号会生成坏掉的暴露文本）。
"""
from __future__ import annotations

import unittest
from unittest import mock

from server import main


class _Row(dict):
    """够用的 sqlite3.Row 替身：支持下标访问。"""


def _rows(pairs) -> list[_Row]:
    return [_Row(account=a, v=v) for a, v in pairs]


class AccountTtftTest(unittest.TestCase):
    def setUp(self) -> None:
        main._ACCOUNT_TTFT_CACHE.update(at=0.0, value=None)
        self.addCleanup(main._ACCOUNT_TTFT_CACHE.update, at=0.0, value=None)

    def test_groups_by_account_and_computes_quantiles(self) -> None:
        pairs = [('alpha', v) for v in range(1, 101)] + [('beta', v) for v in range(1, 41)]
        rows = _rows(pairs)
        with mock.patch.object(main.db, 'query', return_value=rows):
            result = main._account_ttft(min_samples=20)
        self.assertEqual({r['account'] for r in result}, {'alpha', 'beta'})
        alpha = next(r for r in result if r['account'] == 'alpha')
        self.assertEqual(alpha['samples'], 100)
        self.assertEqual(alpha['max'], 100)
        # p50 取第 50 个（1..100 升序），p99 取第 99 个
        self.assertEqual((alpha['p50'], alpha['p95'], alpha['p99']), (51, 96, 100))
        self.assertEqual(len(rows), 140)

    def test_accounts_below_min_samples_are_omitted(self) -> None:
        pairs = [('big', v) for v in range(1, 31)] + [('tiny', 40), ('tiny', 80), ('tiny', 120)]
        with mock.patch.object(main.db, 'query', return_value=_rows(pairs)):
            result = main._account_ttft(min_samples=20)
        names = [r['account'] for r in result]
        self.assertEqual(names, ['big'])

    def test_slowest_accounts_come_first_and_cap_is_enforced(self) -> None:
        pairs = []
        for index in range(40):  # 40 个账号，各 25 个样本，延迟依次递增
            pairs += [(f'acct{index:02d}', 100 + index * 10 + k) for k in range(25)]
        with mock.patch.object(main.db, 'query', return_value=_rows(pairs)):
            result = main._account_ttft(min_samples=20, max_accounts=5)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]['account'], 'acct39')  # 最慢的排第一
        p99s = [r['p99'] for r in result]
        self.assertEqual(p99s, sorted(p99s, reverse=True))

    def test_query_failure_returns_none_instead_of_raising(self) -> None:
        with mock.patch.object(main.db, 'query', side_effect=RuntimeError('db gone')):
            self.assertIsNone(main._account_ttft())

    def test_cache_prevents_repeated_queries_within_window(self) -> None:
        with mock.patch.object(main.db, 'query',
                               return_value=_rows([('a', v) for v in range(1, 31)])) as query:
            main._account_ttft()
            main._account_ttft()
        self.assertEqual(query.call_count, 1, '60 秒缓存没有生效，/metrics 会被频繁抓取')

    def test_rows_with_bad_values_are_skipped(self) -> None:
        rows = _rows([('ok', v) for v in range(1, 31)])
        rows.append(_Row(account='bad', v='not-a-number'))
        with mock.patch.object(main.db, 'query', return_value=rows):
            result = main._account_ttft(min_samples=20)
        self.assertEqual([r['account'] for r in result], ['ok'])

    def test_label_values_are_escaped(self) -> None:
        # Prometheus 暴露文本里，标签值必须转义反斜杠/引号/换行；
        # 否则一行坏数据会影响解析，甚至吞掉后续序列。
        self.assertEqual(main._prom_label('a"b'), 'a\\"b')
        self.assertEqual(main._prom_label('a\\b'), 'a\\\\b')
        self.assertEqual(main._prom_label('a\nb'), 'a\\nb')


if __name__ == '__main__':
    unittest.main()
