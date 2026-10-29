"""U21 回归：稳定 error_code、轨迹检索、分类统计与健康口径。

背景（2026-10-28 台账 U21，P1）：`request_outcomes`（20 万行）只有
`terminal_state` + `error_stage` 两个宽泛枚举，失败原因只能靠它们猜 ——
「429 到底是限流、密钥并发还是预算拒绝」「502 是上游拒绝还是我们连不上」
都分不出来，发布后想回答「这次发布后哪类错误变多了」只能翻文案。

这里锁定六件事：
  1. **稳定错误码**：上游业务码优先（`11102` / `no_healthy_account`），
     其次网关内部分类（鉴权/预算/容量/协议/传输/取消），都拿不到才是
     `gateway.unknown`；成功一律 `none`（不把抖动过的成功算成错误）。
  2. **码表不能夹带敏感内容**：只允许 `[A-Za-z0-9._:-]{1,64}`，从结构上挡住
     URL、空白、中文提示词与 `Bearer xxx` 之类凭据片段。
  3. **落库**：`request_outcomes.error_code` / `error_source` 补列后真的写进去；
     补列幂等、可重复调用。
  4. **检索**：`/api/logs/outcomes` 按 error_code / terminal_state / platform /
     release_id / 时间窗 / traffic_class 过滤，并带 attempt 数与平台。
  5. **统计**：`/api/stats/outcomes` 分类计数 + error_code Top-N +
     error_code 覆盖率；**unknown 不冒充 0**（备份/证书读不到就是 null + 原因）。
  6. **健康口径**：readiness 由「数据库可写 + 回执积压归零 + spool 未降级」决定，
     并暴露 backlog 与最近写入证明时间；spool 降级时立即不就绪。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, main, observability, operational, security  # noqa: E402
from server.routers import stats as stats_router  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402


class ErrorCodeClassifierTest(unittest.TestCase):
    """码表与优先级（不碰数据库）。"""

    def setUp(self) -> None:
        self.state = observability.RequestAudit(
            'rid-classify', '/v1/chat/completions', 'POST', 'openai', time.monotonic())
        self.token = observability.current.set(self.state)
        self.addCleanup(observability.current.reset, self.token)

    def test_upstream_code_wins_and_is_not_overwritten(self) -> None:
        self.assertEqual(observability.mark_error('11102', observability.ERROR_SOURCE_UPSTREAM),
                         '11102')
        self.state.terminal_state = 'upstream_error'
        self.state.http_status = 502
        # 网关分类晚到，不能覆盖上游业务码
        observability.mark_error('gateway.upstream.error')
        self.assertEqual(observability.classify_error(self.state), ('11102', 'upstream'))

    def test_gateway_classification_table(self) -> None:
        cases = [
            (('client_cancelled', None, 499), ('client.cancelled', 'client')),
            (('rejected', None, 401), ('gateway.auth.denied', 'gateway')),
            (('rejected', None, 403), ('gateway.auth.denied', 'gateway')),
            (('rejected', None, 429), ('gateway.rate_limited', 'gateway')),
            (('rejected', None, 503), ('gateway.capacity.rejected', 'gateway')),
            (('rejected', None, 413), ('gateway.request.too_large', 'gateway')),
            (('rejected', 'request_body', 400), ('gateway.request.body_error', 'gateway')),
            (('rejected', 'validation', 400), ('gateway.request.invalid', 'gateway')),
            (('protocol_incomplete', 'protocol', 502), ('gateway.protocol.incomplete', 'gateway')),
            (('transport_error', 'stream_read', 502), ('gateway.transport.error', 'gateway')),
            (('upstream_error', 'upstream', 502), ('gateway.upstream.error', 'gateway')),
            (('application_error', 'application', 500), ('gateway.application.error', 'gateway')),
            (('interrupted', 'application', 500), ('gateway.interrupted', 'gateway')),
            (('completed', None, 200), ('none', 'none')),
            ((None, None, 0), ('gateway.unknown', 'gateway')),
        ]
        for (terminal, stage, status), expected in cases:
            with self.subTest(terminal=terminal, status=status):
                self.assertEqual(
                    observability.classify_error(terminal_state=terminal,
                                                 error_stage=stage, http_status=status),
                    expected)

    def test_success_never_carries_an_error_code(self) -> None:
        observability.mark_error('11102', observability.ERROR_SOURCE_UPSTREAM)
        self.assertEqual(
            observability.classify_error(terminal_state='completed', http_status=200),
            ('none', 'none'), '中途抖动但最终成功的请求不该带错误码')

    def test_codes_cannot_carry_prompts_urls_or_credentials(self) -> None:
        for bad in ('https://api.example.com/v1/x', 'Bearer sk-abcdef',
                    '请检查你的提示词', 'has space', 'a' * 65, '', None, True):
            with self.subTest(value=bad):
                self.assertIsNone(observability.sanitize_error_code(bad))
        self.assertEqual(observability.sanitize_error_code('no_healthy_account'),
                         'no_healthy_account')
        self.assertEqual(observability.sanitize_error_code(11102), '11102')
        self.assertEqual(observability.sanitize_error_code('  gateway.auth.denied  '),
                         'gateway.auth.denied')

    def test_mark_error_ignores_unsafe_codes(self) -> None:
        self.assertIsNone(observability.mark_error('Bearer sk-live-123'))
        self.assertIsNone(self.state.error_code, '不安全的值不该落进 state')

    def test_traffic_class_python_and_sql_agree(self) -> None:
        cases = {'/healthz': 'probe', '/api/readyz': 'probe', '/metrics': 'probe',
                 '/api/logs': 'admin', '/v1/chat/completions': 'inference',
                 '/v2/chat/completions': 'inference', '/unknown': 'other'}
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(observability.traffic_class(path), expected)


class OutcomePersistenceTest(unittest.TestCase):
    """中间件把稳定码写进 `request_outcomes`（含补列幂等）。"""

    def setUp(self) -> None:
        isolate_database(self)
        self.client = TestClient(main.app)

    def _login(self) -> None:
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('TestPw123!')}], 'api_keys': []})
        response = self.client.post('/api/login',
                                    json={'username': 'admin', 'password': 'TestPw123!'},
                                    headers={'Origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.cookies.update(dict(response.cookies))

    def test_ensure_schema_is_idempotent(self) -> None:
        self.assertTrue(observability.ensure_outcome_schema())
        columns = {row[1] for row in db.query("PRAGMA table_info(request_outcomes)")}
        self.assertIn('error_code', columns)
        self.assertIn('error_source', columns)
        self.assertTrue(observability.ensure_outcome_schema(), '重复调用必须仍然成功')
        self.assertEqual(
            len([c for c in columns if c == 'error_code']), 1, '不允许重复补列')

    def test_unauthenticated_api_call_records_auth_error_code(self) -> None:
        self.client.get('/api/me')
        row = db.query_one('SELECT * FROM request_outcomes ORDER BY ts DESC LIMIT 1')
        self.assertIsNotNone(row)
        self.assertEqual(int(row['http_status']), 401)
        self.assertEqual(row['terminal_state'], 'rejected')
        self.assertEqual(row['error_code'], 'gateway.auth.denied')
        self.assertEqual(row['error_source'], 'gateway')

    def test_completed_request_records_none(self) -> None:
        self._login()
        self.client.get('/api/logs/outcomes')
        row = db.query_one("SELECT * FROM request_outcomes WHERE http_status=200 "
                           "ORDER BY ts DESC LIMIT 1")
        self.assertIsNotNone(row)
        self.assertEqual(row['terminal_state'], 'completed')
        self.assertEqual(row['error_code'], 'none')
        self.assertEqual(row['error_source'], 'none')

    def test_outcome_columns_never_hold_messages(self) -> None:
        self.client.get('/api/me')
        row = db.query_one('SELECT * FROM request_outcomes ORDER BY ts DESC LIMIT 1')
        for column in ('error_code', 'error_source'):
            value = row[column] or ''
            self.assertNotIn(' ', value)
            self.assertNotIn('/', value)
            self.assertNotIn('@', value)

    def test_gateway_marked_code_reaches_the_database(self) -> None:
        """端到端：网关在拒绝点标的码（`mark_error`）必须真的落进 outcome。

        这里用限流分支验证 —— 它同时具备「稳定的机器码」
        （`rate_limit_exceeded`，与响应体里的 code 同一个字符串）与「拒绝点
        紧邻 `_record`」两个特征，是接线是否断掉的最好探针。
        """
        from server import keysvc
        from server.routers import gateway
        created = keysvc.create_key(name='u21-rate', realm='cn')
        headers = {'Authorization': 'Bearer ' + created['key']}
        # 限流窗口 _rate 是模块级全局，而每个测试用独立 DB → key_id 会重复，
        # 前面测试留下的命中记录会让本测试的第一次请求就被 429（顺序依赖）。
        gateway._rate.clear()
        with mock.patch.object(gateway, 'RATE_MAX_PER_MIN', 1):
            first = self.client.get('/v1/models', headers=headers)
            second = self.client.get('/v1/models', headers=headers)
        self.assertIn(first.status_code, (200, 502), first.text)
        self.assertEqual(second.status_code, 429, second.text)
        row = db.query_one("SELECT * FROM request_outcomes WHERE endpoint = '/v1/models' "
                           "AND http_status = 429 ORDER BY ts DESC LIMIT 1")
        self.assertIsNotNone(row, '被限流的请求必须留下轨迹')
        self.assertEqual(row['error_code'], 'rate_limit_exceeded')
        self.assertEqual(row['error_source'], 'gateway')


class _SeededEndpointTest(unittest.TestCase):
    """共享夹具：种入轨迹 + 日志 + attempt，登录后供两个只读端点使用。"""

    def setUp(self) -> None:
        self.root = isolate_database(self)
        self.client = TestClient(main.app)
        self.assertTrue(observability.ensure_outcome_schema())
        now = int(time.time())
        self.now = now
        self.rows = [
            # (request_id, ts, endpoint, status, terminal, code, source, release, platform)
            ('rid-ok', now - 60, '/v1/chat/completions', 200, 'completed', 'none', 'none',
             'v-rel', 'workbuddy'),
            ('rid-rate', now - 120, '/v1/chat/completions', 429, 'rejected',
             'rate_limit_exceeded', 'gateway', 'v-rel', 'workbuddy'),
            ('rid-budget', now - 180, '/v1/chat/completions', 429, 'rejected',
             'budget_pending', 'gateway', 'v-rel', 'workbuddy'),
            ('rid-upstream', now - 240, '/v1/chat/completions', 502, 'upstream_error',
             '11102', 'upstream', 'v-rel', 'qoder'),
            ('rid-cancel', now - 300, '/v1/chat/completions', 499, 'client_cancelled',
             'client.cancelled', 'client', 'v-old', 'workbuddy'),
            ('rid-proto', now - 360, '/v1/chat/completions', 502, 'protocol_incomplete',
             'gateway.protocol.incomplete', 'gateway', 'v-old', 'workbuddy'),
            ('rid-probe', now - 420, '/api/readyz', 503, 'rejected',
             'gateway.capacity.rejected', 'gateway', 'v-rel', None),
        ]
        for (rid, ts, endpoint, status, terminal, code, source, release, platform) in self.rows:
            db.execute(
                'INSERT OR IGNORE INTO request_outcomes(request_id, ts, endpoint, method, '
                'protocol, http_status, terminal_state, error_stage, latency_ms, release_id, '
                'error_code, error_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid, ts, endpoint, 'POST', 'openai', status, terminal, None, 42,
                 release, code, source))
            db.add_request_log(ts=ts, key_id=1, ip='127.0.0.1', model='m',
                               mapped_model='m', status=status, prompt_tokens=1,
                               completion_tokens=1, latency_ms=42, stream=1,
                               realm='cn', request_id=rid, actual_platform=platform,
                               terminal_state=terminal, release_id=release)
        # rid-upstream 发生过 2 次 provider attempt
        for attempt in (1, 2):
            db.execute('INSERT OR IGNORE INTO attempt_events VALUES(?,?,?,?,?,?,?,?,?)',
                       ('rid-upstream', attempt, 'selected', now, 'acct', 'm', 'cn', 200, ''))

    def _login(self) -> None:
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('TestPw123!')}], 'api_keys': []})
        response = self.client.post('/api/login',
                                    json={'username': 'admin', 'password': 'TestPw123!'},
                                    headers={'Origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.cookies.update(dict(response.cookies))
        # 登录请求本身也是一条被追踪的轨迹（POST /api/login，U21 之后会带
        # error_code='none'）。它属于夹具噪声而不是被测对象，清掉它让计数断言确定。
        db.execute("DELETE FROM request_outcomes WHERE endpoint = '/api/login'")


class OutcomesEndpointTest(_SeededEndpointTest):

    def test_requires_auth(self) -> None:
        self.assertEqual(self.client.get('/api/logs/outcomes').status_code, 401)

    def test_lists_rows_with_attempt_count_and_platform(self) -> None:
        self._login()
        response = self.client.get('/api/logs/outcomes', params={'days': 1})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['error_code_available'])
        self.assertEqual(body['total'], len(self.rows))
        items = {item['request_id']: item for item in body['items']}
        self.assertEqual(items['rid-upstream']['attempt_count'], 2)
        self.assertEqual(items['rid-upstream']['platform'], 'qoder')
        self.assertEqual(items['rid-upstream']['error_code'], '11102')
        self.assertEqual(items['rid-upstream']['error_source'], 'upstream')
        self.assertEqual(items['rid-ok']['attempt_count'], 0)
        self.assertEqual(items['rid-ok']['traffic_class'], 'inference')
        self.assertEqual(items['rid-probe']['traffic_class'], 'probe')

    def test_filters_by_error_code_terminal_platform_release(self) -> None:
        self._login()
        cases = [
            ({'error_code': 'rate_limit_exceeded'}, {'rid-rate'}),
            ({'error_code': '11102'}, {'rid-upstream'}),
            ({'terminal_state': 'client_cancelled'}, {'rid-cancel'}),
            # 平台过滤：qoder/trae 必须用 `upstream_platform` —— `?platform=qoder`
            # 会被 main.py 的 QTG 平台路由中间件重写掉（/api/logs 在白名单里），
            # 根本到不了本端点。workbuddy 这类值两个参数名都有效。
            ({'upstream_platform': 'qoder'}, {'rid-upstream'}),
            ({'platform': 'workbuddy'},
             {'rid-ok', 'rid-rate', 'rid-budget', 'rid-cancel', 'rid-proto'}),
            ({'release_id': 'v-old'}, {'rid-cancel', 'rid-proto'}),
            ({'traffic_class': 'probe'}, {'rid-probe'}),
            ({'traffic_class': 'inference', 'terminal_state': 'upstream_error'}, {'rid-upstream'}),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                response = self.client.get('/api/logs/outcomes',
                                           params={**params, 'days': 1})
                self.assertEqual(response.status_code, 200, response.text)
                got = {item['request_id'] for item in response.json()['items']}
                self.assertEqual(got, expected)

    def test_rejects_unknown_filter_values_instead_of_silently_empty(self) -> None:
        self._login()
        for params in ({'error_code': 'https://evil/x'}, {'error_code': 'has space'},
                       {'terminal_state': 'exploded'}, {'traffic_class': 'nonsense'}):
            with self.subTest(params=params):
                response = self.client.get('/api/logs/outcomes', params=params)
                self.assertEqual(response.status_code, 400, response.text)

    def test_time_window_excludes_old_rows(self) -> None:
        self._login()
        body = self.client.get('/api/logs/outcomes',
                               params={'since': self.now - 200}).json()
        got = {item['request_id'] for item in body['items']}
        self.assertEqual(got, {'rid-ok', 'rid-rate', 'rid-budget'})


class OutcomesStatsTest(_SeededEndpointTest):

    def setUp(self) -> None:
        super().setUp()
        self._backup_file = pathlib.Path(self.root) / 'backup-status.json'
        patcher = mock.patch.object(config, 'BACKUP_STATUS_FILE', self._backup_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        cert_files = mock.patch.object(stats_router, 'CERT_EVIDENCE_FILES', ())
        cert_files.start()
        self.addCleanup(cert_files.stop)
        cert_dirs = mock.patch.object(stats_router, 'CERT_LIVE_DIRS', ())
        cert_dirs.start()
        self.addCleanup(cert_dirs.stop)

    def test_classes_and_error_code_top(self) -> None:
        self._login()
        response = self.client.get('/api/stats/outcomes', params={'days': 1})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['requests'], len(self.rows))
        classes = body['classes']
        self.assertEqual(classes['completed'], 1)
        self.assertEqual(classes['cancelled'], 1)
        self.assertEqual(classes['protocol_incomplete'], 1)
        self.assertEqual(classes['failed'], 4)
        self.assertEqual(classes['unknown'], 0)
        codes = {row['error_code']: row for row in body['error_code_top']}
        # Top-N 只列真正的错因：`none`（成功）不算错因，否则会把真实错因挤下去。
        self.assertNotIn('none', codes)
        self.assertEqual(codes['11102']['error_source'], 'upstream')
        self.assertEqual(codes['11102']['count'], 1)
        self.assertIn('rate_limit_exceeded', codes)
        # 覆盖率：全部行都有码 → without_code = 0
        self.assertEqual(body['error_code_coverage']['without_code'], 0)
        self.assertEqual(body['error_code_coverage']['ratio'], 1.0)
        self.assertEqual({row['release_id'] for row in body['by_release']},
                         {'v-rel', 'v-old'})

    def test_missing_evidence_is_unknown_not_zero(self) -> None:
        self._login()
        body = self.client.get('/api/stats/outcomes', params={'days': 1}).json()
        backup = body['backup']
        self.assertEqual(backup['status'], 'unknown')
        self.assertIsNone(backup['age_seconds'], '缺失证据不能写成 0 秒')
        self.assertTrue(backup['reason'])
        certificate = body['certificate']
        self.assertEqual(certificate['status'], 'unknown')
        self.assertIsNone(certificate['days_left'], '缺失证据不能写成 0 天')
        self.assertTrue(certificate['reason'])

    def test_backup_and_certificate_evidence_are_read_when_present(self) -> None:
        self._backup_file.write_text(json.dumps(
            {'schema': 1, 'created_at': self.now - 3600, 'bytes': 1024,
             'restore_check': 'ok', 'offsite': False}), encoding='utf-8')
        cert_file = pathlib.Path(self.root) / 'cert-status.json'
        cert_file.write_text(json.dumps({'domain': 'example.test',
                                         'not_after': self.now + 60 * 86400}),
                             encoding='utf-8')
        with mock.patch.object(stats_router, 'CERT_EVIDENCE_FILES', (cert_file,)):
            self._login()
            body = self.client.get('/api/stats/outcomes', params={'days': 1}).json()
        self.assertEqual(body['backup']['status'], 'ok')
        self.assertGreaterEqual(body['backup']['age_seconds'], 3500)
        self.assertEqual(body['certificate']['status'], 'ok')
        self.assertIn(body['certificate']['days_left'], (59, 60))
        self.assertEqual(body['certificate']['domain'], 'example.test')

    def test_stale_backup_is_reported_as_stale(self) -> None:
        self._backup_file.write_text(json.dumps(
            {'created_at': self.now - 3 * 86400, 'bytes': 1024, 'restore_check': 'ok'}),
            encoding='utf-8')
        self._login()
        body = self.client.get('/api/stats/outcomes', params={'days': 1}).json()
        self.assertEqual(body['backup']['status'], 'stale')
        self.assertGreater(body['backup']['age_seconds'], 2 * 86400)

    def test_historical_rows_without_code_are_reported_as_uncoded(self) -> None:
        db.execute("INSERT INTO request_outcomes(request_id, ts, endpoint, method, protocol, "
                   "http_status, terminal_state, latency_ms, release_id) "
                   "VALUES('rid-legacy', ?, '/v1/chat/completions', 'POST', 'openai', 502, "
                   "'upstream_error', 10, 'v-legacy')", (self.now - 30,))
        self._login()
        body = self.client.get('/api/stats/outcomes', params={'days': 1}).json()
        coverage = body['error_code_coverage']
        self.assertEqual(coverage['without_code'], 1)
        self.assertLess(coverage['ratio'], 1.0)
        self.assertIn('不是', coverage['note'])


class ReadinessBasisTest(unittest.TestCase):
    """健康口径：数据库可写 + 回执积压 + spool 状态，并暴露写入证明。"""

    def setUp(self) -> None:
        self.root = isolate_database(self)
        self._orig_healthy = operational.ledger_write_healthy
        self._orig_spool_failed = operational.ledger_spool_failed
        self._orig_streak = operational._ledger_replay_streak
        operational.ledger_write_healthy = True
        operational.ledger_spool_failed = False
        operational._ledger_replay_streak = 0
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        operational.ledger_write_healthy = self._orig_healthy
        operational.ledger_spool_failed = self._orig_spool_failed
        operational._ledger_replay_streak = self._orig_streak

    def test_write_proof_is_unknown_before_the_first_probe(self) -> None:
        proof = operational._write_proof()
        self.assertIsNone(proof['ts'])
        self.assertIsNone(proof['age_seconds'])
        self.assertIn('尚无写入证明', proof['reason'])

    def test_readiness_exposes_backlog_and_write_proof(self) -> None:
        result = operational.readiness()
        self.assertTrue(result['ok'])
        self.assertEqual(result['ledger_backlog'], 0)
        self.assertEqual(result['ledger_spool'], 'ok')
        proof = result['write_proof']
        self.assertIsInstance(proof['ts'], int)
        self.assertLessEqual(proof['age_seconds'], 5)
        self.assertIn('ledger_replay_streak', result)
        self.assertGreaterEqual(result['ledger_recover_streak_required'], 1)
        self.assertIn('ledger_backlog==0', result['recovery_basis'])

    def test_spool_failure_makes_readiness_fail(self) -> None:
        operational.ledger_spool_failed = True
        result = operational.readiness()
        self.assertFalse(result['ok'])
        self.assertFalse(result['ledger_write'])
        self.assertEqual(result['ledger_spool'], 'failed')

    def test_ledger_write_flag_failure_makes_readiness_fail(self) -> None:
        operational.ledger_write_healthy = False
        self.assertFalse(operational.readiness()['ok'])

    def test_public_readyz_still_returns_one_bit(self) -> None:
        client = TestClient(main.app)
        response = client.get('/api/readyz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True},
                         '公开就绪端点只回一位，细节走 /api/operations/health')


if __name__ == '__main__':
    unittest.main()
