"""v1.2.31：把每请求都付一次的配置读取挪出数据库与文件系统。

只测"缓存命中"会漏掉"缓存永不过期"，只测"失效"会漏掉"缓存根本没生效"，
所以两组都写。

注意这里**没有**给 `db.get_setting` 加通用缓存：`settings` 表里混着协调类键
（`task_claim_schedule` 等），给它们加 1 秒缓存会把"栅栏刚被别的 owner 拿走"
读成旧值。v1.2.31 试过一版，被 `test_task_leases` 的 lease 抢占用例当场抓住，
所以改成只缓存出口代理这一个纯配置。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import config, db


class GlobalProxyCacheTests(unittest.TestCase):
    def setUp(self):
        config._GLOBAL_PROXY_CACHE.update(generation=-1, at=0.0, value='')
        config.invalidate_global_proxy()
        self.addCleanup(lambda: config._GLOBAL_PROXY_CACHE.update(generation=-1, at=0.0, value=''))
        self.addCleanup(config.invalidate_global_proxy)

    def _spy(self, mapping):
        calls = []

        def fake_get_setting(key, default=None):
            calls.append(key)
            return mapping.get(key, default)

        return calls, fake_get_setting

    def test_repeated_lookup_hits_the_cache(self):
        calls, spy = self._spy({'http_proxy': 'http://127.0.0.1:7890',
                                'global_proxy_enabled': True})
        with mock.patch.object(db, 'get_setting', spy):
            self.assertEqual(config.global_http_proxy(), 'http://127.0.0.1:7890')
            self.assertEqual(config.global_http_proxy(), 'http://127.0.0.1:7890')
            self.assertEqual(config.global_http_proxy(), 'http://127.0.0.1:7890')
        self.assertEqual(calls, ['global_proxy_enabled', 'http_proxy'],
                         '三次调用只应读一次库')

    def test_invalidate_forces_a_reread(self):
        calls, spy = self._spy({'http_proxy': 'http://a', 'global_proxy_enabled': True})
        with mock.patch.object(db, 'get_setting', spy):
            self.assertEqual(config.global_http_proxy(), 'http://a')
            config.invalidate_global_proxy()
            self.assertEqual(config.global_http_proxy(), 'http://a')
        self.assertEqual(len(calls), 4)

    def test_expired_entry_is_reread(self):
        calls, spy = self._spy({'http_proxy': 'http://a', 'global_proxy_enabled': True})
        with mock.patch.object(db, 'get_setting', spy):
            config.global_http_proxy()
            config._GLOBAL_PROXY_CACHE['at'] = time.monotonic() - config._GLOBAL_PROXY_TTL - 0.01
            config.global_http_proxy()
        self.assertEqual(len(calls), 4)

    def test_disabled_switch_wins(self):
        _, spy = self._spy({'http_proxy': 'http://a', 'global_proxy_enabled': False})
        with mock.patch.object(db, 'get_setting', spy):
            self.assertEqual(config.global_http_proxy(), '')

    def test_saving_the_proxy_setting_invalidates(self):
        before = config._GLOBAL_PROXY_GENERATION
        with mock.patch.object(db, 'execute', lambda *_a, **_k: 1):
            db.set_setting('http_proxy', 'http://new')
        self.assertEqual(config._GLOBAL_PROXY_GENERATION, before + 1)

    def test_saving_an_unrelated_setting_does_not_invalidate(self):
        before = config._GLOBAL_PROXY_GENERATION
        with mock.patch.object(db, 'execute', lambda *_a, **_k: 1):
            db.set_setting('model_map', {'a': 'b'})
        self.assertEqual(config._GLOBAL_PROXY_GENERATION, before)


class UpstreamKeyCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.path.write_text(json.dumps({'api_key': 'first-key'}), encoding='utf-8')
        config._UPSTREAM_KEY_CACHE['marker'] = None
        config._UPSTREAM_KEY_CACHE['value'] = ''
        patches = [mock.patch.object(config, 'UPSTREAM_CONFIG', self.path),
                   mock.patch.object(config, 'WB2API_KEY', '')]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: config._UPSTREAM_KEY_CACHE.update(marker=None, value=''))

    def test_reads_the_file_once_then_serves_from_cache(self):
        real_read = Path.read_text
        reads = []

        def counting_read(self_path, *args, **kwargs):
            reads.append(self_path)
            return real_read(self_path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', counting_read):
            self.assertEqual(config.upstream_api_key(), 'first-key')
            self.assertEqual(config.upstream_api_key(), 'first-key')
            self.assertEqual(config.upstream_api_key(), 'first-key')
        self.assertEqual(len(reads), 1, '同一份文件只应解析一次')

    def test_replaced_file_is_picked_up(self):
        self.assertEqual(config.upstream_api_key(), 'first-key')
        replacement = Path(self.tmp.name) / 'config.json.new'
        replacement.write_text(json.dumps({'api_key': 'second-key'}), encoding='utf-8')
        os.replace(replacement, self.path)
        self.assertEqual(config.upstream_api_key(), 'second-key')

    def test_removed_file_returns_empty_and_drops_the_cache(self):
        self.assertEqual(config.upstream_api_key(), 'first-key')
        self.path.unlink()
        self.assertEqual(config.upstream_api_key(), '')
        self.assertIsNone(config._UPSTREAM_KEY_CACHE['marker'])

    def test_env_override_wins_without_touching_the_file(self):
        with mock.patch.object(config, 'WB2API_KEY', 'env-key'):
            self.assertEqual(config.upstream_api_key(), 'env-key')


class PlatformFactTests(unittest.TestCase):
    """D-03：入口平台必须随事实一起进 spool，否则重放会退回 workbuddy。"""

    def test_request_facts_carry_platform(self):
        source = (Path(__file__).parents[2] / 'server' / 'routers' /
                  'gateway.py').read_text(encoding='utf-8')
        self.assertIn("'platform': forced_platform.get() or 'workbuddy'", source)

    def test_spool_whitelist_accepts_platform(self):
        from server import ledgerreplay
        self.assertIn('platform', ledgerreplay.FACTS_FIELDS)


if __name__ == '__main__':
    unittest.main()
