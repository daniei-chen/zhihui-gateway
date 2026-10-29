"""N05（P1）回归：自动零结算必须在事务内重新核对晚到用量。

## 复现的缺陷（2026-10-28 增量复审 `reaudit-20261028-1108`）

`reconcile_evidenced_unknowns` 是「先扫描、再结清」的两段式：

1. 扫描读到 `charged_tokens=0 / charged_credit=0`，分类为 `upstream_rejected`；
2. 调 `budget.resolve(rid, 0, 0.0, 'auto-reconcile', ..., zero_only=False)`。

`zero_only=False` 时 `resolve()` **不检查 charged**。而扫描与事务之间可能已经有
**晚到的部分用量**落库（回执重放、迟到的结算、另一实例写入），于是事务内算出

```
td       = 0 - 50        = -50           # 用"实际 0"覆盖已记的 50 tokens
cd_units = 0 - 1_000_000 = -1_000_000    # 金额同样被抹掉
```

再写进 `budget_adjustments` / `adjustment_events` —— 不是结清，是**伪造账并把
已消耗的配额退回去**（`UPDATE api_keys SET used_tokens=max(0, used_tokens-50)`）。

第二层问题：`error` 里的 `no_healthy_account` 只证明**至少有一次**上游拒绝，
不证明"所有 attempt 都没被处理"（池侧换号重试时，前一个 attempt 可能已经
打到上游并产生部分计费）。所以结清前还要确认 `attempt_events` 里没有任何
到达上游的阶段。

## 这里钉住

1. `resolve(..., expect_charged_zero=True)`：事务内重读 charged 非 0 → 返回 False，
   **一行都不写**（无 adjustment、无 audit、api_keys 不变、行仍 unknown）；
2. 负向调整**默认拒绝**（`allow_negative=False`）：抛 ValueError，同样不写；
   显式 `allow_negative=True` 才允许"下调"；
3. `reconcile_evidenced_unknowns` 在竞态窗口里遇到晚到用量 → 不结清、不覆盖；
4. `attempt_events` 里有到达上游的阶段 → 跳过（保留 unknown 给人看）；
5. 三方交错（自动结算 / 人工 resolve / 回执重放）不覆盖新事实；
6. 干净的行仍然照常结清（修复不能把正常路径一起挡掉）。
"""
from __future__ import annotations

import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import budget, config, db, keysvc, ledger  # noqa: E402


class _RaceFixture(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        root = pathlib.Path(self._tmp.name)
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        db._conn = None
        db.connect()
        budget._SCHEMA_STATE['key'] = None
        budget.initialize(force=True)
        self.key = keysvc.resolve(keysvc.create_key(name='n05', realm='cn')['key'])
        self.now = int(time.time())

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        budget._SCHEMA_STATE['key'] = None
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _reserve(self, request_id: str, *, state: str = 'unknown', reserved: int = 1000,
                 charged_tokens: int = 0, charged_credit: float = 0.0,
                 age: int = 7200) -> None:
        created = self.now - age
        db.execute(
            'INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
            'reserved_tokens,reserved_credit,charged_tokens,charged_credit,updated_at,'
            'owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (request_id, self.key['id'], created, state, reserved, 0.0,
             charged_tokens, charged_credit, created, 'test', 1, 0))

    def _rejected_log(self, request_id: str) -> None:
        """一条 `upstream_rejected` 分类需要的请求日志（上游明确拒绝、未回 usage）。"""
        db.add_request_log(
            ts=self.now - 7200, key_id=self.key['id'], ip='127.0.0.1', model='m',
            mapped_model='m', status=503, prompt_tokens=0, completion_tokens=0,
            latency_ms=10, ua='pytest', stream=0, credit=None, realm='cn',
            request_id=request_id, terminal_state='upstream_error',
            usage_source='unknown', usage_complete=0,
            error='{"error":{"code":"no_healthy_account"}}', platform='workbuddy')

    def _attempt(self, request_id: str, phase: str, attempt: int = 1) -> None:
        db.execute('INSERT OR IGNORE INTO attempt_events VALUES(?,?,?,?,?,?,?,?,?)',
                   (request_id, attempt, phase, self.now, 'acct', 'm', 'cn', 200, ''))

    def _zero_receipt(self, request_id):
        db.execute("UPDATE request_logs SET usage_source='upstream',usage_complete=1,credit=0 WHERE request_id=?", (request_id,))
        ledger.record(request_id, self.key['id'], 'm', 'cn', 0, 0, 0, False, 'completed', 'fixture')

    def _late_usage(self, request_id: str, tokens: int = 50, credit: float = 1.0) -> None:
        """模拟"扫描之后、事务之前"落库的晚到部分用量。"""
        db.execute('UPDATE budget_reservations SET charged_tokens=?,charged_credit=? '
                   'WHERE request_id=?', (tokens, credit, request_id))

    def _row(self, request_id: str):
        return db.query_one('SELECT * FROM budget_reservations WHERE request_id=?',
                            (request_id,))

    def _adjustments(self, request_id: str) -> list:
        return db.query('SELECT * FROM budget_adjustments WHERE request_id=?',
                        (request_id,))

    def _adjustment_events(self, request_id: str) -> list:
        return db.query('SELECT * FROM adjustment_events WHERE request_id=?',
                        (request_id,))

    def _usage(self) -> tuple[int, int]:
        row = db.query_one('SELECT used_tokens, used_credit_units FROM api_keys WHERE id=?',
                           (self.key['id'],))
        return int(row['used_tokens'] or 0), int(row['used_credit_units'] or 0)

    def _assert_nothing_written(self, request_id: str, before_usage) -> None:
        self.assertEqual(self._adjustments(request_id), [], '拒绝时不许写 budget_adjustments')
        self.assertEqual(self._adjustment_events(request_id), [],
                         '拒绝时不许写不可变 adjustment_events')
        self.assertEqual(self._usage(), before_usage, '拒绝时不许动 api_keys 累计用量')
        self.assertEqual(self._row(request_id)['state'], 'unknown', '拒绝时行仍是 unknown')
        self.assertIsNone(db.query_one(
            "SELECT 1 FROM audit_logs WHERE action='budget.resolve' AND target=?",
            (request_id,)), '拒绝时不许写 resolve 审计')


class ExpectChargedZeroTest(_RaceFixture):

    def test_refuses_after_late_usage_and_writes_nothing(self) -> None:
        self._reserve('rid-late')
        self._rejected_log('rid-late')
        before_usage = self._usage()
        # 扫描阶段：charged 还是 0（此时若直接结清就是缺陷现场）
        self.assertEqual(int(self._row('rid-late')['charged_tokens']), 0)
        # 晚到用量在扫描之后落库
        self._late_usage('rid-late', tokens=50, credit=1.0)
        changed = budget.resolve('rid-late', 0, 0.0, 'auto-reconcile',
                                 '零用量结清', expect_charged_zero=True)
        self.assertFalse(changed, '事务内发现 charged 非 0 必须拒绝结清')
        self._assert_nothing_written('rid-late', before_usage)
        row = self._row('rid-late')
        self.assertEqual(int(row['charged_tokens']), 50, '晚到用量不能被抹掉')
        self.assertEqual(float(row['charged_credit']), 1.0)

    def test_allows_when_charged_is_still_zero(self) -> None:
        self._reserve('rid-clean')
        self._rejected_log('rid-clean')
        self._zero_receipt('rid-clean')
        self.assertTrue(budget.resolve('rid-clean', 0, 0.0, 'auto-reconcile',
                                       '零用量结清', expect_charged_zero=True))
        self.assertEqual(self._row('rid-clean')['state'], 'settled')
        self.assertEqual(len(self._adjustments('rid-clean')), 1)


class NegativeAdjustmentTest(_RaceFixture):

    def test_downward_correction_is_refused_by_default(self) -> None:
        self._reserve('rid-down', charged_tokens=50, charged_credit=1.0)
        before_usage = self._usage()
        with self.assertRaises(ValueError) as caught:
            budget.resolve('rid-down', 10, 0.0, 'ops', '手滑输了个更小的数')
        self.assertIn('不能用更小的数值覆盖', str(caught.exception))
        self._assert_nothing_written('rid-down', before_usage)

    def test_zero_tokens_over_charged_usage_is_refused(self) -> None:
        """定时脚本的调用形态：`resolve(rid, 0, 0.0)`，charged 已非 0。"""
        self._reserve('rid-timer', charged_tokens=50, charged_credit=1.0)
        before_usage = self._usage()
        with self.assertRaises(ValueError):
            budget.resolve('rid-timer', 0, 0.0, 'auto-reconcile', '超期零预扣结清')
        self._assert_nothing_written('rid-timer', before_usage)

    def test_explicit_opt_in_still_allows_downward(self) -> None:
        self._reserve('rid-optin', charged_tokens=50, charged_credit=1.0)
        self.assertTrue(budget.resolve('rid-optin', 10, 0.0, 'ops', '管理员确认实际只有 10',
                                       allow_negative=True))
        row = self._row('rid-optin')
        self.assertEqual(row['state'], 'settled')
        self.assertEqual(int(row['charged_tokens']), 10)
        adjustments = self._adjustments('rid-optin')
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(int(adjustments[0]['token_delta']), -40, '显式声明后才允许负向')


class ReconcileEvidenceTest(_RaceFixture):

    def test_attempt_that_reached_upstream_blocks_auto_settle(self) -> None:
        for request_id, phase in (('rid-headers', 'upstream_headers'),
                                  ('rid-stream', 'stream_terminal'),
                                  ('rid-aggregate', 'aggregate_terminal')):
            self._reserve(request_id)
            self._rejected_log(request_id)
            self._attempt(request_id, phase)
        settled = budget.reconcile_evidenced_unknowns(self.now)
        self.assertEqual(settled, 0, '到达过上游的 attempt 说明可能已部分计费，不能自动结清')
        for request_id in ('rid-headers', 'rid-stream', 'rid-aggregate'):
            self.assertEqual(self._row(request_id)['state'], 'unknown')
            self.assertEqual(self._adjustments(request_id), [])

    def test_attempt_that_never_reached_upstream_does_not_block(self) -> None:
        self._reserve('rid-selected')
        self._rejected_log('rid-selected')
        self._attempt('rid-selected', 'selected')     # 选号阶段，未到达上游
        self._zero_receipt('rid-selected')
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 1)
        self.assertEqual(self._row('rid-selected')['state'], 'settled')

    def test_clean_row_still_settles(self) -> None:
        self._reserve('rid-ok')
        self._rejected_log('rid-ok')
        self._zero_receipt('rid-ok')
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 1)
        row = self._row('rid-ok')
        self.assertEqual(row['state'], 'settled')
        self.assertEqual(int(row['charged_tokens']), 0)
        self.assertEqual(len(self._adjustments('rid-ok')), 1)

    def test_late_usage_landing_inside_the_race_window_is_not_erased(self) -> None:
        """竞态窗口：证据检查通过之后、事务之前，晚到用量落库。"""
        self._reserve('rid-race')
        self._rejected_log('rid-race')
        self._zero_receipt('rid-race')
        before_usage = self._usage()
        original = budget.resolve

        def inject_then_check(request_id: str, *args, **kwargs) -> bool:
            # 模拟"检查完之后、进事务之前"落库的晚到用量
            self._late_usage(request_id, tokens=50, credit=1.0)
            return original(request_id, *args, **kwargs)

        budget.resolve = inject_then_check
        try:
            settled = budget.reconcile_evidenced_unknowns(self.now)
        finally:
            budget.resolve = original
        self.assertEqual(settled, 0, '晚到用量出现后必须放弃结清')
        self._assert_nothing_written('rid-race', before_usage)
        row = self._row('rid-race')
        self.assertEqual(int(row['charged_tokens']), 50)
        self.assertEqual(float(row['charged_credit']), 1.0)

    def test_upstream_phase_constant_matches_ledger(self) -> None:
        """阶段集合与 attempt_events 的写入方同源，不许各写一份。"""
        self.assertEqual(tuple(budget._UPSTREAM_PHASES), tuple(ledger._REACHED_PHASES))


class ThreeWayInterleaveTest(_RaceFixture):
    """自动结算 / 人工 resolve / 回执重放 三方交错，谁先落谁算数，后来者不覆盖。"""

    def test_replay_settles_first_then_auto_settle_skips(self) -> None:
        self._reserve('rid-replay')
        self._rejected_log('rid-replay')
        # 回执重放先把它结算掉（`ledgerreplay._settle` 的等价写入：settled + charged）
        db.execute("UPDATE budget_reservations SET state='settled',charged_tokens=15,"
                   "charged_credit=0.5 WHERE request_id='rid-replay'")
        before_usage = self._usage()
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 0)
        row = self._row('rid-replay')
        self.assertEqual(row['state'], 'settled')
        self.assertEqual(int(row['charged_tokens']), 15, '重放记下的事实不能被覆盖')
        self.assertEqual(self._usage(), before_usage)
        self.assertEqual(self._adjustments('rid-replay'), [])

    def test_manual_resolve_first_then_auto_settle_skips(self) -> None:
        self._reserve('rid-manual')
        self._rejected_log('rid-manual')
        self.assertTrue(budget.resolve('rid-manual', 120, 0.5, 'ops', '人工核对：实际 120'))
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 0)
        row = self._row('rid-manual')
        self.assertEqual(int(row['charged_tokens']), 120)
        self.assertEqual(len(self._adjustments('rid-manual')), 1, '只保留人工那一条')

    def test_auto_settle_first_then_manual_resolve_is_noop(self) -> None:
        self._reserve('rid-auto')
        self._rejected_log('rid-auto')
        self._zero_receipt('rid-auto')
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 1)
        before = self._row('rid-auto')
        self.assertFalse(budget.resolve('rid-auto', 120, 0.5, 'ops', '再来一次'))
        after = self._row('rid-auto')
        self.assertEqual(after['state'], 'settled')
        self.assertEqual(int(after['charged_tokens']), int(before['charged_tokens']))
        self.assertEqual(len(self._adjustments('rid-auto')), 1)

    def test_settled_row_is_never_reopened_by_late_usage(self) -> None:
        self._reserve('rid-stable')
        self._rejected_log('rid-stable')
        self._zero_receipt('rid-stable')
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 1)
        # 结清之后晚到的用量只能落在这行之外（request_logs/usage_events），
        # 本函数不再碰它 —— 断言"不会被自动改回 unknown"
        self._late_usage('rid-stable', tokens=50, credit=1.0)
        self.assertEqual(budget.reconcile_evidenced_unknowns(self.now), 0)
        self.assertEqual(self._row('rid-stable')['state'], 'settled')


class ZeroOnlyPathUnchangedTest(_RaceFixture):

    def test_zero_only_still_refuses_nonzero_reserved(self) -> None:
        """零预扣对账的语义没被 N05 改掉（reserved 非 0 仍然不结清）。"""
        self._reserve('rid-reserved', reserved=5000)
        self.assertFalse(budget.resolve('rid-reserved', 0, 0.0, 'auto-reconcile',
                                        '零预扣对账', zero_only=True))
        self.assertEqual(self._row('rid-reserved')['state'], 'unknown')

    def test_zero_only_allows_fully_zero_row(self) -> None:
        self._reserve('rid-zero', reserved=0)
        self._rejected_log('rid-zero')
        self._zero_receipt('rid-zero')
        self.assertTrue(budget.resolve('rid-zero', 0, 0.0, 'auto-reconcile',
                                       '零预扣对账', zero_only=True))
        self.assertEqual(self._row('rid-zero')['state'], 'settled')


if __name__ == '__main__':
    unittest.main()
