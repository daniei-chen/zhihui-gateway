"""K22 unknown variables, safe diagnostics, actual authorization and startup order."""
import json
import os
from unittest.mock import patch

from fastapi.testclient import TestClient
from identity_support import IdentityTestBase
from qtg.configcontract import ConfigurationError, validate_environment
from qtg.main import create_app


class ConfigurationTest(IdentityTestBase):
    def test_unknown_variable_and_invalid_units_fail_without_echoing_values(self):
        for name,value in [('QTG_PROXY_TIMOUT_SEC','secret-sentinel'),
                           ('QTG_PROXY_TIMEOUT_SEC','30ms'),('QTG_PROXY_TIMEOUT_SEC','nan'),
                           ('QTG_AUTHZ_ENFORCE','perhaps'),('QTG_DATA_DIR','./relative')]:
            with self.subTest(name=name), self.assertRaises(ConfigurationError) as caught:
                validate_environment({name:value})
            self.assertNotIn(value,str(caught.exception))

    def test_startup_rejects_unknown_before_runtime_initialization(self):
        app = create_app(self.rt.settings)
        with patch.dict(os.environ,{'QTG_AUTHZ_ENFORC':'not-a-secret'}), \
             patch('qtg.main.Runtime',side_effect=AssertionError('runtime side effect')):
            with self.assertRaises(ConfigurationError):
                with TestClient(app):pass

    def test_snapshot_requires_admin_and_keeps_authoritative_active_flags(self):
        for role in ('viewer','readonly'):
            r = self.client.get('/api/v1/system/configuration',headers={
                'X-QTG-Identity':self.signed_identity(role=role,scopes=['read'])})
            self.assertEqual(r.status_code,403,r.text)
        result = self.client.get('/api/v1/system/configuration',headers=self.auth)
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()['active']['authz_enforce'])
        row=result.json()['configuration']['settings']['QTG_DATA_DIR']
        self.assertEqual(row['value'],self.rt.settings.data_dir)
        self.assertEqual(row['source'],'explicit_settings_object')
        self.assertEqual(result.headers['cache-control'],'no-store')

    def test_secret_fields_and_url_credentials_are_not_in_snapshot(self):
        result = validate_environment({'QTG_ALLOWED_ORIGINS':'https://host.example',
            'QTG_QODER_BASE':'http://secret-user:secret-pw@127.0.0.1:8080/private-key?q=secret',
            'QTG_ADMIN_TOKEN_FILE':'/private/secret-file-path'})
        blob = json.dumps(result)
        for secret in ('secret-user','secret-pw','private-key','q=secret','secret-file-path'):
            self.assertNotIn(secret,blob)

    # ── N34：读取模式必须在启动期校验，不能静默退回 sqlite ──────────────
    def test_read_mode_rejects_unknown_value_at_startup(self):
        """非法 QTG_WB_READ_MODE 必须启动即拒。

        修复前只有 WorkBuddyAdapter.read_mode 运行期兜底（警告 + 退回 sqlite），
        运维把 http 拼错成 htttp 时服务照常启动、照常读本地 SQL，从外部看不出
        任何异常。schema 登记 choices 后行为变为启动失败。
        """
        with self.assertRaises(ConfigurationError) as caught:
            validate_environment({'QTG_WB_READ_MODE': 'postgres'})
        self.assertEqual(caught.exception.errors[0]['code'], 'invalid_choice')
        # 快照保留运维实际写的字面值，比较按小写；适配器读取时也统一 lower()。
        for good in ('sqlite', 'http', 'HTTP'):
            row = validate_environment({'QTG_WB_READ_MODE': good})['settings']['QTG_WB_READ_MODE']
            self.assertEqual(str(row['value']).lower(), good.lower())

    def test_every_registered_choice_is_lowercase(self):
        """configcontract 用小写比较 choices，大写候选永远匹配不到。"""
        from qtg.configcontract import load_schema
        bad = {name: spec['choices'] for name, spec in load_schema()['settings'].items()
               if spec.get('choices') and any(c != c.lower() for c in spec['choices'])}
        self.assertEqual(bad, {}, f'choices 必须全小写：{bad}')
