"""K22 validates actual service environment before opening data or starting work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from server.configcontract import ConfigurationError, validate_environment


class ConfigurationContractTest(unittest.TestCase):
    def test_documented_session_day_clamp_is_visible_without_rejecting_legacy_configuration(self):
        for raw in ('0','-1','-30'):
            row=validate_environment({'WB_SESSION_DAYS':raw})['settings']['WB_SESSION_DAYS']
            self.assertEqual(row['value'],1)
            self.assertEqual(row['requested_value'],int(raw))
            self.assertTrue(row['normalization_applied'])
        self.assertFalse(validate_environment({'WB_SESSION_DAYS':'7'})['settings']['WB_SESSION_DAYS']['normalization_applied'])
        with self.assertRaises(ConfigurationError):
            validate_environment({'WB_SESSION_DAYS':'1day'})

    def test_fresh_import_rejects_bad_numeric_environment_without_side_effects_or_value_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'must-not-be-created'
            environment = {**os.environ, 'WB_DATA_DIR':str(target),
                           'WB_GATEWAY_RATE_PER_MIN':'private-malformed-numeric-sentinel'}
            result = subprocess.run([sys.executable, '-c', 'from server import config'],
                env=environment,capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('WB_GATEWAY_RATE_PER_MIN',result.stderr)
            self.assertNotIn('private-malformed-numeric-sentinel',result.stdout+result.stderr)
            self.assertFalse(target.exists())

    def test_actual_units_and_types_are_not_silently_coerced_to_defaults(self):
        for name,value in [('WB_UPSTREAM_TIMEOUT','10ms'),('WB_CONNECT_TIMEOUT_S','nan'),
                           ('WB_TOTAL_TIMEOUT_S','inf'),('WB_HTTP_MAX_POOLS','0'),
                           ('WB_GATEWAY_RATE_PER_MIN','-1'),('WB_TRUST_PROXY','true'),
                           ('WB_LOCAL_IMPORT','perhaps'),('WB_TRUSTED_PROXY_CIDRS','not-an-ip'),
                           ('WB2API_BASE','file:///private'),('WB_HTTP_PROXY','http://bad:99999')]:
            with self.subTest(name=name), self.assertRaises(ConfigurationError) as caught:
                validate_environment({name:value})
            self.assertIn(name, str(caught.exception))

    def test_current_legacy_values_and_unrelated_process_variables_remain_valid(self):
        result = validate_environment({'WB_UPSTREAM_TIMEOUT':'600','WB_GATEWAY_RATE_PER_MIN':'600',
            'WB_MANAGER_PORT':'0','WB_LOCAL_IMPORT':'off','PATH':'unrelated'})
        rows = result['settings']
        self.assertEqual(rows['WB_UPSTREAM_TIMEOUT']['value'],600)
        self.assertEqual(rows['WB_UPSTREAM_TIMEOUT']['unit'],'seconds')
        self.assertEqual(rows['WB_GATEWAY_RATE_PER_MIN']['unit'],'per_minute')
        self.assertEqual(rows['WB_MANAGER_PORT']['value'],0)
        self.assertNotIn('PATH',rows)

    def test_graceful_environment_and_actual_launcher_cannot_undercut_drain(self):
        with self.assertRaises(ConfigurationError):
            validate_environment({'WB_DRAIN_TIMEOUT_SEC':'25','WB_GRACEFUL_SHUTDOWN_S':'3'})
        from server.serve import main
        with patch('server.config.DRAIN_TIMEOUT_SEC',25), self.assertRaises(SystemExit) as caught:
            main(['--timeout-graceful-shutdown','3'])
        self.assertEqual(caught.exception.code,2)

    def test_managed_launch_resolves_cli_before_environment_without_binding_a_socket(self):
        from server import config
        from server.serve import main
        original=config.LAUNCH_ARGUMENTS
        try:
            with patch.dict(os.environ,{'WB_MANAGER_PORT':'7777','WB_MANAGER_HOST':'127.0.0.2'}), \
                 patch('server.config.PORT',7777),patch('server.config.HOST','127.0.0.2'), \
                 patch('server.serve.DrainServer') as server,patch('server.serve.signal.signal'):
                main(['--port','8888'])
                self.assertEqual(config.LAUNCH_ARGUMENTS['port'],8888)
                self.assertEqual(config.LAUNCH_ARGUMENTS['host'],'127.0.0.2')
                server.return_value.run.assert_called_once()
        finally:config.LAUNCH_ARGUMENTS=original

    def test_unknown_variable_has_no_value_in_error(self):
        with self.assertRaises(ConfigurationError) as caught:
            validate_environment({'WB_UPSTREM_TIMEOUT':'never-echo-this-value'})
        self.assertEqual(caught.exception.errors,[{'name':'WB_UPSTREM_TIMEOUT','code':'unknown_variable'}])
        self.assertNotIn('never-echo',str(caught.exception))

    def test_snapshot_redacts_credentials_in_urls_and_secret_fields(self):
        rows = validate_environment({'WB2API_KEY':'key-sentinel', 'WB_ADMIN_PASSWORD':'pwd-sentinel',
            'WB_HTTP_PROXY':'http://user-sentinel:pass-sentinel@127.0.0.1:9000/private-token?q=secret'})
        blob = json.dumps(rows)
        for forbidden in ('key-sentinel','pwd-sentinel','user-sentinel','pass-sentinel','private-token','q=secret'):
            self.assertNotIn(forbidden,blob)
        self.assertEqual(rows['settings']['WB_HTTP_PROXY']['value'],'http://127.0.0.1:9000')

    def test_bad_configuration_stops_before_directories_database_or_background_work(self):
        from fastapi.testclient import TestClient
        from server.main import app
        with patch.dict(os.environ,{'WB_GATEWAY_RATE_PER_MIN':'not-an-integer'}), \
             patch('server.config.ensure_dirs',side_effect=AssertionError('directory side effect')), \
             patch('server.db.connect',side_effect=AssertionError('database side effect')):
            with self.assertRaises(ConfigurationError):
                with TestClient(app):pass
