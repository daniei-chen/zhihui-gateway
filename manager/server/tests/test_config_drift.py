"""U16 回归：配置四源对照、默认值对齐、敏感字段与跨字段校验。

背景（2026-10-28 台账 U16，P1）：`server/config-schema.json` 是唯一配置模型
（未登记的 `WB_*` 会让服务**启动失败**），但「schema 默认值 / 源码默认值 /
systemd unit+drop-in 的生效值 / 进程实际值」四者会漂移。实测到的样本：

  · `WB_GATEWAY_MAX_CONCURRENCY` / `WB_GATEWAY_LARGE_MAX_CONCURRENCY` /
    `WB_KEY_CONCURRENCY`：schema 默认写 0（看着像"不限"），代码默认 8/2/4 ——
    而 0 在 `admit_key` / `active >= maximum` 里实际是"**拒绝一切**"；
  · `WB2API_CONTAINER`：schema 说 `workbuddy2api`，代码与 unit 都是
    `zhihui-workbuddy`（生产容器名）；
  · `WB_BACKUP_STATUS_FILE`：schema 说 `/root/backups/...`，写方与 `config.py`
    都用 `/var/lib/zhihui-gateway/backup-status.json`；
  · `WB_LOG_LEVEL`：`main.py` 在读，却从未登记 —— 运维一设就让服务起不来；
  · `WB_GATEWAY_RATE_PER_MIN` / `WB_MANAGER_HOST`：在 unit 与 drop-in 里各写一份
    （谁生效取决于文件名排序）。

这里锁定五件事：
  1. 源码默认值必须等于 schema 默认值（唯一例外进 `config.SCHEMA_DRIFT_ALLOWED`）；
  2. 表达式型默认值按**文本**比较（`str(ROOT / 'data')` 这类）；
  3. 代码读到的每个 `WB_*` 都必须在 schema 里登记，且 schema 不遗漏代码默认值；
  4. 敏感字段不出现在任何输出里（只给"是否设置 + 指纹"），未登记变量的值也不显示；
  5. `/api/system/config` 只给管理员，能标出「多文件重复定义」「改了没重启」
     「敏感值明文进 unit」，且 `graceful >= drain` 的交叉校验仍然生效。
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, main, security  # noqa: E402
from server import configcontract  # noqa: E402
from server.configcontract import ConfigurationError, validate_environment  # noqa: E402
from server.routers import system as system_router  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402

SERVER_DIR = pathlib.Path(config.__file__).resolve().parent


class SourceDefaultDriftTest(unittest.TestCase):
    """schema 默认值 与 源码默认值 必须一致（这是「唯一配置模型」的前提）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.specs = configcontract.load_schema()['settings']
        cls.source = configcontract.parse_source_defaults(SERVER_DIR, refresh=True)

    def test_every_literal_source_default_matches_schema(self) -> None:
        allowed = dict(config.SCHEMA_DRIFT_ALLOWED)
        problems = []
        for name, spec in self.specs.items():
            source = self.source.get(name) or {}
            if source.get('default') is None or 'default' not in spec:
                continue
            if (configcontract._norm(source['default'])
                    != configcontract._norm(spec['default'])) and name not in allowed:
                problems.append({'name': name, 'source': source['default'],
                                 'schema': spec['default'],
                                 'sites': source.get('sites')})
        self.assertEqual(problems, [], '源码默认值与 schema 不一致（要么改 schema，'
                                       '要么把例外写进 config.SCHEMA_DRIFT_ALLOWED）')

    def test_expression_defaults_match_schema_text(self) -> None:
        for name, spec in self.specs.items():
            expression = spec.get('default_expression')
            if not expression:
                continue
            source = self.source.get(name) or {}
            self.assertIsNotNone(source.get('expression'),
                                 f'{name} 的表达式默认值没被识别')
            self.assertEqual(configcontract._norm_expression(source['expression']),
                             configcontract._norm_expression(expression), name)

    def test_allow_list_has_no_stale_entries(self) -> None:
        for name, reason in dict(config.SCHEMA_DRIFT_ALLOWED).items():
            self.assertTrue(str(reason).strip(), f'{name} 的允许差异必须写清理由')
            source = self.source.get(name) or {}
            spec = self.specs.get(name) or {}
            differs = (source.get('default') is not None and 'default' in spec
                       and configcontract._norm(source['default'])
                       != configcontract._norm(spec['default']))
            self.assertTrue(differs, f'{name} 在允许清单里但其实已经没有差异，应删除')

    def test_every_env_read_is_registered_in_schema(self) -> None:
        unregistered = sorted(set(self.source) - set(self.specs))
        self.assertEqual(unregistered, [],
                         '这些变量被代码读取却没登记 schema：一旦运维设置它们，'
                         'validate_environment 会以 unknown_variable 让服务启动失败')

    def test_schema_does_not_omit_code_defaults(self) -> None:
        missing = []
        for name, source in self.source.items():
            spec = self.specs.get(name)
            if spec is None or 'default' in spec or 'default_expression' in spec:
                continue
            if source.get('default') is not None:
                missing.append({'name': name, 'source': source['default'],
                                'sites': source.get('sites')})
        self.assertEqual(missing, [], 'schema 没写 default，但代码里有默认值 —— '
                                      '读 schema 的人会以为「必须显式设置」')

    def test_zero_concurrency_defaults_are_not_pretend_unlimited(self) -> None:
        """0 在 admission 里是「拒绝一切」，所以 schema 默认值不能是 0。"""
        for name, expected in (('WB_GATEWAY_MAX_CONCURRENCY', 8),
                               ('WB_GATEWAY_LARGE_MAX_CONCURRENCY', 2),
                               ('WB_KEY_CONCURRENCY', 4)):
            self.assertEqual(self.specs[name]['default'], expected, name)
            self.assertGreater(self.specs[name]['default'], 0, name)


class SensitiveRedactionTest(unittest.TestCase):
    SENTINEL = 'u16-secret-sentinel-8f2a'

    def test_sensitive_values_are_never_returned(self) -> None:
        specs = configcontract.load_schema()['settings']
        sensitive = sorted(name for name, spec in specs.items() if spec.get('sensitive'))
        self.assertGreaterEqual(len(sensitive), 5)
        env = {name: self.SENTINEL for name in sensitive}
        result = configcontract.compare_sources(specs, process_env=env, declared_env=env)
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.SENTINEL, blob, '敏感值绝不允许出现在输出里')
        rows = {row['name']: row for row in result['rows']}
        for name in sensitive:
            row = rows[name]
            self.assertTrue(row['process']['redacted'], name)
            self.assertTrue(row['process']['set'], name)
            self.assertEqual(len(row['process']['fingerprint']), 8, name)
            # 同一次采集内跨源可比（unit 与进程是同一个值 → 指纹相同）
            self.assertEqual(row['process']['fingerprint'],
                             row['declared']['fingerprint'], name)
            self.assertIn('sensitive_in_unit', row['flags'], name)

    def test_unregistered_values_are_hidden(self) -> None:
        result = configcontract.compare_sources(
            {}, process_env={'WB_UNKNOWN_THING': self.SENTINEL},
            declared_env={'WB_UNKNOWN_THING': self.SENTINEL})
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.SENTINEL, blob, '未登记变量不知道是否敏感 → 值一律不显示')
        row = result['rows'][0]
        self.assertIn('unregistered', row['flags'])
        self.assertEqual(row['risk'], 'high')
        self.assertTrue(row['process']['unregistered'])

    def test_schema_snapshot_redacts_url_credentials(self) -> None:
        rows = validate_environment(
            {'WB_HTTP_PROXY': 'http://user:pass@127.0.0.1:9000/private?q=1'})['settings']
        self.assertEqual(rows['WB_HTTP_PROXY']['value'], 'http://127.0.0.1:9000')


class CrossCheckTest(unittest.TestCase):
    def test_graceful_below_drain_is_still_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            validate_environment({'WB_DRAIN_TIMEOUT_SEC': '25',
                                  'WB_GRACEFUL_SHUTDOWN_S': '3'})

    def test_cross_check_reports_both_directions(self) -> None:
        specs = configcontract.load_schema()['settings']
        ok = configcontract.cross_checks(
            specs, {'WB_GRACEFUL_SHUTDOWN_S': '650', 'WB_DRAIN_TIMEOUT_SEC': '75'})
        self.assertTrue(ok[0]['ok'])
        self.assertEqual(ok[0]['check'], 'graceful_shutdown_vs_drain')
        bad = configcontract.cross_checks(
            specs, {'WB_GRACEFUL_SHUTDOWN_S': '3', 'WB_DRAIN_TIMEOUT_SEC': '75'})
        self.assertFalse(bad[0]['ok'])
        self.assertIn('SIGKILL', bad[0]['detail'])


class SystemConfigEndpointTest(unittest.TestCase):
    """`GET /api/system/config`：只读、仅管理员、四源对照 + 漂移标记。"""

    def setUp(self) -> None:
        self.root = isolate_database(self)
        self.client = TestClient(main.app)
        self.unit_dir = pathlib.Path(self.root) / 'systemd'
        self.unit_dir.mkdir()
        original = system_router.SYSTEMD_UNIT_DIR
        system_router.SYSTEMD_UNIT_DIR = self.unit_dir
        self.addCleanup(setattr, system_router, 'SYSTEMD_UNIT_DIR', original)

    def _login(self) -> None:
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('TestPw123!')}], 'api_keys': []})
        response = self.client.post('/api/login',
                                    json={'username': 'admin', 'password': 'TestPw123!'},
                                    headers={'Origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.cookies.update(dict(response.cookies))

    def _units(self, files: dict) -> None:
        for name, text in files.items():
            (self.unit_dir / name).write_text(text, encoding='utf-8')

    def _dropins(self, files: dict) -> None:
        dropin = self.unit_dir / 'workbuddy-web.service.d'
        dropin.mkdir(exist_ok=True)
        for name, text in files.items():
            (dropin / name).write_text(text, encoding='utf-8')

    def test_requires_admin(self) -> None:
        self.assertEqual(self.client.get('/api/system/config').status_code, 401)

    def test_reports_four_sources_without_unit_files(self) -> None:
        self._login()
        response = self.client.get('/api/system/config')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(set(body['sources']),
                         {'schema', 'source_default', 'declared', 'process'})
        self.assertFalse(body['sources']['declared']['available'],
                         '没有 unit 文件时要如实说不可读，不能假装"没有覆盖"')
        self.assertEqual(body['summary']['settings'],
                         len(configcontract.load_schema()['settings']))
        names = {row['name'] for row in body['settings']}
        self.assertIn('WB_DRAIN_TIMEOUT_SEC', names)
        self.assertIn('WB_LOG_LEVEL', names)
        self.assertEqual(body['cross_checks'][0]['check'], 'graceful_shutdown_vs_drain')
        self.assertEqual(body['policy']['order'][0], 'config-schema.json')

    def test_detects_duplicate_definition_and_unrestarted_change(self) -> None:
        self._units({'workbuddy-web.service':
                     '[Service]\nEnvironment=WB_UPSTREAM_TIMEOUT=600 WB_DRAIN_TIMEOUT_SEC=999\n'})
        self._dropins({'zz-timeout.conf': '[Service]\nEnvironment=WB_UPSTREAM_TIMEOUT=900\n'})
        self._login()
        # WB_UPSTREAM_TIMEOUT 进程里也是 900（与 unit 一致）→ 只剩"多文件重复定义"；
        # WB_DRAIN_TIMEOUT_SEC 进程里是 75、unit 声明 999 → "改了没重启"。
        with mock.patch.dict('os.environ', {'WB_DRAIN_TIMEOUT_SEC': '75',
                                            'WB_UPSTREAM_TIMEOUT': '900'}, clear=False):
            body = self.client.get('/api/system/config').json()
        rows = {row['name']: row for row in body['settings']}
        duplicate = rows['WB_UPSTREAM_TIMEOUT']
        self.assertIn('duplicate_in_units', duplicate['flags'])
        self.assertEqual(duplicate['risk'], 'medium')
        self.assertEqual(len(body['duplicate_keys']['WB_UPSTREAM_TIMEOUT']), 2)
        self.assertEqual(duplicate['declared'], '900', '后加载的 drop-in 必须覆盖 unit')
        # unit 声明 999、进程实际 75 → 改了没重启
        stale = rows['WB_DRAIN_TIMEOUT_SEC']
        self.assertIn('declared_vs_process', stale['flags'])
        self.assertEqual(stale['risk'], 'high')
        self.assertEqual(stale['declared'], '999')
        self.assertEqual(stale['process'], '75')

    def test_hides_sensitive_and_unregistered_unit_values(self) -> None:
        sentinel = 'u16-unit-secret-sentinel'
        self._units({'workbuddy-web.service':
                     '[Service]\nEnvironment=WB_ADMIN_PASSWORD=%s WB_NOT_IN_SCHEMA_AT_ALL=%s\n'
                     % (sentinel, sentinel)})
        self._login()
        response = self.client.get('/api/system/config')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(sentinel, response.text, 'unit 里的敏感值绝不允许回显')
        body = response.json()
        rows = {row['name']: row for row in body['settings']}
        self.assertTrue(rows['WB_ADMIN_PASSWORD']['declared']['redacted'])
        self.assertIn('sensitive_in_unit', rows['WB_ADMIN_PASSWORD']['flags'])
        self.assertEqual(rows['WB_ADMIN_PASSWORD']['risk'], 'high')
        self.assertIn('WB_NOT_IN_SCHEMA_AT_ALL', body['summary']['unregistered'])
        self.assertTrue(rows['WB_NOT_IN_SCHEMA_AT_ALL']['declared']['unregistered'])

    def test_refresh_parameter_rescans_source_defaults(self) -> None:
        self._login()
        first = self.client.get('/api/system/config').json()
        second = self.client.get('/api/system/config', params={'refresh': 'true'}).json()
        self.assertEqual(first['summary']['settings'], second['summary']['settings'])
        self.assertEqual(first['sources']['source_default']['modules_with_defaults'],
                         second['sources']['source_default']['modules_with_defaults'])


if __name__ == '__main__':
    unittest.main()
