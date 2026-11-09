"""Selected upstream ideas must retain local auth and accounting contracts."""
import unittest

from server import keysvc
from server.routers import gateway, responses
from server.services import tencent


class ModelDisplayTest(unittest.TestCase):
    def test_normalizes_cn_only_without_mutating_source(self):
        source = {'object': 'list', 'data': [
            {'id': 'cn:deepseek-v4.1-flash'}, {'id': 'global:deepseek-v4.1-flash'},
            {'id': 'qoder:model'}, {'id': 'CN:glm-5.2'}, {'id': 'cn:'}, None]}
        result = gateway._bare_cn_model_ids(source)
        self.assertEqual([x['id'] for x in result['data'] if isinstance(x, dict)],
                         ['deepseek-v4.1-flash', 'global:deepseek-v4.1-flash',
                          'qoder:model', 'glm-5.2', 'cn:'])
        self.assertEqual(source['data'][0]['id'], 'cn:deepseek-v4.1-flash')
        self.assertEqual(result['object'], 'list')

    def test_duplicate_keeps_complete_entry_and_original_order(self):
        source = {'data': [{'id': 'cn:x'}, {'id': 'global:x'},
                           {'id': 'x', 'supports_images': True}]}
        self.assertEqual(gateway._bare_cn_model_ids(source)['data'],
                         [{'id': 'x', 'supports_images': True}, {'id': 'global:x'}])

    def test_nested_prefix_is_not_reinterpreted_as_a_different_realm(self):
        source = {'data': [{'id': 'cn:global:x'}, {'id': 'cn:qoder:x'}]}
        self.assertEqual(gateway._bare_cn_model_ids(source), source)

    def test_scope_runs_before_display(self):
        source = {'data': [{'id': 'cn:glm-5.2'}, {'id': 'cn:forbidden'},
                           {'id': 'global:glm-5.2'}]}
        key = {'realm': 'cn', 'models': ['cn:glm-5.2']}
        result = gateway._bare_cn_model_ids(gateway._scope_models(source, key))
        self.assertEqual(result['data'], [{'id': 'glm-5.2'}])
        self.assertTrue(keysvc.model_allowed(key, result['data'][0]['id']))

    def test_portal_whitelist_is_not_expanded(self):
        from server import portalpolicy
        key = {'source': 'portal-v1'}
        source = {'data': [{'id': 'cn:deepseek-v4.1-flash'}, {'id': 'cn:glm-5.2'},
                           {'id': 'global:deepseek-v4.1-flash'}]}
        result = gateway._bare_cn_model_ids(gateway._scope_models(source, key))
        self.assertTrue(all(portalpolicy.model_allowed(x['id']) for x in result['data']))
        self.assertEqual(len(result['data']), 1)

    def test_unknown_shape_is_not_rewritten(self):
        for source in (None, [], 'bad', {'error': 'bad'}, {'data': 'bad'}):
            self.assertIs(gateway._bare_cn_model_ids(source), source)


class NamespaceCompatibilityTest(unittest.TestCase):
    def test_all_child_spellings_restore_namespace(self):
        for field in ('tools', 'children', 'functions'):
            with self.subTest(field=field):
                bridge = responses._ToolBridge([{'type': 'namespace', 'name': 'api', field: [
                    {'type': 'function', 'name': 'fetch'}]}])
                self.assertEqual(len(bridge.chat_tools), 1)
                self.assertEqual(bridge.restore(bridge.outbound_name('fetch', 'api')),
                                 ('fetch', 'function', 'api'))

    def test_implicit_namespace_and_empty_primary_children(self):
        bridge = responses._ToolBridge([{'name': 'api', 'tools': [], 'children': [
            {'type': 'custom', 'name': 'raw'}]}])
        self.assertEqual(bridge.restore(bridge.outbound_name('raw', 'api')), ('raw', 'custom', 'api'))

    def test_four_levels_work_and_five_fail_closed(self):
        tool = {'type': 'function', 'name': 'fetch'}
        for index in range(4):
            tool = {'type': 'namespace', 'name': 'ns' + str(index), 'tools': [tool]}
        self.assertEqual(len(responses._ToolBridge([tool]).chat_tools), 1)
        with self.assertRaisesRegex(responses.ToolSearchArgumentsError, 'four levels'):
            responses._ToolBridge([{'type': 'namespace', 'name': 'extra', 'tools': [tool]}])

    def test_same_name_across_namespaces_remains_distinct(self):
        bridge = responses._ToolBridge([{'type': 'namespace', 'name': ns, 'functions': [
            {'type': 'function', 'name': 'fetch'}]} for ns in ('one', 'two')])
        self.assertEqual(len(bridge.chat_tools), 2)
        self.assertNotEqual(bridge.outbound_name('fetch', 'one'), bridge.outbound_name('fetch', 'two'))

    def test_loaded_tools_use_the_same_depth_guard(self):
        tool = {'type': 'function', 'name': 'fetch', 'defer_loading': True}
        for index in range(5):
            tool = {'type': 'namespace', 'name': str(index), 'functions': [tool]}
        with self.assertRaises(responses.ToolSearchArgumentsError):
            responses._ToolBridge([], [{'type': 'additional_tools', 'tools': [tool]}])


class CreditSummaryTest(unittest.TestCase):
    def test_known_subtotals_not_added_to_coded_details(self):
        rows = [{'PackageCode': 'a', 'CapacityRemain': 900},
                {'PackageCode': 123, 'CapacityRemain': 100},
                {'PackageName': '\u5e73\u53f0\u5956\u52b1\u79ef\u5206', 'CapacityRemain': 1000},
                {'PackageName': '\u5957\u9910\u57fa\u7840\u79ef\u5206', 'CapacityRemain': 500},
                {'PackageCode': 'b', 'CapacityRemain': 500}]
        self.assertEqual(tencent._resource_package_rows(rows), [rows[0], rows[1], rows[4]])

    def test_distinct_uncoded_equal_value_packages_are_not_deduplicated(self):
        rows = [{'PackageCode': 'a', 'CapacityRemain': 100},
                {'PackageName': 'legacy A', 'CapacityRemain': 50},
                {'PackageName': 'legacy B', 'CapacityRemain': 50}]
        self.assertEqual(tencent._resource_package_rows(rows), rows)

    def test_without_coded_details_preserves_old_shape(self):
        rows = [{'PackageName': '\u5e73\u53f0\u5956\u52b1\u79ef\u5206', 'CapacityRemain': 100}]
        self.assertEqual(tencent._resource_package_rows(rows), rows)

    def test_explicit_code_keeps_summary_named_real_package(self):
        rows = [{'PackageCode': 0, 'PackageName': '\u8d2d\u4e70\u79ef\u5206', 'CapacityRemain': 42}]
        self.assertEqual(tencent._resource_package_rows(rows), rows)

    def test_malformed_names_do_not_break_credit_refresh(self):
        rows = [{'PackageCode': 'a', 'CapacityRemain': 1},
                {'PackageName': {'unexpected': True}, 'CapacityRemain': 2}]
        self.assertEqual(tencent._resource_package_rows(rows), rows)

    def test_actual_credit_refresh_uses_filtered_rows_for_total_and_expiries(self):
        from server.tests.test_credit_expiry import FetchCreditsExpiriesTest
        rows = [{'PackageCode': 'a', 'CapacityRemain': 100, 'CycleEndTime': '2030-01-01 00:00:00'},
                {'PackageName': '\u5e73\u53f0\u5956\u52b1\u79ef\u5206', 'CapacityRemain': 100,
                 'CycleEndTime': '2030-01-01 00:00:00'},
                {'PackageName': 'legacy', 'CapacityRemain': 30}]
        ok, total, _, expiries = FetchCreditsExpiriesTest()._fetch(rows)
        self.assertTrue(ok)
        self.assertEqual(total, 130)
        self.assertEqual([x['amount'] for x in expiries], [100])

    def test_retired_verifier_entries_stay_identical(self):
        from server.tests.test_v004_managed_update_boundary import load_script, ROOT
        import contextlib
        import io
        outputs = []
        for path in (ROOT / 'deploy/verify_signed_release.py', ROOT.parent / 'tools/verify_signed_release.py'):
            if path.exists():
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(load_script(path, 'retired_test').main(), 2)
                outputs.append(output.getvalue())
        self.assertTrue(outputs)
        self.assertEqual(len(set(outputs)), 1)
