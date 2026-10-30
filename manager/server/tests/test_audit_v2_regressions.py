"""Release .2: metadata redaction and read-status filesystem failures only."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import config, configcontract, main, security
from server.routers import system as system_router
from server.services import taskrun, updater
from ._db_fixture import isolate_database


class DriftAllSourceRedactionTest(unittest.TestCase):
    SENTINEL = 'synthetic-audit-v2-secret-never-return'

    def setUp(self):
        isolate_database(self)

    def test_sensitive_schema_source_expression_and_duplicate_values_are_redacted(self):
        name = 'WB_ADMIN_PASSWORD'
        specs = {name: {'type': 'string', 'sensitive': True, 'default': self.SENTINEL}}
        source = {name: {'default': self.SENTINEL,
                         'expression': f'str({self.SENTINEL!r})',
                         'sites': ['config.py:10'], 'conflict': False}}
        duplicate = {name: [{'file': 'a.service', 'value': self.SENTINEL},
                            {'file': 'b.conf', 'value': self.SENTINEL}]}
        original = copy.deepcopy((specs, source, duplicate))
        result = configcontract.compare_sources(
            specs, source_defaults=source, declared_env={name: self.SENTINEL},
            process_env={name: self.SENTINEL}, unit_duplicates=duplicate)
        self.assertNotIn(self.SENTINEL, json.dumps(result))
        row = result['rows'][0]
        for field in ('schema_default', 'source_default', 'source_expression', 'declared', 'process'):
            self.assertTrue(row[field]['redacted'], field)
            self.assertTrue(row[field]['set'], field)
        self.assertEqual(row['source_default']['fingerprint'], row['process']['fingerprint'])
        self.assertEqual(row['source_sites'], ['config.py:10'])
        for item in result['duplicate_keys'][name]:
            self.assertTrue(item['value']['redacted'])
            self.assertEqual(item['value']['fingerprint'], row['process']['fingerprint'])
        self.assertEqual((specs, source, duplicate), original, 'Never mutate cached source or unit data')

    def test_sensitive_schema_expression_and_empty_values_keep_absence_semantics(self):
        name = 'WB_ADMIN_PASSWORD'
        specs = {name: {'sensitive': True, 'default_expression': self.SENTINEL}}
        row = configcontract.compare_sources(specs, source_defaults={name: {
            'default': '', 'expression': None}})['rows'][0]
        self.assertTrue(row['schema_default']['redacted'])
        self.assertTrue(row['source_default']['redacted'])
        self.assertFalse(row['source_default']['set'])
        self.assertIsNone(row['source_expression'])
        self.assertIsNone(row['declared'])
        self.assertIsNone(row['process'])

    def test_unknown_environment_duplicates_hidden_but_public_code_preserved(self):
        name = 'WB_FUTURE_SETTING'
        result = configcontract.compare_sources({},
            source_defaults={name: {'default': 7, 'expression': 'str(ROOT / "data")'}},
            process_env={name: self.SENTINEL},
            unit_duplicates={name: [{'file': 'unknown.conf', 'value': self.SENTINEL}]})
        self.assertNotIn(self.SENTINEL, json.dumps(result))
        row = result['rows'][0]
        self.assertEqual(row['source_default'], 7)
        self.assertEqual(row['source_expression'], 'str(ROOT / "data")')
        self.assertTrue(result['duplicate_keys'][name][0]['value']['unregistered'])

    def test_non_sensitive_defaults_keep_native_types_and_duplicate_values(self):
        specs = {'WB_NUMBER': {'default': 7}, 'WB_FLAG': {'default': False}}
        result = configcontract.compare_sources(specs,
            source_defaults={'WB_NUMBER': {'default': 7}, 'WB_FLAG': {'default': False}},
            unit_duplicates={'WB_NUMBER': [{'file': 'a.conf', 'value': '8'}]})
        rows = {row['name']: row for row in result['rows']}
        self.assertIs(type(rows['WB_NUMBER']['source_default']), int)
        self.assertIs(rows['WB_FLAG']['source_default'], False)
        self.assertEqual(result['duplicate_keys']['WB_NUMBER'][0]['value'], '8')

    def test_url_credentials_are_not_reintroduced_by_source_or_duplicates(self):
        name = 'WB_HTTP_PROXY'
        url = 'http://u:synthetic-password@127.0.0.1:9000/private?q=secret'
        result = configcontract.compare_sources({name: {}},
            source_defaults={name: {'default': url}},
            unit_duplicates={name: [{'file': 'a.conf', 'value': url}]})
        self.assertEqual(result['rows'][0]['source_default'], 'http://127.0.0.1:9000')
        self.assertEqual(result['duplicate_keys'][name][0]['value'], 'http://127.0.0.1:9000')
        self.assertNotIn('synthetic-password', json.dumps(result))

    def test_real_endpoint_never_serializes_sensitive_duplicate_or_source_values(self):
        original = dict(main.app.dependency_overrides)
        try:
            main.app.dependency_overrides[security.require_panel_reader] = lambda: {'role': 'admin', 'username': 'fixture'}
            name = 'WB_ADMIN_PASSWORD'
            unit = {'declared': {name: self.SENTINEL}, 'duplicates': {
                name: [{'file': 'a.conf', 'value': self.SENTINEL},
                       {'file': 'b.conf', 'value': self.SENTINEL}]},
                'files': ['a.conf', 'b.conf'], 'available': True, 'reason': None,
                'errors': [], 'service': 'fixture.service'}
            with mock.patch.object(system_router, 'read_unit_environment', return_value=unit), \
                 mock.patch.object(configcontract, 'parse_source_defaults', return_value={name: {
                     'default': self.SENTINEL, 'expression': repr(self.SENTINEL), 'sites': ['fixture.py:1']}}):
                response = TestClient(main.app).get('/api/system/config')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(self.SENTINEL, response.text)
            self.assertEqual(len(response.json()['duplicate_keys'][name]), 2)
        finally:
            main.app.dependency_overrides.clear()
            main.app.dependency_overrides.update(original)


class TaskStatusFilesystemFailureTest(unittest.TestCase):
    def setUp(self):
        isolate_database(self)

    def test_host_stat_permission_error_uses_cached_image_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cached = base / 'cache' / 'task_runner.py'
            cached.parent.mkdir()
            cached.write_text('# synthetic; never executed\n', encoding='ascii')
            host = base / 'host' / 'scripts' / 'task_runner.py'
            original = Path.is_file

            def stat(path):
                if path == host:
                    raise PermissionError('synthetic inaccessible host mount')
                return original(path)

            with mock.patch.object(updater, '_upstream_dir', return_value=base / 'host'), \
                 mock.patch.object(Path, 'is_file', stat), \
                 mock.patch.object(taskrun, '_extract_dir', return_value=cached.parent), \
                 mock.patch.object(taskrun, '_cache_is_fresh', return_value=True), \
                 mock.patch.object(taskrun, 'extract_scripts') as extract:
                self.assertIsNone(taskrun._host_script())
                self.assertEqual(taskrun._script_path(), cached)
                extract.assert_not_called()

    def test_host_stat_permission_error_keeps_image_extraction_path(self):
        with mock.patch.object(updater, '_upstream_dir', return_value=Path('/fixture/host')), \
             mock.patch.object(Path, 'is_file', side_effect=[PermissionError('denied'), False]), \
             mock.patch.object(taskrun, '_extract_dir', return_value=Path('/fixture/cache')), \
             mock.patch.object(taskrun, 'extract_scripts', return_value=(True, 'fixture')) as extract:
            self.assertEqual(taskrun._script_path(), Path('/fixture/cache/task_runner.py'))
        extract.assert_called_once_with()

    def test_extracted_script_stat_permission_error_is_readable_unavailable(self):
        with mock.patch.object(taskrun, '_script_path', return_value=Path('/fixture/task_runner.py')), \
             mock.patch.object(Path, 'is_file', side_effect=PermissionError('private value not returned')):
            ok, reason = taskrun.available()
        self.assertFalse(ok)
        self.assertIn('PermissionError', reason)
        self.assertIn('WB_UPSTREAM_DIR', reason)
        self.assertNotIn('private value', reason)

    def test_script_resolution_oserror_is_unavailable_and_async_matches(self):
        with mock.patch.object(taskrun, '_script_path', side_effect=OSError('synthetic mount failure')):
            self.assertFalse(taskrun.available()[0])
            self.assertFalse(asyncio.run(taskrun.available_async())[0])

    def test_account_directory_stat_permission_error_is_unavailable(self):
        with mock.patch.object(taskrun, '_script_path', return_value=Path('/fixture/task_runner.py')), \
             mock.patch.object(Path, 'is_file', return_value=True), \
             mock.patch.object(Path, 'is_dir', side_effect=PermissionError('synthetic')):
            ok, reason = taskrun.available()
        self.assertFalse(ok)
        self.assertIn('WB_AUTH_DIR', reason)

    def test_non_oserror_programming_bug_is_not_swallowed(self):
        with mock.patch.object(taskrun, '_script_path', side_effect=RuntimeError('programming bug')):
            with self.assertRaises(RuntimeError):
                taskrun.available()
        with mock.patch.object(updater, '_upstream_dir', return_value=Path('/fixture')), \
             mock.patch.object(Path, 'is_file', side_effect=ValueError('programming bug')):
            with self.assertRaises(ValueError):
                taskrun._host_script()

    def test_status_returns_unavailable_and_never_starts_task(self):
        with mock.patch.object(taskrun, '_script_path', side_effect=PermissionError('synthetic')), \
             mock.patch.object(taskrun.db, 'get_setting', return_value={}), \
             mock.patch('subprocess.run') as spawn:
            snapshot = taskrun.status()
        self.assertFalse(snapshot['available'])
        self.assertIn('PermissionError', snapshot['unavailable_reason'])
        spawn.assert_not_called()

    def test_real_read_endpoint_survives_permission_error_without_task_execution(self):
        original = dict(main.app.dependency_overrides)
        try:
            main.app.dependency_overrides[security.require_panel_reader] = lambda: {'role': 'admin', 'username': 'fixture'}
            with mock.patch.object(taskrun, '_script_path', side_effect=PermissionError('synthetic')), \
                 mock.patch.object(taskrun.db, 'get_setting', return_value={}), \
                 mock.patch('subprocess.run') as spawn:
                response = TestClient(main.app).get('/api/task-run')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['available'])
            spawn.assert_not_called()
        finally:
            main.app.dependency_overrides.clear()
            main.app.dependency_overrides.update(original)


if __name__ == '__main__':
    unittest.main()
