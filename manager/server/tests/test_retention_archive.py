"""K24 · 账本保留/归档分类与归档脚本骨架的测试。

覆盖审计包 K24 的专项场景：分类完整性、未知预算不自动释放、no-delete 触发器
不被删除、归档 → 校验 → 清理的顺序、断点重跑不重不漏、归档文件被篡改可发现。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'manager'))

from server import archival, budget, config, db, retention  # noqa: E402


class PolicyTest(unittest.TestCase):
    def test_every_ledger_table_is_classified(self) -> None:
        names = {d.name for d in retention.DATASETS}
        for required in ('request_logs', 'request_outcomes', 'usage_daily', 'usage_hourly',
                         'usage_events', 'forwarding_events', 'attempt_events',
                         'adjustment_events', 'admin_events', 'budget_reservations',
                         'budget_adjustments', 'audit_logs', 'ip_access_logs',
                         'task_logs', 'checkin_logs', 'api_keys', 'archive_runs'):
            self.assertIn(required, names, f'{required} 没有归类')
        for entry in retention.policy():
            self.assertTrue(entry['evidence'], f"{entry['dataset']} 缺证据来源")
            self.assertTrue(entry['reconcile'], f"{entry['dataset']} 缺对账口径")
            if entry['class'] in (retention.CLASS_LEDGER_EVENT,) and \
                    entry['action'] == retention.ACTION_ARCHIVE_THEN_PRUNE:
                self.fail('不可变事件表不允许 archive-then-prune')

    def test_unknown_budget_is_never_prunable(self) -> None:
        d = retention.dataset('budget_reservations')
        self.assertIn("state IN ('settled','released')", d.archive_filter)
        self.assertIn("state IN ('settled','released')", d.delete_filter)
        self.assertIn('unknown', retention.dataset('budget_reservations').note)


class TempDBTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k24.db'
        db._conn = None
        db.connect()
        self.conn = db._conn
        self._out = Path(self._tmp.name) / 'archive-out'

    def tearDown(self) -> None:
        try:
            db._conn.close()  # type: ignore[union-attr]
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()


class TriggerGuardTest(TempDBTest):
    def test_triggers_present_on_fresh_db(self) -> None:
        retention.assert_immutable_triggers(self.conn)
        have = retention.immutable_triggers(self.conn)
        for table, triggers in retention.IMMUTABLE_TABLES.items():
            self.assertEqual(sorted(have[table]), sorted(triggers))

    def test_missing_trigger_raises(self) -> None:
        db.execute('DROP TRIGGER usage_events_no_delete')
        with self.assertRaises(RuntimeError):
            retention.assert_immutable_triggers(self.conn)

    def test_unknown_budget_not_released_by_reconcile(self) -> None:
        budget.initialize()
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at) '
                   "VALUES(1,'k','h','p',0)")
        db.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,"
                   "reserved_tokens,reserved_credit,charged_tokens,charged_credit,"
                   "updated_at,owner,fence,lease_until) "
                   "VALUES('rid-u',1,?, 'unknown',10,0.1,0,0,?, '',0,0)",
                   (int(time.time()) - 86400 * 400, int(time.time())))
        before = db.query_one("SELECT state FROM budget_reservations WHERE request_id='rid-u'")
        report = retention.reconcile(self.conn)
        self.assertEqual(report['unknown_budget']['counts'].get('unknown'), 1)
        self.assertFalse(report['unknown_budget']['auto_released'])
        after = db.query_one("SELECT state FROM budget_reservations WHERE request_id='rid-u'")
        self.assertEqual(before['state'], after['state'], '对账不得改变未知预算状态')
        self.assertEqual(report['admin_chain']['ok'], True)


class ArchiveTest(TempDBTest):
    def setUp(self) -> None:
        super().setUp()
        # budget_reservations 由 budget.initialize() 建表（不在 db.connect 的保证里）
        budget.initialize()
        self.tool = archival
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at) '
                   "VALUES(1,'k','h','p',0)")
        old = int(time.time()) - 100 * 86400
        for i in range(3):
            db.execute(
                'INSERT INTO request_logs(ts,key_id,model,status,prompt_tokens,'
                'completion_tokens,credit,request_id,terminal_state) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (old + i, 1, 'deepseek-v3', 200, 100, 50, 0.01, f'old-{i}', 'completed'))
        db.execute(
            'INSERT INTO request_logs(ts,key_id,model,status,prompt_tokens,'
            'completion_tokens,request_id,terminal_state) VALUES(?,?,?,?,?,?,?,?)',
            (int(time.time()), 1, 'deepseek-v3', 200, 10, 5, 'new-0', 'completed'))

    def test_archive_then_verify_then_delete(self) -> None:
        result = self.tool.archive_dataset(
            self.conn, 'request_logs', out_dir=self._out,
            before_ts=int(time.time()) - 90 * 86400)
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(result['rows'], 3, '只归档保留期满的行')
        file = Path(result['file'])
        self.assertTrue(file.is_file())
        manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['sha256'], result['sha256'])
        self.assertEqual(manifest['sums_of_this_run']['prompt_tokens'], 300)
        # 校验：内容可解析、sha 一致
        verify = self.tool.verify_archive(file, expected_sha=result['sha256'])
        self.assertTrue(verify['ok'])
        # 删除必须有人工确认的 sha，且只删已归档水位内的行
        with self.assertRaises(SystemExit):
            self.tool.delete_archived(self.conn, 'request_logs',
                                      watermark=result['watermark'],
                                      sha=result['sha256'], confirm_sha='wrong')
        deleted = self.tool.delete_archived(self.conn, 'request_logs',
                                            watermark=result['watermark'],
                                            sha=result['sha256'],
                                            confirm_sha=result['sha256'])
        self.assertEqual(deleted['deleted_rows'], 3)
        left = db.query_one('SELECT COUNT(*) c FROM request_logs')['c']
        self.assertEqual(left, 1, '保留期内的行不能删')
        run = db.query_one("SELECT state, rows_archived, sha256 FROM archive_runs "
                           "WHERE dataset='request_logs' ORDER BY id DESC LIMIT 1")
        self.assertEqual(run['state'], 'deleted')

    def test_rerun_is_idempotent(self) -> None:
        cutoff = int(time.time()) - 90 * 86400
        first = self.tool.archive_dataset(self.conn, 'request_logs',
                                          out_dir=self._out, before_ts=cutoff)
        second = self.tool.archive_dataset(self.conn, 'request_logs',
                                           out_dir=self._out, before_ts=cutoff)
        self.assertEqual(second['rows'], 0, '水位之后没有新行，重复归档不能重复导出')
        self.assertEqual(second['watermark'], first['watermark'])

    def test_tampered_archive_is_detected(self) -> None:
        result = self.tool.archive_dataset(
            self.conn, 'request_logs', out_dir=self._out,
            before_ts=int(time.time()) - 90 * 86400)
        file = Path(result['file'])
        with file.open('ab') as fh:
            fh.write(b'{"tampered":1}\n')
        verify = self.tool.verify_archive(file, expected_sha=result['sha256'])
        self.assertFalse(verify['ok'])

    def test_immutable_dataset_cannot_be_deleted(self) -> None:
        db.execute("INSERT INTO usage_events(event_id,request_id,attempt,event_type,ts,"
                   "prompt_tokens,completion_tokens,counted,provenance) "
                   "VALUES('e1','rid-1',1,'usage',1,10,5,1,'provider-reported')")
        with self.assertRaises(SystemExit):
            self.tool.delete_archived(self.conn, 'usage_events', watermark=1,
                                      sha='x', confirm_sha='x')

    def test_budget_archive_excludes_unknown(self) -> None:
        old = int(time.time()) - 400 * 86400
        for rid, state in (('b-settled', 'settled'), ('b-unknown', 'unknown'),
                           ('b-reserved', 'reserved')):
            db.execute(
                'INSERT INTO budget_reservations(request_id,key_id,created_at,state,'
                'reserved_tokens,reserved_credit,charged_tokens,charged_credit,updated_at,'
                'owner,fence,lease_until) VALUES(?,1,?,?,10,0.1,0,0,?,?,0,0)',
                (rid, old, state, old, ''))
        result = self.tool.archive_dataset(
            self.conn, 'budget_reservations', out_dir=self._out,
            before_ts=int(time.time()) - 180 * 86400)
        self.assertEqual(result['rows'], 1, '未决/未知预算不得归档')
        self.assertEqual(result['sums'].get('charged_tokens'), 0)
        states = {r['state'] for r in db.query('SELECT state FROM budget_reservations')}
        self.assertEqual(states, {'settled', 'unknown', 'reserved'},
                         '归档不得改变预留状态')

    def test_archive_uses_archive_runs_ledger(self) -> None:
        self.tool.archive_dataset(self.conn, 'request_logs', out_dir=self._out,
                                  before_ts=int(time.time()) - 90 * 86400)
        row = db.query_one('SELECT COUNT(*) c FROM archive_runs')
        self.assertEqual(row['c'], 1)
        entry = db.query_one('SELECT * FROM archive_runs ORDER BY id DESC LIMIT 1')
        self.assertIsNotNone(entry['sha256'])
        self.assertGreater(entry['rows_archived'], 0)


if __name__ == '__main__':
    unittest.main()


class ArchiveIntegrityTest(unittest.TestCase):
    """K24 补测：行数/金额对账、归档失败不删源、断点重跑无重漏。

    已有测试覆盖"归档→校验→删除"的正向路径。这里补三个**失败与对账**维度，
    它们才是长期保留能不能信的关键：归档文件里的数字必须和库里的数字对得上，
    归档写到一半失败时源数据一行都不能少。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k24.db'
        db._conn = None
        db.connect()
        self.conn = db._conn
        self._out = Path(self._tmp.name) / 'archive-out'
        budget.initialize()
        self.tool = archival
        db.execute('INSERT INTO api_keys(id,name,key_hash,prefix,created_at) '
                   "VALUES(1,'k','h','p',0)")
        self.old = int(time.time()) - 100 * 86400
        for i in range(5):
            db.execute(
                'INSERT INTO request_logs(ts,key_id,model,status,prompt_tokens,'
                'completion_tokens,credit,request_id,terminal_state) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (self.old + i, 1, 'm', 200, 10, 5, 0.125, f'r{i}', 'completed'))

    def tearDown(self) -> None:
        try:
            db._conn.close()  # type: ignore[union-attr]
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    def test_row_and_amount_reconcile(self) -> None:
        """归档摘要的行数/金额必须与库内实际值逐项相等。"""
        result = self.tool.archive_dataset(
            self.conn, 'request_logs', out_dir=self._out,
            before_ts=int(time.time()) - 90 * 86400)
        self.assertEqual(result['state'], 'verified')
        # 与库内独立统计对比（不复用 archival 的算法）
        expect = db.query_one(
            'SELECT COUNT(*) c, SUM(prompt_tokens) p, SUM(completion_tokens) k, '
            'SUM(credit) cr FROM request_logs WHERE ts < ?',
            (int(time.time()) - 90 * 86400,))
        self.assertEqual(result['rows'], expect['c'])
        self.assertEqual(result['sums']['prompt_tokens'], expect['p'])
        self.assertEqual(result['sums']['completion_tokens'], expect['k'])
        # sums 按列名累计；request_logs 的列是 credit（金额）
        self.assertAlmostEqual(result['sums']['credit'], expect['cr'], places=6)
        # manifest 里的摘要与返回值一致
        manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['rows_in_this_run'], result['rows'])
        self.assertEqual(manifest['sums_of_this_run']['prompt_tokens'], expect['p'])

    def test_archive_failure_does_not_delete_source(self) -> None:
        """归档写失败时，源行必须一行不少（先归档后删除，顺序不能反）。"""
        before = db.query_one('SELECT COUNT(*) c FROM request_logs')['c']
        real_open = Path.open
        target = self._out

        def failing_open(self, *a, **kw):
            if target in self.parents or self.parent == target:
                raise OSError('disk full (simulated)')
            return real_open(self, *a, **kw)

        with unittest.mock.patch.object(Path, 'open', failing_open):
            with self.assertRaises((OSError, SystemExit, RuntimeError)):
                self.tool.archive_dataset(self.conn, 'request_logs',
                                          out_dir=self._out,
                                          before_ts=int(time.time()) - 90 * 86400)
        after = db.query_one('SELECT COUNT(*) c FROM request_logs')['c']
        self.assertEqual(after, before, '归档失败不得删除任何源行')

    def test_resume_after_partial_run_has_no_duplicates(self) -> None:
        """归档中途中断后重跑：不能漏行，也不能把同一行导出两次。"""
        cutoff = int(time.time()) - 90 * 86400
        first = self.tool.archive_dataset(self.conn, 'request_logs',
                                          out_dir=self._out, before_ts=cutoff)
        self.assertEqual(first['rows'], 5)
        # 写入比已归档水位更早的新行（模拟归档后又补录历史数据）
        db.execute(
            'INSERT INTO request_logs(ts,key_id,model,status,prompt_tokens,'
            'completion_tokens,credit,request_id,terminal_state) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (self.old - 10, 1, 'm', 200, 7, 3, 0.5, 'late-0', 'completed'))
        second = self.tool.archive_dataset(self.conn, 'request_logs',
                                           out_dir=self._out, before_ts=cutoff)
        # 新行的 ts 比水位早，但水位机制不应让它被漏掉或重复
        total = first['rows'] + second['rows']
        self.assertEqual(total, 6, f'行数必须是 6（不重不漏），实际 {total}')
        # 两个归档文件之间不得有重复 request_id（各自内部也不得重复）
        seen_per_file = []
        for f in (first['file'], second['file']):
            ids = [json.loads(line)['request_id']
                   for line in Path(f).read_text(encoding='utf-8').splitlines() if line.strip()]
            self.assertEqual(len(ids), len(set(ids)), f'{f} 内部有重复行')
            seen_per_file.append(set(ids))
        overlap = seen_per_file[0] & seen_per_file[1]
        self.assertEqual(overlap, set(), f'两个归档文件重复导出了：{overlap}')
        self.assertEqual(sum(len(s) for s in seen_per_file), 6)
