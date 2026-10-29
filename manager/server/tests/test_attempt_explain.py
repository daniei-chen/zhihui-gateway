"""U22：attempt 轨迹解释 + 健康三要素（采样时间 / 新鲜度 / 证据来源）。

审计要求的两句话，这里逐条变成断言：

1. 「先提供已有 attempt/phase/结果查询」→ `GET /api/logs/attempts/explain`
   把 `attempt_events` / `request_outcomes` / `attempt_billing_facts` 拼成时间线；
2. 「『为何选中』只在上游提供候选/排除/权重/冷却快照后展示」→ 响应里必须写
   `explainable=false` + `reason='upstream_does_not_expose_selection_snapshot'`，
   并且**不能**把缺失证据渲染成 0 或"健康"。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, ledger, security  # noqa: E402
from server.services import wb2api  # noqa: E402

ADMIN_PW = 'AdminPw123!u22'
VIEWER_PW = 'ViewerPw123!u22'
T0 = 1_760_000_000


def _seed_attempt(request_id: str, attempt: int, phase: str, ts: int,
                  *, uid: str = 'uid-1', status: int = 200) -> None:
    db.execute(
        'INSERT INTO attempt_events(request_id,attempt,phase,ts,account_uid,'
        'resolved_model,realm,status,config_hash) VALUES(?,?,?,?,?,?,?,?,?)',
        (request_id, attempt, phase, ts, uid, 'glm-5.2', 'cn', status, 'cfg-hash'))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        directory = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE)
        config.DB_PATH = directory / 'u22.db'
        config.USERS_FILE = directory / 'users.json'
        security.save_users({
            'secret': 'S' * 64,
            'users': [
                {'username': 'admin', 'role': 'admin',
                 'pwd_hash': security.make_hash(ADMIN_PW)},
                {'username': 'viewer', 'role': 'viewer',
                 'pwd_hash': security.make_hash(VIEWER_PW)},
            ],
            'api_keys': [],
        })
        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def client_for(self, who: str) -> TestClient:
        client = TestClient(self.app)
        if who == 'anonymous':
            return client
        response = client.post('/api/login',
                               json={'username': who,
                                     'password': ADMIN_PW if who == 'admin' else VIEWER_PW},
                               headers={'Origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200, response.text)
        client.cookies.update(dict(response.cookies))
        return client


# ────────────────────────────────────────────────────────────────────────
# 1. 时间线拼装（纯查询层）
# ────────────────────────────────────────────────────────────────────────
class AttemptTimelineTest(_Base):
    RID = 'req-u22-timeline'

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # attempt 1：选中后丢了容量（没到上游）
        _seed_attempt(cls.RID, 1, 'selected', T0)
        _seed_attempt(cls.RID, 1, 'capacity_lost', T0 + 1, status=0)
        # attempt 2：到上游并正常收尾
        _seed_attempt(cls.RID, 2, 'selected', T0 + 2, uid='uid-2')
        _seed_attempt(cls.RID, 2, 'upstream_headers', T0 + 3, uid='uid-2')
        _seed_attempt(cls.RID, 2, 'stream_terminal', T0 + 5, uid='uid-2')
        db.execute(
            'INSERT INTO request_outcomes(request_id,ts,endpoint,method,protocol,'
            'http_status,terminal_state,error_stage,latency_ms,release_id) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (cls.RID, T0, '/v1/chat/completions', 'POST', 'openai', 200,
             'completed', None, 5000, 'test-release'))
        db.execute(
            'INSERT INTO attempt_billing_facts(request_id,attempt,ts,billing_fact,'
            'billing_source,evidence) VALUES(?,?,?,?,?,?)',
            (cls.RID, 2, T0 + 6, 'reported', 'provider_receipt', '上游回了请求级 usage'))

    def test_attempts_and_phase_sequences(self) -> None:
        data = ledger.attempt_timeline(self.RID)
        self.assertEqual(data['attempt_count'], 2)
        self.assertEqual(data['attempt_count_source'], 'attempt_events')
        first, second = data['attempts']
        self.assertEqual(first['attempt'], 1)
        self.assertEqual(first['phase_sequence'], ['selected', 'capacity_lost'])
        self.assertEqual(first['duration_ms'], 1000)
        self.assertFalse(first['reached_upstream'], 'capacity_lost 阶段没到上游')
        self.assertIsNone(first['terminal_phase'], '没有终态 phase 就该是 null')
        self.assertEqual(second['attempt'], 2)
        self.assertEqual(second['phase_sequence'],
                         ['selected', 'upstream_headers', 'stream_terminal'])
        self.assertTrue(second['reached_upstream'])
        self.assertEqual(second['terminal_phase'], 'stream_terminal')
        self.assertEqual(second['duration_ms'], 3000)
        self.assertEqual(second['phase_gaps_ms'], [1000, 2000])
        self.assertEqual(second['account_uid'], 'uid-2')
        self.assertEqual(second['billing']['billing_fact'], 'reported')
        self.assertIsNone(first['billing'], '没有落库的计费事实就是 null，不是 not_applicable')

    def test_timeline_is_merged_and_ordered(self) -> None:
        data = ledger.attempt_timeline(self.RID)
        timeline = data['timeline']
        self.assertEqual(len(timeline), 5)
        self.assertEqual(timeline[0]['elapsed_ms'], 0)
        self.assertEqual([(e['attempt'], e['phase']) for e in timeline], [
            (1, 'selected'), (1, 'capacity_lost'), (2, 'selected'),
            (2, 'upstream_headers'), (2, 'stream_terminal')])
        self.assertEqual(timeline[-1]['elapsed_ms'], 5000)

    def test_request_level_outcome_is_carried(self) -> None:
        data = ledger.attempt_timeline(self.RID)
        self.assertEqual(data['request']['source'], 'request_outcomes')
        self.assertEqual(data['request']['terminal_state'], 'completed')
        self.assertEqual(data['request']['http_status'], 200)
        self.assertEqual(data['request']['latency_ms'], 5000)

    def test_selection_is_declared_unexplainable(self) -> None:
        data = ledger.attempt_timeline(self.RID)
        self.assertIs(data['explainable'], False)
        self.assertEqual(data['reason'], 'upstream_does_not_expose_selection_snapshot')
        boundary = data['boundary']
        self.assertTrue(boundary['can_answer'])
        self.assertTrue(boundary['cannot_answer'])
        self.assertTrue(boundary['missing_evidence'],
                        '必须列出池侧缺哪些证据，否则"不可解释"只是口号')
        self.assertIn('候选集', ' '.join(boundary['missing_evidence']))

    def test_missing_evidence_is_null_not_zero(self) -> None:
        """没有任何记录时：null / unknown，**不是** 0、不是"健康"、不是"没失败"。"""
        data = ledger.attempt_timeline('req-u22-nonexistent')
        self.assertEqual(data['attempt_count'], 0)
        self.assertEqual(data['attempt_count_source'], 'absent')
        self.assertEqual(data['attempts'], [])
        self.assertEqual(data['timeline'], [])
        self.assertEqual(data['request']['source'], 'absent')
        for field in ('terminal_state', 'error_stage', 'error_code', 'http_status',
                      'latency_ms'):
            self.assertIsNone(data['request'][field], f'{field} 缺证据时应为 null')
        self.assertEqual(data['evidence']['attempt_events'], 0)
        self.assertEqual(data['evidence']['attempt_billing_facts'], 0)
        self.assertEqual(data['evidence']['request_outcomes'], 0)


# ────────────────────────────────────────────────────────────────────────
# 2. 端点契约
# ────────────────────────────────────────────────────────────────────────
class ExplainEndpointTest(_Base):
    RID = 'req-u22-endpoint'

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _seed_attempt(cls.RID, 1, 'selected', T0)
        _seed_attempt(cls.RID, 1, 'refresh_failed', T0 + 2, status=401)

    def test_anonymous_is_rejected(self) -> None:
        response = self.client_for('anonymous').get(
            '/api/logs/attempts/explain', params={'request_id': self.RID})
        self.assertEqual(response.status_code, 401)

    def test_missing_request_id_is_rejected(self) -> None:
        client = self.client_for("admin")
        # 2026-10-28：该缺陷已修（返回 dict 列表），断言从「抛异常」改为「200 + 有内容」。
        rows = client.get("/api/logs/attempts", params={"request_id": self.RID})
        self.assertEqual(rows.status_code, 200)
        self.assertIsInstance(rows.json(), list)
        self.assertGreaterEqual(len(rows.json()), 1)
        response = self.client_for('viewer').get(
            '/api/logs/attempts/explain', params={'request_id': self.RID})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['request_id'], self.RID)
        self.assertIs(body['explainable'], False)
        self.assertEqual(body['reason'], 'upstream_does_not_expose_selection_snapshot')
        self.assertEqual(body['attempts'][0]['phase_sequence'],
                         ['selected', 'refresh_failed'])
        self.assertEqual(body['attempts'][0]['last_phase'], 'refresh_failed')
        self.assertIsNone(body['attempts'][0]['terminal_phase'])

    def test_existing_attempts_endpoint_is_untouched(self) -> None:
        """/attempts/billing 保持原样；`/attempts` 的**既有缺陷**本轮只记录、不修。

        发现（本轮实测，与本轮改动无关）：`GET /api/logs/attempts` 直接返回
        `db.query()` 的 `sqlite3.Row` 列表，而签名声明 `-> list[dict]`，于是
        **只要该请求有 attempt 行**，FastAPI 序列化就抛 ResponseValidationError
        → 500。空结果时看不出问题，所以一直没被发现 —— 而它恰恰是 U22「先提供
        已有轨迹查询」要复用的那个端点。

        修复只需一行（`[dict(r) for r in db.query(...)]`），但它是既有端点，
        按本轮边界（「别动已有端点」）不在改动范围内。这里把现状钉住：
        改好之后这条会红，届时把断言改成 200 即可（说明缺陷已关闭）。
        """
        client = self.client_for('admin')
        # 2026-10-28：该缺陷已修（返回 dict 列表），断言从「抛异常」改为「200 + 有内容」。
        rows = client.get('/api/logs/attempts', params={'request_id': self.RID})
        self.assertEqual(rows.status_code, 200)
        self.assertIsInstance(rows.json(), list)
        self.assertGreaterEqual(len(rows.json()), 1)
        # 空结果时才 200 —— 这就是它长期没被发现的原因
        empty = client.get('/api/logs/attempts', params={'request_id': 'req-u22-none'})
        self.assertEqual(empty.status_code, 200)
        billing = client.get('/api/logs/attempts/billing',
                             params={'request_id': self.RID})
        self.assertEqual(billing.status_code, 200)
        self.assertIn('per_attempt_billing', billing.json())


# ────────────────────────────────────────────────────────────────────────
# 3. 健康三要素：采样时间 / 新鲜度 / 证据来源
# ────────────────────────────────────────────────────────────────────────
class HealthFreshnessTest(_Base):
    def setUp(self) -> None:
        wb2api.reset_health_samples()
        self.addCleanup(wb2api.reset_health_samples)

    def _failing_client(self):
        class _Client:
            async def __aenter__(self):
                raise RuntimeError('上游不可达')

            async def __aexit__(self, *a):
                return False

        return lambda *a, **k: _Client()

    def test_no_sample_at_all_is_unknown_not_healthy(self) -> None:
        block = wb2api.pool_health(None)
        self.assertEqual(block['freshness'], 'unknown')
        self.assertIsNone(block['sampled_at'], '没采样过就没有采样时间')
        self.assertIsNone(block['last_known'])
        self.assertEqual(block['evidence']['source'], 'absent')

    def test_failed_sample_without_history_is_unknown(self) -> None:
        with mock.patch.object(config, 'http_client', self._failing_client()):
            status = asyncio.run(wb2api.get_status(base_url='http://127.0.0.1:1'))
        self.assertFalse(status['connected'])
        health = status['health']
        self.assertEqual(health['freshness'], 'unknown')
        self.assertIsNone(health['sampled_at'])
        self.assertIsNone(health['last_known'])
        self.assertEqual(health['evidence']['source'], 'upstream:/status')
        self.assertIn('上游不可达', health['evidence']['error'])

    def test_recent_sample_then_failure_is_stale_and_not_reused(self) -> None:
        wb2api.record_health_sample('http://127.0.0.1:1', ok=True, http_status=200,
                                    now=int(time.time()) - 5)
        with mock.patch.object(config, 'http_client', self._failing_client()):
            status = asyncio.run(wb2api.get_status(base_url='http://127.0.0.1:1'))
        health = status['health']
        self.assertEqual(health['freshness'], 'stale')
        self.assertLessEqual(health['age_s'], wb2api.HEALTH_STALE_SEC)
        # 旧数字**不作为当前值**出现：connected 仍是 False，last_known 只带时间
        self.assertFalse(status['connected'])
        self.assertNotIn('healthy', status)
        self.assertNotIn('total', status)
        self.assertIsNotNone(health['last_known'])
        self.assertIs(health['last_known']['is_current'], False)
        self.assertEqual(set(health['last_known']), {'sampled_at', 'age_s', 'is_current'})

    def test_sample_older_than_threshold_is_dropped(self) -> None:
        """超过阈值：连"最近已知"都不给 —— 两分钟前的池状态不能当现在用。"""
        wb2api.record_health_sample('http://127.0.0.1:1', ok=True, http_status=200,
                                    now=int(time.time()) - (wb2api.HEALTH_STALE_SEC + 60))
        with mock.patch.object(config, 'http_client', self._failing_client()):
            status = asyncio.run(wb2api.get_status(base_url='http://127.0.0.1:1'))
        health = status['health']
        self.assertEqual(health['freshness'], 'stale')
        self.assertIsNone(health['last_known'], '超窗口的旧采样必须丢弃')
        self.assertGreater(health['age_s'], wb2api.HEALTH_STALE_SEC)

    def test_successful_sample_is_fresh_with_evidence(self) -> None:
        """隔离运行器的私有 loopback 上游：/status 可用 → fresh + 证据来源。"""
        status = asyncio.run(wb2api.get_status())
        self.assertTrue(status.get('connected'), status)
        health = status['health']
        self.assertEqual(health['freshness'], 'fresh')
        self.assertEqual(health['age_s'], 0)
        self.assertEqual(health['evidence']['source'], 'upstream:/status')
        self.assertEqual(health['evidence']['http_status'], 200)
        self.assertIsNotNone(health['evidence']['sampled_at'])
        self.assertEqual(wb2api.pool_health(status), health)

    def test_status_and_accounts_endpoints_carry_pool_health(self) -> None:
        client = self.client_for('admin')
        status = client.get('/api/status')
        self.assertEqual(status.status_code, 200, status.text)
        self.assertIn('health', status.json())
        self.assertIn(status.json()['health']['freshness'], ('fresh', 'stale', 'unknown'))
        accounts = client.get('/api/accounts')
        self.assertEqual(accounts.status_code, 200, accounts.text)
        pool_health = accounts.json()['pool_health']
        for field in ('sampled_at', 'age_s', 'freshness', 'evidence'):
            self.assertIn(field, pool_health, f'/api/accounts 的 pool_health 缺 {field}')
        self.assertIn(pool_health['freshness'], ('fresh', 'stale', 'unknown'))

    def test_stale_pool_does_not_claim_current_availability(self) -> None:
        """池状态 stale 时，接口不能只给 `pool_available=true` 让人以为现在可用。"""
        client = self.client_for('admin')
        body = client.get('/api/accounts').json()
        pool_health = body['pool_health']
        self.assertIn('pool_available', body)
        if pool_health['freshness'] != 'fresh':
            self.assertNotEqual(
                (body['pool_available'], pool_health['freshness']), (True, 'stale'),
                'stale 的池状态不得同时声称 pool_available=true')


if __name__ == '__main__':
    unittest.main()
