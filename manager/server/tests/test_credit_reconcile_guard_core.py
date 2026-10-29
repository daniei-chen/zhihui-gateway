"""U03：重建/修复/对账入口不得改写 api_keys.used_credit_units。

背景（2026-10-28 审计 U03，P0）：`request_logs` 受 90 天保留期裁剪
（`db._prune_request_logs`），而 `api_keys.used_credit_units` 是**终身累计**的
配额执法值。若某个 apply 路径用 `SUM(request_logs.credit)` 覆盖它，裁剪发生
之后就会把已消耗的配额**退回去**，而且越老越严重。

候选树里确实存在过这样一条路径：`deploy/reconcile_credits.py`
（第 47 行取明细总和、第 109 行 `UPDATE api_keys SET used_credit_units=?`、
第 117 行按同一总和复核）。它不在服务运行路径内，但 `--apply` 直接写库、
默认 DB 指向生产库，必须被视为**禁用**。

本测试锁定修复后的不变量：
  1. 90 天裁剪历史后执行 rebuild，`used_credit_units` 不变（不返还配额）；
  2. rebuild 重复执行幂等，且自检守卫真的会拦下改写；
  3. 重建/修复/对账端点：匿名 401、viewer 403、管理员可调用；
  4. `credit` 为 NULL（上游未回）与 0（确实扣 0）在对账口径里不被混同；
  5. 漂移修复只走不可变事件的**增量上调**，绝不下调（下调=返还配额）。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security, tokensvc  # noqa: E402

DAY = 86400


class CreditReconcileGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = root / 'u03.db'
        config.USERS_FILE = root / 'users.json'
        config.STATIC_DIR = root / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'U' * 64,
            'users': [{'username': 'root', 'role': 'admin',
                       'pwd_hash': security.make_hash('root-pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig
        self._tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────
    def _key(self) -> dict:
        created = keysvc.create_key(name='u03', realm='cn')
        return keysvc.resolve(created['key'])

    def _row(self, key_id: int, credit, days_ago: int = 0) -> None:
        """写一条 request_logs 明细；credit=None 表示上游未回（未知）。"""
        db.execute(
            'INSERT INTO request_logs(ts, key_id, model, credit, realm, '
            'prompt_tokens, completion_tokens) VALUES(?,?,?,?,?,?,?)',
            (int(time.time()) - days_ago * DAY, key_id, 'glm-5.2', credit, 'cn', 100, 50),
        )

    def _units(self, key_id: int) -> int:
        return int(db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?',
                                (key_id,))['used_credit_units'] or 0)

    def _h(self, scope: str) -> dict:
        tok = tokensvc.create_token('u03-' + scope, scope, None, 'test')
        return {'Authorization': 'Bearer ' + tok['token']}

    # ── 1. 90 天裁剪后重建：绝不返还已消耗配额 ───────────────────────────
    def test_rebuild_after_retention_prune_does_not_refund_quota(self) -> None:
        """明细被裁掉一半，rebuild 不得把执法值拉回明细总和。"""
        key = self._key()
        keysvc.touch(key, '1.2.3.4', 1500, 5.0)       # 终身累计 5.0
        self._row(key['id'], 3.0, days_ago=120)       # 会被 90 天保留期裁掉
        self._row(key['id'], 2.0, days_ago=1)
        self.assertEqual(self._units(key['id']), 5_000_000)

        # 模拟 db._prune_request_logs：删除 90 天前的明细
        db.execute('DELETE FROM request_logs WHERE ts < ?',
                   (int(time.time()) - 90 * DAY,))
        self.assertEqual(
            int(db.query_one('SELECT COUNT(*) AS n FROM request_logs')['n']), 1)

        before = db.credit_units_snapshot()
        result = db.rebuild_usage_from_logs()

        # 关键断言：明细只剩 2.0，执法值仍是 5.0 —— 没有返还
        self.assertEqual(self._units(key['id']), 5_000_000)
        self.assertEqual(db.credit_units_snapshot(), before)
        # 重建确实按（剩下的）明细重算了展示层，不是空跑
        self.assertEqual(int(result['rows_after']), 1)

    # ── 2. 幂等 + 自检守卫真的会拦 ───────────────────────────────────────
    def test_rebuild_is_idempotent_and_never_touches_api_keys(self) -> None:
        key = self._key()
        keysvc.touch(key, '1.2.3.4', 1500, 5.0)
        self._row(key['id'], 2.0, days_ago=1)
        self._row(key['id'], 1.0, days_ago=2)
        before = db.credit_units_snapshot()

        db.rebuild_usage_from_logs()
        second = db.rebuild_usage_from_logs()

        self.assertEqual(second['rows_before'], second['rows_after'])
        self.assertEqual(second['requests_delta'], 0)
        self.assertEqual(second['tokens_delta'], 0)
        self.assertEqual(db.credit_units_snapshot(), before)

    def test_guard_rejects_any_credit_units_rewrite(self) -> None:
        """守卫不能是空转：真的改写了就必须抛。"""
        key = self._key()
        keysvc.touch(key, '1.2.3.4', 1000, 5.0)
        before = db.credit_units_snapshot()
        db.execute('UPDATE api_keys SET used_credit_units=0 WHERE id=?', (key['id'],))
        with self.assertRaises(RuntimeError):
            db.assert_credit_units_unchanged(before, 'u03-selftest')

    # ── 3. 权限：匿名 401 / viewer 403 / 管理员 200 ──────────────────────
    def test_rebuild_usage_requires_admin(self) -> None:
        key = self._key()
        keysvc.touch(key, '1.2.3.4', 1000, 5.0)
        before = db.credit_units_snapshot()

        self.assertEqual(self.client.post('/api/stats/rebuild-usage').status_code, 401)
        denied = self.client.post('/api/stats/rebuild-usage',
                                  headers=self._h('readonly'))
        self.assertEqual(denied.status_code, 403, denied.text)

        ok = self.client.post('/api/stats/rebuild-usage', headers=self._h('admin'))
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertIn('rows_after', ok.json())
        # 管理员跑完重建，执法值仍然不变
        self.assertEqual(db.credit_units_snapshot(), before)

    def test_repair_usage_requires_admin(self) -> None:
        self.assertEqual(self.client.post('/api/stats/repair-usage').status_code, 401)
        self.assertEqual(
            self.client.post('/api/stats/repair-usage',
                             headers=self._h('readonly')).status_code, 403)
        ok = self.client.post('/api/stats/repair-usage', headers=self._h('admin'))
        self.assertEqual(ok.status_code, 200, ok.text)

    # ── 4. credit NULL 与 0 不得混同（对账口径）───────────────────────────
    def test_credit_reconcile_separates_null_from_zero(self) -> None:
        key = self._key()
        self._row(key['id'], None, days_ago=1)    # 上游未回：未知，不计入
        self._row(key['id'], 0.0, days_ago=1)     # 确实扣了 0
        self._row(key['id'], 1.5, days_ago=1)     # 实扣 1.5

        r = self.client.get('/api/stats/credit-reconcile', headers=self._h('readonly'))
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        totals = data['totals']
        self.assertEqual(totals['detail_unknown_rows'], 1)
        self.assertEqual(totals['detail_zero_rows'], 1)
        self.assertEqual(totals['detail_known_rows'], 2)
        self.assertAlmostEqual(totals['detail'], 1.5, places=6)   # NULL 不参与求和

        item = next(i for i in data['items'] if i['key_id'] == key['id'])
        self.assertEqual(item['detail_credit_unknown_rows'], 1)
        self.assertEqual(item['detail_credit_known_rows'], 2)
        self.assertAlmostEqual(item['detail_credit'], 1.5, places=6)
        # 只读诊断：绝不改写执法值
        self.assertTrue(data['read_only'])
        self.assertFalse(data['writes_enforced_credit'])
        self.assertEqual(self._units(key['id']), 0)

    # ── 5. 漂移修复：只按不可变事件增量上调，绝不下调 ─────────────────────
    def test_credit_adjust_is_admin_only_and_never_reduces(self) -> None:
        key = self._key()
        keysvc.touch(key, '1.2.3.4', 1000, 5.0)
        body = {'reason': 'u03 drift check', 'apply': True}

        self.assertEqual(
            self.client.post('/api/stats/credit-reconcile/adjust',
                             json=body).status_code, 401)
        self.assertEqual(
            self.client.post('/api/stats/credit-reconcile/adjust', json=body,
                             headers=self._h('readonly')).status_code, 403)

        before = db.credit_units_snapshot()
        r = self.client.post('/api/stats/credit-reconcile/adjust', json=body,
                             headers=self._h('admin'))
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # 账本里没有任何事件 → expected(0) < enforced(5.0)：只报告、不下调
        self.assertEqual(data['applied'], [])
        self.assertEqual([x['key_id'] for x in data['protected_keys']], [key['id']])
        self.assertEqual(db.credit_units_snapshot(), before)   # 一分都没退

    def test_credit_adjust_tops_up_from_immutable_events_only(self) -> None:
        key = self._key()
        # 账本里有一条漏记事件（usage_events 不可变、永不裁剪）
        db.execute(
            'INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('e1', 'req-1', 1, 'usage', int(time.time()), key['id'], 'glm-5.2',
             'cn', 100, 50, 2_000_000, 1, 'settled', 'test', None))
        self.assertEqual(self._units(key['id']), 0)

        body = {'reason': 'u03 backfill', 'apply': True}
        r = self.client.post('/api/stats/credit-reconcile/adjust', json=body,
                             headers=self._h('admin'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._units(key['id']), 2_000_000)

        # 审计：不可变 adjustment_events 带 actor/reason，且落 audit_logs
        ev = db.query_one('SELECT * FROM adjustment_events WHERE key_id=?', (key['id'],))
        self.assertIsNotNone(ev)
        self.assertEqual(int(ev['credit_units_delta']), 2_000_000)
        self.assertIn('u03 backfill', ev['reason'])
        self.assertTrue(ev['actor'])
        self.assertIsNotNone(db.query_one(
            "SELECT 1 FROM audit_logs WHERE action='credit.reconcile' LIMIT 1"))

        # 幂等：再跑一次不再加
        r2 = self.client.post('/api/stats/credit-reconcile/adjust', json=body,
                              headers=self._h('admin'))
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()['applied'], [])
        self.assertEqual(self._units(key['id']), 2_000_000)

    def test_credit_adjust_requires_reason(self) -> None:
        r = self.client.post('/api/stats/credit-reconcile/adjust',
                             json={'apply': True}, headers=self._h('admin'))
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == '__main__':
    unittest.main()
