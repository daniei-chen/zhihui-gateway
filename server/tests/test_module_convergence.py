"""U28 回归：模块收敛后**行为一致**（不断言代码形状，只断言真实调用的结果）。

本轮收敛两处：

**A. `qtgcore/adapters/workbuddy.py::_settings_from_sql` 的 settings 读取**
原实现 `SELECT * FROM settings LIMIT 1` + `dict(zip(cols, row))` —— `settings` 是
`(key PRIMARY KEY, value)` 的 key/value 表，于是"设置面板"实际返回的是
`{'key': …, 'value': …}`（某一行的两个列），取到哪一行由扫描顺序决定，
可能正好是 `github_token`（与 U19 在 `routers/settings.py` 修掉的是同一缺陷）。
现在改走 `qtgcore/adapters/local_settings.py::read_panel`（key/value 逐键 +
白名单 + JSON 解析），与 Manager 的 `/api/settings/panel` 同一口径。

这里钉住的是**等价性**：
  * 同一份数据库上，适配器读到的东西 == Manager 端点返回的东西；
  * 白名单两份逐字相同（重复可以，漂移不行）；
  * 凭据键不出现、JSON 值被解析、空表是 `{}`；
  * **外层信封没变**（`scheduler`/`pool`/`raw`）—— `qtgcore/service.py` 读的是
    `wb_settings['scheduler']`，改信封会静默打断它。

**B. `qtgcore/routers/compat*.py` 里 `_rt` / `_no` 的复制粘贴**
`_rt` 在 5 个文件里逐字节相同、`_no` 在 2 个里相同。现在统一在
`qtgcore/routers/_shared.py`，各路由继续 re-export（`compat._rt` 等符号仍可用）。
这里钉住：各模块导出的**就是**共享函数、且它们的行为（读 app.state.rt / 409 文案）不变。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.qtgcore.adapters import local_settings, workbuddy as workbuddy_adapter  # noqa: E402
from server.qtgcore.routers import (  # noqa: E402
    _shared, compat, compat_accounts_ext, compat_fixes, compat_settings, compat_stats,
)
from server.routers import settings as manager_settings  # noqa: E402

from ._db_fixture import isolate_database  # noqa: E402

#: 与 `routers/settings.py` 注释里点名的敏感键对应：它们**不允许**出现在面板里。
SECRET_KEYS = ('github_token', 'pg_sync_config', 'update_proxy', 'proxy_pool',
               'proxy_health', 'proxy_sticky', 'security')


class _SettingsFixture(unittest.TestCase):

    def setUp(self) -> None:
        isolate_database(self)
        self._tmp = None

    def _seed(self, rows: dict) -> None:
        with db.transaction() as conn:
            for key, value in rows.items():
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)',
                             (key, value))

    def _panel_from_manager(self) -> dict:
        """直接调 Manager 的端点函数（绕开 FastAPI 依赖注入，仍然走真实实现）。"""
        return manager_settings.settings_panel({'username': 'admin', 'role': 'admin'})


class SettingsPanelConvergenceTest(_SettingsFixture):

    def test_whitelists_are_identical(self) -> None:
        """两份白名单必须逐字相同 —— 这是"重复可接受、漂移不可接受"的护栏。"""
        self.assertEqual(local_settings.PANEL_SETTING_KEYS,
                         manager_settings._PANEL_SETTING_KEYS)

    def test_adapter_panel_equals_manager_panel_on_same_database(self) -> None:
        self._seed({
            'model_map': json.dumps({'gpt-4o': 'cn:deepseek-v3'}),
            'prompt_compat_enabled': json.dumps(True),
            'credits_snapshot': json.dumps({'at': 1, 'total': 2}),
            'daily_tasks_plan': 'not-json-at-all',
        })
        conn = sqlite3.connect(str(db._conn.execute('PRAGMA database_list').fetchone()[2]))
        try:
            adapter_panel = local_settings.read_panel(conn)
        finally:
            conn.close()
        self.assertEqual(adapter_panel, self._panel_from_manager())

    def test_panel_is_flat_and_never_leaks_credentials(self) -> None:
        # 把凭据键放在**第一行**：旧实现（LIMIT 1 + zip(cols,row)）会把它当"配置"返回
        self._seed({'github_token': json.dumps('ghp_super_secret_token'),
                    'pg_sync_config': json.dumps({'dsn': 'postgresql://u:p@h/db'}),
                    'model_map': json.dumps({'a': 'b'})})
        conn = db._conn
        panel = local_settings.read_panel(conn)
        self.assertEqual(panel, {'model_map': {'a': 'b'}})
        for secret in SECRET_KEYS:
            self.assertNotIn(secret, panel, '敏感键不允许出现在设置面板里')
        blob = json.dumps(panel, ensure_ascii=False)
        self.assertNotIn('ghp_super_secret_token', blob)
        self.assertNotIn('postgresql://', blob)
        # 形状：必须是"键 → 值"，不是"某一行的两个列"
        self.assertNotIn('key', panel)
        self.assertNotIn('value', panel)

    def test_json_values_parsed_and_bad_json_kept_raw(self) -> None:
        self._seed({'model_map': json.dumps({'x': 1}),
                    'token_refresh_plan': '{"broken": ',
                    'task_claim_schedule': json.dumps([1, 2, 3])})
        panel = local_settings.read_panel(db._conn)
        self.assertEqual(panel['model_map'], {'x': 1})
        self.assertEqual(panel['task_claim_schedule'], [1, 2, 3])
        self.assertEqual(panel['token_refresh_plan'], '{"broken": ',
                         'JSON 解析失败要回原文，不能吞成 default')

    def test_empty_table_is_empty_dict(self) -> None:
        self.assertEqual(local_settings.read_panel(db._conn), {})
        self.assertEqual(self._panel_from_manager(), {})

    def test_missing_settings_table_degrades_in_adapter_but_fails_loudly_in_api(self) -> None:
        """刻意的差异：适配器读不到表 → `{}`（降级）；API 读不到表 → 503（不伪装"没配置"）。"""
        from fastapi import HTTPException
        db.execute('DROP TABLE settings')
        self.assertEqual(local_settings.read_panel(db._conn), {})
        with self.assertRaises(HTTPException) as caught:
            self._panel_from_manager()
        self.assertEqual(caught.exception.status_code, 503)


class AdapterSettingsEnvelopeTest(_SettingsFixture):

    def _adapter(self, conn: sqlite3.Connection):
        """真实适配器实例（跳过 __init__ 的网络/配置依赖，只替换两个数据源）。"""
        adapter = object.__new__(workbuddy_adapter.WorkBuddyAdapter)
        adapter._db = lambda: conn                     # type: ignore[method-assign]
        adapter._auths = lambda: [{'uid': 'a'}, {'uid': 'b'}]   # type: ignore[method-assign]
        return adapter

    def test_envelope_is_unchanged_and_panel_is_flat(self) -> None:
        self._seed({'model_map': json.dumps({'m': 'n'})})
        tasks = [{'name': '签到', 'key': 'checkin', 'hours': [7], 'enabled': True}]
        result = self._adapter(db._conn)._settings_from_sql({'upstream': 'x'},
                                                            {'checkin_hours': [7]}, tasks)
        # 信封：qtgcore/service.py 读的就是 'scheduler'，不能变
        self.assertEqual(set(result), {'scheduler', 'pool', 'raw'})
        self.assertEqual(result['scheduler']['tasks'], tasks)
        self.assertTrue(result['scheduler']['enabled'])
        self.assertEqual(result['pool']['total'], 2)
        self.assertEqual(result['raw']['config'], {'upstream': 'x'})
        # panel：扁平白名单字典（不是 {'key','value'}）
        self.assertEqual(result['raw']['panel'], {'model_map': {'m': 'n'}})

    def test_envelope_survives_missing_database(self) -> None:
        """`_db()` 返回 None（库不存在/schema 不符）时，信封仍在、panel 为空。"""
        adapter = object.__new__(workbuddy_adapter.WorkBuddyAdapter)
        adapter._db = lambda: None                     # type: ignore[method-assign]
        adapter._auths = lambda: []                    # type: ignore[method-assign]
        result = adapter._settings_from_sql({}, {}, [])
        self.assertEqual(result['raw']['panel'], {})
        self.assertEqual(result['pool']['total'], 0)
        self.assertEqual(result['scheduler']['tasks'], [])


class CompatHelperConvergenceTest(unittest.TestCase):

    ROUTERS = (compat, compat_accounts_ext, compat_fixes, compat_settings, compat_stats)

    def test_all_compat_routers_reexport_the_shared_rt(self) -> None:
        for module in self.ROUTERS:
            with self.subTest(module=module.__name__):
                self.assertIs(module._rt, _shared._rt,
                              '各 compat 路由必须继续导出共享的 _rt（不删符号）')

    def test_no_is_shared_where_it_is_used(self) -> None:
        for module in (compat_accounts_ext, compat_settings):
            with self.subTest(module=module.__name__):
                self.assertIs(module._no, _shared._no)

    def test_rt_reads_app_state(self) -> None:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rt='RUNTIME')))
        self.assertEqual(_shared._rt(request), 'RUNTIME')

    def test_no_is_a_409_with_stable_detail(self) -> None:
        response = _shared._no('账号导入')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)['detail'], '本部署不支持账号导入')
        response = _shared._no('账号导入', '引擎未启用')
        self.assertEqual(json.loads(response.body)['detail'], '本部署不支持账号导入：引擎未启用')

    def test_compat_routers_still_expose_their_routes(self) -> None:
        """收敛不能动路由注册：每个 compat 路由至少还挂着一条路径，且前缀没变。"""
        for module in self.ROUTERS:
            with self.subTest(module=module.__name__):
                self.assertGreaterEqual(len(module.router.routes), 1)
                self.assertEqual(module.router.prefix, '/api')
        paths = {route.path for route in compat.router.routes}
        self.assertIn('/api/accounts/checkin-all', paths, 'compat 的核心路径必须还在')


if __name__ == '__main__':
    unittest.main()
