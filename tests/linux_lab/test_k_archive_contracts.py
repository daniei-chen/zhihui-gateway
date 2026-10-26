"""K27/K47 与 K41 归档脚本的纯逻辑反例（离线，Windows 可跑）。

覆盖两类容易“改宽成随便都过”的判定：

* ``k27_k47_trace``：RV01 两套 ID 分离（执行 ID 服务端 hex32 / 关联 ID 单独回显）、
  恶意 request-id 不得成为执行 ID 或回显成头、同一外部 ID 两笔执行必须独立；
* ``k41_export_import``：脚本自种子账号的幂等判定（新建 / 已存在 / 被其他原因拒绝）。

这里只测不依赖 Manager/DB 的纯函数；真实请求路径仍由脚本本体在 Linux lab 跑。
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


k27 = _load('k27_k47_trace')
k41 = _load('k41_export_import')

HEX32 = 'c6a54caadab743518c1e7f306390a88f'
EXTERNAL = 'k4792958453'


class ExecutionIdTest(unittest.TestCase):
    def test_server_hex32_is_execution_id(self):
        self.assertTrue(k27.is_execution_id(HEX32))

    def test_external_style_id_is_not_execution_id(self):
        self.assertFalse(k27.is_execution_id(EXTERNAL))
        self.assertFalse(k27.is_execution_id('A' * 300))
        self.assertFalse(k27.is_execution_id('abc def'))

    def test_uppercase_or_wrong_length_is_rejected(self):
        self.assertFalse(k27.is_execution_id(HEX32.upper()))
        self.assertFalse(k27.is_execution_id(HEX32[:-1]))
        self.assertFalse(k27.is_execution_id(HEX32 + '0'))

    def test_none_and_empty_are_rejected(self):
        self.assertFalse(k27.is_execution_id(None))
        self.assertFalse(k27.is_execution_id(''))


class HeaderContractTest(unittest.TestCase):
    def test_two_ids_separated_passes(self):
        verdict = k27.header_contract_verdict(EXTERNAL, HEX32, EXTERNAL)
        self.assertTrue(verdict['ok'], verdict['reasons'])

    def test_external_id_reused_as_execution_id_fails(self):
        verdict = k27.header_contract_verdict(EXTERNAL, EXTERNAL, EXTERNAL)
        self.assertFalse(verdict['ok'])
        self.assertIn('execution_id_not_server_hex32', verdict['reasons'])
        self.assertIn('execution_id_equals_external_id', verdict['reasons'])

    def test_missing_echo_fails(self):
        verdict = k27.header_contract_verdict(EXTERNAL, HEX32, None)
        self.assertFalse(verdict['ok'])
        self.assertIn('client_request_id_not_echoed', verdict['reasons'])

    def test_wrong_echo_fails(self):
        verdict = k27.header_contract_verdict(EXTERNAL, HEX32, 'someone-else')
        self.assertFalse(verdict['ok'])

    def test_no_external_id_must_not_echo(self):
        self.assertTrue(k27.no_external_id_verdict(HEX32, None)['ok'])
        self.assertFalse(k27.no_external_id_verdict(HEX32, 'echoed')['ok'])
        self.assertFalse(k27.no_external_id_verdict('not-hex', None)['ok'])


class MaliciousIdTest(unittest.TestCase):
    def test_ignored_bad_value_with_server_id_passes(self):
        verdict = k27.malicious_id_verdict(status=200, execution_id=HEX32,
                                           executed_client_id=None, bad_value='A' * 300)
        self.assertTrue(verdict['ok'], verdict['reasons'])

    def test_bad_value_becoming_execution_id_fails(self):
        verdict = k27.malicious_id_verdict(status=200, execution_id='A' * 300,
                                           executed_client_id=None, bad_value='A' * 300)
        self.assertFalse(verdict['ok'])
        self.assertIn('execution_id_not_server_hex32', verdict['reasons'])

    def test_echoing_unvalidated_value_fails(self):
        verdict = k27.malicious_id_verdict(status=200, execution_id=HEX32,
                                           executed_client_id='abc def', bad_value='abc def')
        self.assertFalse(verdict['ok'])
        self.assertIn('unvalidated_client_request_id_echoed', verdict['reasons'])

    def test_explicit_rejection_is_acceptable(self):
        verdict = k27.malicious_id_verdict(status=400, execution_id=None,
                                           executed_client_id=None, bad_value='abc\x01def')
        self.assertTrue(verdict['ok'], verdict['reasons'])


class RowIdentityTest(unittest.TestCase):
    def row(self, **overrides):
        base = {'request_id': HEX32, 'client_request_id': EXTERNAL}
        base.update(overrides)
        return base

    def test_separated_ids_pass(self):
        verdict = k27.identity_verdict(self.row(), external_id=EXTERNAL, execution_id=HEX32)
        self.assertTrue(verdict['ok'], verdict['reasons'])

    def test_missing_row_fails(self):
        self.assertFalse(k27.identity_verdict({}, external_id=EXTERNAL,
                                              execution_id=HEX32)['ok'])

    def test_client_id_stored_as_request_id_fails(self):
        verdict = k27.identity_verdict(self.row(request_id=EXTERNAL,
                                                client_request_id=EXTERNAL),
                                       external_id=EXTERNAL, execution_id=HEX32)
        self.assertFalse(verdict['ok'])

    def test_unrecorded_client_id_fails(self):
        verdict = k27.identity_verdict(self.row(client_request_id=None),
                                       external_id=EXTERNAL, execution_id=HEX32)
        self.assertFalse(verdict['ok'])
        self.assertIn('client_request_id_mismatch', verdict['reasons'])


class DuplicateExternalIdTest(unittest.TestCase):
    def test_two_distinct_executions_pass(self):
        verdict = k27.distinct_execution_verdict(EXTERNAL, [
            {'status': 200, 'execution_id': HEX32},
            {'status': 200, 'execution_id': 'a' * 32}])
        self.assertTrue(verdict['ok'], verdict['reasons'])

    def test_shared_execution_id_fails(self):
        verdict = k27.distinct_execution_verdict(EXTERNAL, [
            {'status': 200, 'execution_id': HEX32},
            {'status': 200, 'execution_id': HEX32}])
        self.assertFalse(verdict['ok'])
        self.assertIn('execution_ids_shared', verdict['reasons'])

    def test_external_id_as_execution_id_fails(self):
        verdict = k27.distinct_execution_verdict(EXTERNAL, [
            {'status': 200, 'execution_id': EXTERNAL},
            {'status': 200, 'execution_id': 'a' * 32}])
        self.assertFalse(verdict['ok'])

    def test_second_request_500_fails(self):
        verdict = k27.distinct_execution_verdict(EXTERNAL, [
            {'status': 200, 'execution_id': HEX32},
            {'status': 500, 'execution_id': 'a' * 32}])
        self.assertFalse(verdict['ok'])
        self.assertIn('duplicate_external_id_request_failed', verdict['reasons'])

    def test_single_execution_is_not_enough(self):
        self.assertFalse(k27.distinct_execution_verdict(
            EXTERNAL, [{'status': 200, 'execution_id': HEX32}])['ok'])


class ProvenanceTest(unittest.TestCase):
    def test_known_provenance_passes(self):
        self.assertTrue(k27.provenance_verdict(
            {'provenance': 'unknown-credit', 'counted': 0})['ok'])
        self.assertTrue(k27.provenance_verdict(
            {'provenance': 'provider-reported', 'counted': 1})['ok'])

    def test_missing_or_unknown_provenance_fails(self):
        self.assertFalse(k27.provenance_verdict(None)['ok'])
        self.assertFalse(k27.provenance_verdict({'provenance': 'guessed'})['ok'])


class SeedPayloadTest(unittest.TestCase):
    def test_payload_shape_is_importable(self):
        raw = k41.seed_account_payload()
        payload = json.loads(raw.decode('utf-8'))
        self.assertEqual(payload['uid'], k41.SEED_UID)
        self.assertEqual(payload['accessToken'], k41.SEED_TOKEN)
        self.assertIsInstance(payload['expiresAt'], int)
        self.assertGreater(payload['expiresAt'], 0)

    def test_filename_matches_server_naming(self):
        self.assertEqual(k41.SEED_FILENAME, 'workbuddy-' + k41.SEED_UID + '.json')

    def test_uid_respects_server_charset(self):
        # write_auth_file 的 uid 白名单：[0-9A-Za-z_-]{1,80}
        self.assertRegex(k41.SEED_UID, r'^[0-9A-Za-z_-]{1,80}$')


class SeedVerdictTest(unittest.TestCase):
    def test_created_account_is_ready(self):
        verdict = k41.seed_result_verdict(
            200, {'ok': True, 'added': [{'uid': k41.SEED_UID, 'file': k41.SEED_FILENAME}],
                  'uploaded': [], 'rejected': [], 'failed': []})
        self.assertTrue(verdict['ready'], verdict['reasons'])
        self.assertTrue(verdict['created'])
        self.assertFalse(verdict['already_present'])

    def test_existing_account_is_ready_and_idempotent(self):
        verdict = k41.seed_result_verdict(
            200, {'ok': False, 'added': [],
                  'rejected': [{'file': k41.SEED_FILENAME, 'uid': k41.SEED_UID,
                                'message': '账号已存在，未覆盖'}],
                  'failed': []})
        self.assertTrue(verdict['ready'], verdict['reasons'])
        self.assertFalse(verdict['created'])
        self.assertTrue(verdict['already_present'])

    def test_rejected_for_other_reason_is_not_ready(self):
        verdict = k41.seed_result_verdict(
            200, {'ok': False, 'added': [],
                  'rejected': [{'file': k41.SEED_FILENAME, 'message': '账号条目无效'}],
                  'failed': []})
        self.assertFalse(verdict['ready'])
        self.assertFalse(verdict['already_present'])

    def test_failed_upload_is_not_ready(self):
        verdict = k41.seed_result_verdict(
            200, {'ok': False, 'added': [], 'rejected': [],
                  'failed': [{'file': k41.SEED_FILENAME, 'message': '文件格式无效'}]})
        self.assertFalse(verdict['ready'])
        self.assertIn('seed_failed', verdict['reasons'][0])

    def test_http_error_is_not_ready(self):
        self.assertFalse(k41.seed_result_verdict(
            403, {'ok': False, 'rejected': [{'message': '账号已存在，未覆盖'}]})['ready'])

    def test_non_json_body_is_not_ready(self):
        self.assertFalse(k41.seed_result_verdict(200, None)['ready'])
        self.assertFalse(k41.seed_result_verdict(200, '<html>')['ready'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
