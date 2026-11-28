"""G-05 预算出口三件套：forwarded 泄漏回收 / upstream_rejected 归档 / unresolved 去截断。

三条都来自 2026-11-28 审计（D-05 / D-06 / D-26），共同点是**钱和准入的出口不对等**：

  · D-05  `forwarded` 行的在途判据**没有时间窗**，而 `lease_until` 只写不读 —— 属主
          锁还活着、心跳早停了的那条预留，重启时被 `recover()` 直接 continue，之后
          再也不会有结论。有配额密钥的在途上限默认 1，于是这条行 = 该密钥永久
          `budget_pending`。
  · D-06  上游**明确拒绝**（请求根本没被受理）也被挡在归档之外，对有 `quota_credit`
          的密钥，任意一条这样的 unknown 就是 `credit_quota_unconfirmed` 的永久闸 ——
          一次上游拒绝把密钥锁死到永远。拍板口径：**零计费，但必须归档**。
  · D-26  `unresolved()` 是 `ORDER BY created_at LIMIT 1000` 升序，砍掉的是**最新**的
          那批；`/resolve-batch` 用它建 pending 字典，窗口外的 request_id 被判成
          「不存在」，运维据此核对永远看不到新积压。

判据都是**行为**：准入是否放行、行是否被收回、归档留痕写了什么。
"""

import pathlib
import shutil
import sys
import tempfile
import time
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from server import budget, budgetowner, config, db, keysvc, observability  # noqa: E402


class _BudgetCase(unittest.TestCase):
    """每个用例一个临时库：这些判据都被上一段的残留行污染过。"""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='g05-'))
        self._saved = (config.DB_PATH, config.USERS_FILE)
        if db._conn:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self.tmp / 'm.db'
        config.USERS_FILE = self.tmp / 'users.json'
        db.connect()
        budget.initialize(force=True)
        self.now = int(time.time())
        self.key = self._make_key('g05')

    def tearDown(self) -> None:
        if db._conn:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_key(self, name):
        created = keysvc.create_key(name=name, realm='cn')
        key = keysvc.resolve(created['key'])
        db.execute('UPDATE api_keys SET quota=?, quota_credit=? WHERE id=?',
                   (1_000_000, 10.0, key['id']))
        return key

    def _reserve(self, rid, state, age, owner='probe', lease=None, reserved=4096,
                 key_id=None):
        db.execute(
            'INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
            'reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (rid, key_id or self.key['id'], self.now - age, state, reserved, 0.0,
             self.now - age, owner, 1, self.now + 60 if lease is None else lease))

    def _row(self, rid):
        return dict(db.query_one(
            'SELECT * FROM budget_reservations WHERE request_id=?', (rid,)))

    def _admission(self, key=None):
        """跑一次真实准入：返回拒绝码，或 '' 表示放行。"""
        target = key or self.key
        fresh = dict(db.query_one('SELECT * FROM api_keys WHERE id=?', (target['id'],)))
        observability.current.set(
            observability.RequestAudit('g05-test', '/v1/chat/completions', 'POST',
                                       'openai', time.monotonic()))
        try:
            budget.prepare(fresh, {'model': 'g05-test-model', 'messages': []})
            return ''
        except budget.BudgetRejected as exc:
            return getattr(exc, 'code', '') or str(exc)
        finally:
            observability.current.set(None)


class ForwardedLeaseReclaimTests(_BudgetCase):
    """D-05：租约过期就该收回，不能只看属主锁还在不在。"""

    def test_expired_lease_is_reclaimed_even_while_the_owner_lock_is_alive(self):
        owner = budgetowner.identity()
        self.assertIs(budgetowner.alive(owner), True,
                      '本进程的锁应该是活的，这条用例的前提就不成立')
        self._reserve('leak-live', 'forwarded', age=86400, owner=owner,
                      lease=self.now - 3600)

        budget.recover()

        self.assertNotEqual(self._row('leak-live')['state'], 'forwarded',
                            '属主锁活着但租约早过期，forwarded 行被永远跳过（D-05 泄漏）')

    def test_a_row_that_never_heartbeated_is_not_expired(self):
        """`lease_until=0` 是「还没续过租」，不是「过期」。

        库默认值就是 0，老行与「进程刚起来还没跑心跳」的行都长这样。把它当过期，
        升级后第一次 `recover()` 会把**活着的**在途请求全收掉 ——
        `test_budget_kill_closure` 的进程死亡契约钉的就是这一点。
        """
        owner = budgetowner.identity()
        self._reserve('fresh-owner', 'reserved', age=5, owner=owner, lease=0)

        budget.recover()
        self.assertEqual(self._row('fresh-owner')['state'], 'reserved',
                         'lease_until=0 被当成过期，动了活着的在途请求')
        self.assertEqual(budget.reap_expired_leases(self.now), 0)

    def test_live_lease_is_left_alone(self):
        """没过期的租约不能收 —— 那是真的还在跑的请求。"""
        owner = budgetowner.identity()
        self._reserve('busy', 'forwarded', age=10, owner=owner, lease=self.now + 600)

        budget.recover()

        self.assertEqual(self._row('busy')['state'], 'forwarded',
                         '把还在跑的请求收掉了：租约没过期就必须放过')

    def test_reaper_works_without_a_restart(self):
        """`recover()` 只在启动跑；运行中的泄漏要靠周期性回收。"""
        self._reserve('leak-runtime', 'forwarded', age=86400,
                      owner=budgetowner.identity(), lease=self.now - 3600)

        reclaimed = budget.reap_expired_leases(self.now)

        self.assertEqual(reclaimed, 1, '周期性回收没有收获')
        self.assertNotEqual(self._row('leak-runtime')['state'], 'forwarded')

    def test_reaper_is_idempotent(self):
        self._reserve('leak-once', 'forwarded', age=86400,
                      owner=budgetowner.identity(), lease=self.now - 3600)
        budget.reap_expired_leases(self.now)
        self.assertEqual(budget.reap_expired_leases(self.now), 0,
                         '第二次又收了一遍：行没有离开 forwarded 状态')

    def test_key_can_be_used_again_after_the_leak_is_reclaimed(self):
        """对齐的是用户可见的后果：泄漏行在，密钥就永远 429。"""
        self._reserve('leak-admission', 'forwarded', age=86400,
                      owner=budgetowner.identity(), lease=self.now - 3600)
        self.assertEqual(self._admission(), 'budget_pending',
                         '前提不成立：本来就是放行的')

        budget.reap_expired_leases(self.now)

        fresh = dict(db.query_one('SELECT * FROM api_keys WHERE id=?', (self.key['id'],)))
        db.execute('UPDATE budget_reservations SET updated_at=? WHERE request_id=?',
                   (self.now - 3600, 'leak-admission'))   # 15 分钟窗口也过掉
        code = self._admission(fresh)
        self.assertNotEqual(code, 'budget_pending',
                            '收回之后仍然 budget_pending，密钥还是死的')


class UpstreamRejectedExitTests(_BudgetCase):
    """D-06：上游拒绝 = 零计费**结清**，但记录不许从证据桶消失。

    拍板口径（2026-11-28）把「结清」与「归档」拆成两个动作：
      · 结清 = 解锁密钥 + 零计费 + 可继续用；
      · 归档 = 从证据桶移走 —— **不做**。供应商拒绝恰恰是运维要看见的事实。

    所以本类钉的是**三件事同时成立**：记录还在证据桶、仍带 rejection 标记、
    密钥能用。只证「解锁了」不够 —— 那也可能靠删记录实现。
    """

    def _rejected(self, rid='rej-1', status=429,
                  error='{"error":{"message":"quota_exceeded"}}', age=30):
        self._reserve(rid, 'unknown', age=age * 86400, reserved=0)
        db.execute(
            'INSERT INTO request_logs(ts,key_id,status,request_id,terminal_state,'
            'usage_source,credit,prompt_tokens,completion_tokens,usage_complete,error) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (self.now - age * 86400, self.key['id'], status, rid, 'error', 'unknown',
             None, 0, 0, 0, error))

    def _bucket(self, name):
        from server import budgetreview
        return {item['request_id'] for item in budgetreview.page(bucket=name, limit=100)['items']}

    # ── 结清之后：三件事同时成立 ──────────────────────────────────────
    def test_settled_rejection_keeps_the_record_in_the_evidence_bucket(self):
        self._rejected()
        self.assertEqual(budget.settle_zero_billed_rejections(self.now), 1)

        row = self._row('rej-1')
        self.assertEqual(row['state'], 'unknown', '结清不该把记录挪出 unknown')
        self.assertFalse(row['resolution'].startswith(budget.ARCHIVE_RESOLUTION_PREFIX),
                         '结清被写成了归档留痕 —— 记录会从证据桶消失')
        self.assertIn('零计费', row['resolution'],
                      '结清留痕没写明口径：下一个人会以为这笔钱被放过了')
        self.assertIn('不产生 charged', row['resolution'],
                      '留痕没写清零计费是结构性的（该分类不产生 charged）')
        self.assertIn('rej-1', self._bucket('upstream_rejected'),
                      '结清之后 review 的 upstream_rejected 桶里看不到它了')
        self.assertNotIn('rej-1', self._bucket('archived_pending'),
                         '它不该出现在归档桶')

    def test_settled_rejection_still_shows_as_needing_human_review(self):
        self._rejected()
        budget.settle_zero_billed_rejections(self.now)
        summary = budget.unresolved_summary(key_id=self.key['id'], now=self.now)['summary']
        self.assertEqual(summary['archived_pending'], 0)
        self.assertEqual(summary['needs_human'], 1, '结清不等于不用看：它还得留在人工队列')
        self.assertEqual(summary['by_class']['upstream_rejected'], 1)

    def test_settled_rejection_still_carries_the_rejection_marker(self):
        self._rejected()
        budget.settle_zero_billed_rejections(self.now)
        row = dict(self._row('rej-1'))
        log = budget._logs_for(['rej-1'])['rej-1']
        self.assertEqual(budget._classify(row, log), 'upstream_rejected',
                         '分类被结清改掉了 —— 记录还在，但说的已经不是那件事')

    def test_key_can_be_used_again_after_the_rejection_is_settled(self):
        self._rejected()
        self.assertTrue(self._admission(), '前提不成立：未结清时本来就该被拒')

        budget.settle_zero_billed_rejections(self.now)

        self.assertEqual(self._admission(), '',
                         '结清之后仍然拒绝，出口等于没有')

    # ── 不许偷偷变成「归档」 ──────────────────────────────────────────
    def test_rejections_are_never_archivable(self):
        """归档会把记录移出证据桶 —— 那是三条既有不变量在守的东西。"""
        self.assertNotIn('upstream_rejected', budget.ARCHIVABLE_CLASSES,
                         'upstream_rejected 被放进了归档分类：记录会从证据桶消失')
        self._rejected()
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 0,
                         '归档函数把供应商拒绝收走了')
        self.assertIsNone(self._row('rej-1')['resolution'])

    def test_zero_billing_holds_importantly_no_charged_row_is_ever_rejected(self):
        """「零计费」成立的结构性前提：有 charged 的行先落 charged_partial。"""
        self._rejected('rej-charged2')
        db.execute('UPDATE budget_reservations SET charged_tokens=?, charged_credit=? '
                   'WHERE request_id=?', (128, 0.5, 'rej-charged2'))
        row = self._row('rej-charged2')
        cls = budget._classify(row, budget._logs_for(['rej-charged2'])['rej-charged2'])
        self.assertEqual(cls, 'charged_partial')
        self.assertEqual(budget.settle_zero_billed_rejections(self.now), 0,
                         '已计费的行被按零计费结清了')

    # ── 迟到证据撤销结清 ─────────────────────────────────────────────
    def test_late_usage_evidence_revokes_the_settlement(self):
        self._rejected('rej-late')
        budget.settle_zero_billed_rejections(self.now)
        self.assertEqual(self._admission(), '', '前提：结清后本来可用')

        db.execute("UPDATE request_logs SET usage_source='upstream',usage_complete=1,"
                   "credit=0.25,status=200,terminal_state='completed' WHERE request_id=?",
                   ('rej-late',))
        self.assertEqual(budget.settle_zero_billed_rejections(self.now), 1,
                         '迟到证据到了却没有撤销结清')
        self.assertIsNone(self._row('rej-late')['resolution'])
        self.assertTrue(self._admission(), '撤销结清后应当重新计入额度闸')

    def test_settlement_waits_for_the_review_deadline(self):
        self._rejected('rej-fresh', age=0)
        self.assertEqual(budget.settle_zero_billed_rejections(self.now), 0,
                         '刚发生的拒绝立刻结清：没给重试与迟到回执留窗口')

    def test_settlement_is_idempotent(self):
        self._rejected('rej-once')
        self.assertEqual(budget.settle_zero_billed_rejections(self.now), 1)
        self.assertEqual(budget.settle_zero_billed_rejections(self.now), 0)


class UnresolvedCapTests(_BudgetCase):
    """D-26：`unresolved()` 不能砍掉最新的那批。"""

    def _fill(self, count=1100):
        for i in range(count):
            self._reserve('bulk-%04d' % i, 'unknown', age=86400 + i, reserved=0)

    def test_returns_every_unresolved_row(self):
        self._fill()
        total = db.query_one(
            "SELECT count(*) n FROM budget_reservations WHERE key_id=? AND state IN "
            "('reserved','forwarded','unknown')", (self.key['id'],))['n']
        self.assertEqual(total, 1100)

        rows = budget.unresolved(key_id=self.key['id'])

        self.assertEqual(len(rows), total, '仍然有截断：批量核对看不到全量')

    def test_newest_rows_are_visible(self):
        """截断是「升序 + LIMIT」，所以丢的是最新那批 —— 这正是运维会误判的方向。"""
        self._fill()
        seen = {row['request_id'] for row in budget.unresolved(key_id=self.key['id'])}
        self.assertIn('bulk-0000', seen, '最新的一条不在结果里')
        self.assertIn('bulk-1099', seen, '最老的一条不在结果里')

    def test_order_is_still_ascending(self):
        self._fill(20)
        stamps = [(row['created_at'], row['request_id'])
                  for row in budget.unresolved(key_id=self.key['id'])]
        self.assertEqual(stamps, sorted(stamps), '升序契约变了，面板的阅读顺序会跟着变')

    def test_has_unresolved_answers_the_same_question_without_the_scan(self):
        self.assertFalse(budget.has_unresolved(self.key['id']))
        self._reserve('one-row', 'unknown', age=60, reserved=0)
        self.assertTrue(budget.has_unresolved(self.key['id']))
        self.assertFalse(budget.has_unresolved(self.key['id'] + 999))


if __name__ == '__main__':
    unittest.main()
