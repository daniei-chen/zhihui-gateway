"""U02 收尾回归：unknown 的「处理期限」与「已归档待核对」。

背景（2026-10-28 生产）：41 条 unknown 按「有证据才结清」清到 1 条，剩下的
`client_cancelled`（request_id `3e4433326699441fb98cfd568dd7b4fc`）可能已部分
计费，**不能**自动结清；但它会让 `budget_unknown` 告警长期 firing，把真正需要
人看的信号稀释。

这里锁定 U02 收尾的五件事：
  1. 超过 `WB_BUDGET_REVIEW_DEADLINE_DAYS`（默认 7 天）仍无上游证据 → 标记
     「已归档待核对」，**不改 state、不删除**，仍可人工 resolve；
  2. 未超期仍计入 `needs_human_old`（告警判据不变，不提前静音）；
  3. 归档幂等、留痕（`resolution` 前缀 + audit_logs），且**不改用量**；
  4. 有用量证据的分类（charged_partial / usage_present）与可自动结清的
     upstream_rejected 一律不归档；
  5. 告警判据只看 `needs_human_old`，已归档的单独以 `archived_pending` 明示。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import budget, config, db, keysvc, operational  # noqa: E402

DAY = 86400


class UnknownArchiveTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        self._orig_deadline = os.environ.get('WB_BUDGET_REVIEW_DEADLINE_DAYS')
        os.environ.pop('WB_BUDGET_REVIEW_DEADLINE_DAYS', None)
        root = pathlib.Path(self._tmp.name)
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        db._conn = None
        db.connect()
        budget.initialize()
        self.key = keysvc.resolve(keysvc.create_key(name='u02b', realm='cn')['key'])
        self.now = int(time.time())

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        if self._orig_deadline is None:
            os.environ.pop('WB_BUDGET_REVIEW_DEADLINE_DAYS', None)
        else:
            os.environ['WB_BUDGET_REVIEW_DEADLINE_DAYS'] = self._orig_deadline
        operational.alerts.pop('budget_unknown', None)
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _reserve(self, request_id: str, *, state: str = 'unknown', reserved: int = 1000,
                 charged_tokens: int = 0, charged_credit: float = 0.0,
                 age: int = 8 * DAY) -> None:
        created = self.now - age
        db.execute(
            'INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
            'reserved_tokens,reserved_credit,charged_tokens,charged_credit,updated_at,'
            'owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (request_id, self.key['id'], created, state, reserved, 0.0,
             charged_tokens, charged_credit, created, 'test', 1, 0))

    def _log(self, request_id: str, **overrides) -> None:
        fields = {
            'ts': self.now, 'key_id': self.key['id'], 'ip': '127.0.0.1',
            'model': 'm', 'mapped_model': 'm', 'status': 502, 'prompt_tokens': 0,
            'completion_tokens': 0, 'latency_ms': 10, 'ua': 'pytest', 'stream': 0,
            'credit': None, 'realm': 'cn', 'request_id': request_id,
            'terminal_state': 'upstream_error', 'usage_source': 'unknown',
            'error': '{"error":{"code":"no_healthy_account"}}', 'platform': 'workbuddy',
        }
        fields.update(overrides)
        db.add_request_log(**fields)

    def _cancel(self, request_id: str, **overrides) -> None:
        self._log(request_id, status=499, terminal_state='client_cancelled', **overrides)

    def _row(self, request_id: str):
        return db.query_one('SELECT * FROM budget_reservations WHERE request_id=?',
                            (request_id,))

    def _usage(self) -> tuple[int, int]:
        row = db.query_one('SELECT used_tokens, used_credit_units FROM api_keys WHERE id=?',
                           (self.key['id'],))
        return int(row['used_tokens'] or 0), int(row['used_credit_units'] or 0)

    def _summary(self) -> dict:
        return budget.unresolved_summary(now=self.now)['summary']

    # ── 1. 超期无证据 → 归档待核对，不改 state、不删除 ──────────────────
    def test_overdue_evidenceless_unknown_is_archived_without_state_change(self) -> None:
        self._reserve('cancel', reserved=390367, age=8 * DAY)
        self._cancel('cancel')
        before = self._summary()
        self.assertEqual(before['needs_human_old'], 1)
        self.assertEqual(before['archived_pending'], 0)

        self.assertEqual(budget.archive_overdue_unknowns(self.now), 1)

        row = self._row('cancel')
        self.assertIsNotNone(row, '归档不允许删除行')
        self.assertEqual(row['state'], 'unknown', '归档不允许改 state')
        self.assertTrue(str(row['resolution'] or '').startswith(budget.ARCHIVE_RESOLUTION_PREFIX))
        self.assertIn('处理期限', row['resolution'])
        self.assertIn('未改用量', row['resolution'])

        after = self._summary()
        self.assertEqual(after['needs_human_old'], 0, '已归档不该继续触发告警')
        self.assertEqual(after['needs_human'], 0)
        self.assertEqual(after['archived_pending'], 1)
        self.assertEqual(after['archived_pending_tokens'], 390367,
                         'archived_pending_tokens 是预留估算下界')
        self.assertEqual(after['unknown_total'], 1, '归档不是消失，仍在 unknown 汇总里')
        item = [i for i in budget.unresolved_summary(now=self.now)['items']
                if i['request_id'] == 'cancel'][0]
        self.assertTrue(item['archived'])
        self.assertEqual(item['class'], 'client_cancelled')

    def test_archive_does_not_fire_budget_unknown_alert(self) -> None:
        self._reserve('cancel', age=8 * DAY)
        self._cancel('cancel')
        active, _ = operational.budget_unknown_alert(self._summary())
        self.assertTrue(active, '未归档前应处于告警态')
        budget.archive_overdue_unknowns(self.now)
        active, detail = operational.budget_unknown_alert(self._summary())
        self.assertFalse(active, '归档后不得继续 firing')
        self.assertIn('archived-pending 1', detail)
        operational.transition('budget_unknown', active, detail)
        self.assertNotIn('budget_unknown', operational.alerts)

    # ── 2. 未超期仍计入告警 ────────────────────────────────────────────
    def test_recent_unknown_is_not_archived_and_still_alerts(self) -> None:
        self._reserve('fresh', age=2 * DAY)
        self._cancel('fresh')
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 0)
        self.assertIsNone(self._row('fresh')['resolution'])
        summary = self._summary()
        self.assertEqual(summary['needs_human_old'], 1)
        self.assertEqual(summary['archived_pending'], 0)
        self.assertTrue(operational.budget_unknown_alert(summary)[0])

    def test_deadline_env_override_changes_the_cutoff(self) -> None:
        self._reserve('mid', age=3 * DAY)
        self._cancel('mid')
        with mock.patch.dict(os.environ, {'WB_BUDGET_REVIEW_DEADLINE_DAYS': '2'}, clear=False):
            self.assertEqual(budget.review_deadline_days(), 2)
            self.assertEqual(budget.archive_overdue_unknowns(self.now), 1)
        self.assertEqual(self._row('mid')['state'], 'unknown')
        self.assertEqual(self._summary()['archived_pending'], 1)

        # 期限放宽到 30 天：同样的行不该被归档
        self._reserve('mid2', age=3 * DAY)
        self._cancel('mid2')
        with mock.patch.dict(os.environ, {'WB_BUDGET_REVIEW_DEADLINE_DAYS': '30'}, clear=False):
            self.assertEqual(budget.archive_overdue_unknowns(self.now), 0)
        self.assertIsNone(self._row('mid2')['resolution'])
        self.assertEqual(self._summary()['needs_human_old'], 1)

    def test_deadline_default_is_seven_days(self) -> None:
        self.assertEqual(budget.review_deadline_days(), 7)
        self._reserve('d7', age=6 * DAY)
        self._cancel('d7')
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 0)
        self._reserve('d8', age=8 * DAY)
        self._cancel('d8')
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 1)

    # ── 3. 不改用量、幂等、留痕 ───────────────────────────────────────
    def test_archive_does_not_change_usage_or_timestamps(self) -> None:
        self._reserve('cancel', reserved=5000, age=8 * DAY)
        self._cancel('cancel')
        before_usage = self._usage()
        before = self._row('cancel')
        budget.archive_overdue_unknowns(self.now)
        self.assertEqual(self._usage(), before_usage, '归档不得改密钥累计用量')
        after = self._row('cancel')
        self.assertEqual(int(after['charged_tokens']), int(before['charged_tokens']))
        self.assertEqual(float(after['charged_credit']), float(before['charged_credit']))
        self.assertEqual(int(after['updated_at']), int(before['updated_at']),
                         'updated_at 不能刷成现在，否则旧行会被当成「在途」阻塞新请求')
        self.assertIsNone(db.query_one(
            'SELECT 1 FROM budget_adjustments WHERE request_id=?', ('cancel',)),
            '归档不是结清，不允许写调整')

    def test_archive_is_idempotent_and_leaves_audit_trace(self) -> None:
        self._reserve('cancel', age=8 * DAY)
        self._cancel('cancel')
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 1)
        first = self._row('cancel')['resolution']
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 0)
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 0)
        self.assertEqual(self._row('cancel')['resolution'], first, '重复归档不得改写留痕')
        self.assertEqual(
            db.query_one("SELECT count(*) n FROM audit_logs WHERE action='budget.archive'")['n'], 1)
        self.assertEqual(db.query_one(
            "SELECT count(*) n FROM admin_events WHERE action='budget.archive'")['n'], 1)

    # ── 4. 归档后仍可人工 resolve ─────────────────────────────────────
    def test_archived_row_is_still_manually_resolvable(self) -> None:
        self._reserve('cancel', reserved=5000, age=8 * DAY)
        self._cancel('cancel')
        budget.archive_overdue_unknowns(self.now)
        before = self._usage()
        self.assertTrue(budget.resolve('cancel', 120, 0.5, 'ops', '人工核对：上游已计费 120 token'))
        row = self._row('cancel')
        self.assertEqual(row['state'], 'settled')
        self.assertEqual(int(row['charged_tokens']), 120)
        self.assertIn('人工核对', row['resolution'])
        self.assertEqual(self._usage()[0], before[0] + 120)
        self.assertIsNotNone(db.query_one(
            'SELECT 1 FROM adjustment_events WHERE request_id=?', ('cancel',)))

    def test_archived_row_resurfaces_if_late_usage_evidence_arrives(self) -> None:
        """迟到的上游日志带来用量证据时，归档行必须重新计入告警（宁可重报）。"""
        self._reserve('late', age=8 * DAY)
        self._cancel('late')
        budget.archive_overdue_unknowns(self.now)
        self.assertEqual(self._summary()['archived_pending'], 1)
        # 迟到证据：同一 request_id 的用量被补上（分类变成 usage_present）
        db.execute("UPDATE request_logs SET usage_source='upstream',usage_complete=1,"
                   "credit=0.25,status=200,terminal_state='completed' WHERE request_id=?",
                   ('late',))
        summary = self._summary()
        self.assertEqual(summary['archived_pending'], 0)
        self.assertEqual(summary['needs_human'], 1, '有证据的必须重新回到人工队列')
        self.assertEqual(summary['by_class']['usage_present'], 1)

    # ── 5. 有证据 / 可自动结清的分类不归档 ────────────────────────────
    def test_evidence_bearing_classes_are_never_archived(self) -> None:
        self._reserve('partial', charged_tokens=7, age=30 * DAY)
        self._cancel('partial')
        self._reserve('usage', age=30 * DAY)
        self._log('usage', usage_source='upstream', credit=0.1, status=200,
                  terminal_state='completed')
        self._reserve('rejected', age=30 * DAY)
        self._log('rejected')
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 0)
        for request_id in ('partial', 'usage', 'rejected'):
            self.assertIsNone(self._row(request_id)['resolution'], request_id)
        summary = self._summary()
        self.assertEqual(summary['archived_pending'], 0)
        self.assertEqual(summary['auto_reconcilable'], 0)
        self.assertEqual(summary['by_class']['charged_partial'], 1)
        self.assertEqual(summary['by_class']['usage_present'], 1)

    def test_evidenceless_classes_all_get_archived(self) -> None:
        self._reserve('cancel', age=8 * DAY)
        self._cancel('cancel')
        self._reserve('nolog', age=8 * DAY)          # 无请求日志
        self._reserve('weird', age=8 * DAY)          # 未解释
        self._log('weird', error='upstream timeout')
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 3)
        summary = self._summary()
        self.assertEqual(summary['archived_pending'], 3)
        self.assertEqual(summary['needs_human_old'], 0)
        self.assertEqual(summary['needs_human'], 0)

    # ── 6. 只归档 unknown，不碰在途请求 ───────────────────────────────
    def test_in_flight_reservations_are_never_archived(self) -> None:
        self._reserve('reserved', state='reserved', age=30 * DAY)
        self._reserve('forwarded', state='forwarded', age=30 * DAY)
        self.assertEqual(budget.archive_overdue_unknowns(self.now), 0)
        self.assertIsNone(self._row('reserved')['resolution'])
        self.assertIsNone(self._row('forwarded')['resolution'])

    # ── 7. 新变量必须登记在 config-schema.json（否则服务起不来）────────
    def test_review_deadline_is_registered_in_config_schema(self) -> None:
        from server.configcontract import validate_environment
        rows = validate_environment({'WB_BUDGET_REVIEW_DEADLINE_DAYS': '3'})['settings']
        row = rows['WB_BUDGET_REVIEW_DEADLINE_DAYS']
        self.assertEqual(row['value'], 3)
        self.assertEqual(row['unit'], 'days')
        self.assertEqual(rows['WB_BUDGET_RECONCILE_MIN_AGE_SEC']['value'], 7200)
        self.assertEqual(rows['WB_BUDGET_RECONCILE_INTERVAL_SEC']['value'], 900)
        from server.configcontract import ConfigurationError
        with self.assertRaises(ConfigurationError):
            validate_environment({'WB_BUDGET_REVIEW_DEADLINE_DAYS': '0'})


if __name__ == '__main__':
    unittest.main()
