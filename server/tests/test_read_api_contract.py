"""K26 读 API 版本探测 + settings panel + usage 族端点（Manager 侧）。

为什么值得有：这些端点是 QTG 迁移的**契约入口**。它们错了不会让现有页面报错，
只会让 QTG 在 http 模式下拿到错误的数据形状，表现为"统计页少一半字段"这类难查问题。

钉住的不变量：
  1. `/api/read-api/version` 必须声明 version / capabilities / 口径字段，
     且**能力表覆盖 QTG 实际会用的族**（v2 起含 usage）；
  2. 端点都要**鉴权**（未登录 401），不能成为匿名信息泄露点；
  3. `/api/settings/panel` 在 settings 表为空时返回 `{}`（"没配置"），
     但表结构不可读时返回 5xx（"读不到"）——两者语义必须分开；
  4. R1-A（v2）：`/api/read-api/usage-matrix` 与 `/usage-totals` 的口径必须与
     QTG 现有 sqlite 读法逐位一致（原始行、无上界、失败数含 status=0），
     参数形态错了显式 400 而不是回落今天。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import db, main, security  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402


class ReadApiContractTest(unittest.TestCase):
    def setUp(self) -> None:
        isolate_database(self)
        self.client = TestClient(main.app)

    def _login(self) -> None:
        # 用已知口令写入用户表（测试环境的管理员密码是随机生成的，不能硬编码）
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('TestPw123!')}], 'api_keys': []})
        r = self.client.post('/api/login',
                             json={'username': 'admin', 'password': 'TestPw123!'},
                             headers={'Origin': 'http://testserver'})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.cookies.update(dict(r.cookies))

    def test_version_requires_auth(self) -> None:
        r = self.client.get('/api/read-api/version')
        self.assertEqual(r.status_code, 401, '读取契约探测不得匿名可用')

    def test_panel_requires_auth(self) -> None:
        r = self.client.get('/api/settings/panel')
        self.assertEqual(r.status_code, 401)

    def test_version_shape(self) -> None:
        self._login()
        r = self.client.get('/api/read-api/version')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # 契约版本（R1-A 升到 2：新增 usage 族）
        self.assertEqual(body['version'], 2)
        # 能力表必须覆盖 QTG 会用的族
        caps = body['capabilities']
        for family in ('stats', 'keys', 'logs', 'tasks', 'checkin', 'audit',
                       'accounts', 'models', 'settings', 'usage'):
            self.assertIn(family, caps, f'能力表缺 {family}')
            self.assertTrue(caps[family], f'能力表说 {family} 不可用')
        # 口径声明（调用方据此归一化，不能靠猜）
        self.assertEqual(body['credit_unit'], 'float')
        self.assertEqual(body['timestamp_unit'], 'epoch_seconds')
        self.assertIn('timezone', body)
        self.assertEqual(body['pagination']['style'], 'offset_limit')
        # 端点清单要能对上能力表里的族
        for family in caps:
            if caps[family]:
                self.assertIn(family, body['endpoints'], f'{family} 声明可用但没给端点')

    def test_health_endpoint(self) -> None:
        self._login()
        r = self.client.get('/api/read-api/health')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['version'], 2)

    def test_panel_empty_table_returns_empty_dict(self) -> None:
        """表存在但没有行 -> `{}`（表示"没配置"，不是"读不到"）。"""
        self._login()
        r = self.client.get('/api/settings/panel')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {})

    # ── v2：usage 族（R1-A）────────────────────────────────
    def _seed_usage(self) -> None:
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens,'
            ' completion_tokens, credit, realm) VALUES (?,?,?,?,?,?,?,?)',
            ('2020-01-01', 1, 'old-model', 100, 1000, 500, 10.0, 'cn'))
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens,'
            ' completion_tokens, credit, realm) VALUES (?,?,?,?,?,?,?,?)',
            ('2099-01-01', 1, 'new-model', 5, 50, 5, 0.5, 'cn'))

    def test_usage_matrix_returns_raw_rows(self) -> None:
        """原始行（不在这里聚合）：逐行返回，QTG 两种读模式共用同一段映射。"""
        self._login()
        self._seed_usage()
        r = self.client.get('/api/read-api/usage-matrix',
                            params={'since': '2020-01-01'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['version'], 2)
        self.assertEqual(body['since'], '2020-01-01')
        self.assertEqual([row['day'] for row in body['rows']],
                         ['2020-01-01', '2099-01-01'])
        row = body['rows'][0]
        for field in ('day', 'model', 'requests', 'prompt_tokens',
                      'completion_tokens', 'credit', 'realm'):
            self.assertIn(field, row, f'缺字段 {field}')
        self.assertEqual(row['model'], 'old-model')
        self.assertEqual(row['requests'], 100)
        self.assertIn('failures', body)

    def test_usage_matrix_since_has_no_upper_bound(self) -> None:
        """与 QTG 现有读法一致：只过滤 `day >= since`，没有上界。"""
        self._login()
        self._seed_usage()
        r = self.client.get('/api/read-api/usage-matrix',
                            params={'since': '2099-01-01'})
        self.assertEqual([row['day'] for row in r.json()['rows']], ['2099-01-01'])

    def test_usage_matrix_rejects_malformed_since(self) -> None:
        """形态错了显式 400——静默回落"今天"会让对账看起来一致而实际查了别的区间。"""
        self._login()
        for bad in ('2026/10/01', '20261001', '2026-10-1', 'today'):
            with self.subTest(since=bad):
                r = self.client.get('/api/read-api/usage-matrix',
                                    params={'since': bad})
                self.assertEqual(r.status_code, 400, f'{bad}: {r.text}')

    def test_usage_matrix_requires_since(self) -> None:
        self._login()
        r = self.client.get('/api/read-api/usage-matrix')
        self.assertEqual(r.status_code, 422, r.text)

    def test_usage_totals_shape(self) -> None:
        self._login()
        self._seed_usage()
        r = self.client.get('/api/read-api/usage-totals')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['version'], 2)
        self.assertEqual(body['requests'], 105)
        self.assertEqual(body['prompt_tokens'], 1050)
        self.assertEqual(body['completion_tokens'], 505)
        self.assertAlmostEqual(body['credits'], 10.5)

    def test_usage_endpoints_require_auth(self) -> None:
        for path in ('/api/read-api/usage-matrix', '/api/read-api/usage-totals'):
            with self.subTest(path=path):
                r = self.client.get(path, params={'since': '2020-01-01'})
                self.assertEqual(r.status_code, 401, f'{path}: {r.text}')


if __name__ == '__main__':
    unittest.main()
