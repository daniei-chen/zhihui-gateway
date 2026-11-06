"""Management edits preserve unrelated state and bound malformed peer input."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from starlette.requests import Request

from server import config, observability
from server.routers import gateway
from server.services import proxyroutes, wb2api


class ManagementBoundaryTests(unittest.TestCase):
    def test_settings_and_proxy_edits_cannot_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'pool': {'max_in_flight': 1}, 'api_key': 'synthetic'}))
            entered, release, proxy_started = threading.Event(), threading.Event(), threading.Event()
            proxy_loaded = threading.Event()
            original = wb2api._sanitize_section
            load_original = proxyroutes._load
            def sanitize(*args):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('fixture config edit was not released')
                return original(*args)
            def proxy_edit():
                proxy_started.set()
                return proxyroutes.upsert('route', 'http://127.0.0.1:8080')
            def load():
                proxy_loaded.set()
                return load_original()
            with patch.object(config, 'UPSTREAM_CONFIG', path), \
                    patch.object(wb2api, '_sanitize_section', side_effect=sanitize), \
                    patch.object(wb2api, 'load_upstream_config', return_value={}), \
                    patch.object(proxyroutes, '_load', side_effect=load), \
                    ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(wb2api.save_upstream_config, {'pool': {'max_in_flight': 2}})
                try:
                    self.assertTrue(entered.wait(1))
                    second = executor.submit(proxy_edit)
                    self.assertTrue(proxy_started.wait(1))
                    # The second complete read/modify/write must wait for the
                    # first writer, rather than read its stale snapshot.
                    self.assertFalse(proxy_loaded.wait(.05), 'Concurrent proxy writer read a stale snapshot')
                    self.assertFalse(second.done())
                finally:
                    release.set()
                first.result(timeout=2)
                second.result(timeout=2)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['pool']['max_in_flight'], 2)
            self.assertEqual(saved['proxies']['route'], 'http://127.0.0.1:8080')
            self.assertEqual(saved['api_key'], 'synthetic')

    def test_auto_assignment_keeps_existing_bindings_unless_requested(self):
        rows = [{'file': name, 'uid': name, 'nickname': name, 'proxy': bound}
                for name, bound in [('a', ''), ('b', ''), ('c', 'existing')]]
        with patch.object(proxyroutes, 'routes', return_value={'target': 'http://fixture:80'}), \
                patch.object(proxyroutes, 'account_rows', return_value=rows), \
                patch.object(proxyroutes, 'set_binding', return_value={'updated': 2}) as bind:
            result = proxyroutes.auto_assign('target', 3)
            self.assertEqual(result['candidates'], 2)
            self.assertEqual(bind.call_args.args[1], ['a', 'b'])
            proxyroutes.auto_assign('target', 3, include_bound=True)
            self.assertEqual(bind.call_args.args[1], ['a', 'b', 'c'])

    def test_proxy_urls_are_idempotent_with_default_ports_and_ipv6(self):
        for raw in ('http://fixture', 'http://fixture:80', 'https://fixture:443',
                    'socks5://user:secret@[::1]:1080'):
            normalized = proxyroutes.normalize_url(raw)
            self.assertEqual(proxyroutes.normalize_url(normalized), normalized)
        stored = 'socks5://user:secret@[::1]:1080'
        masked = proxyroutes.mask_url(stored)
        self.assertNotIn('secret', masked)
        self.assertEqual(urlsplit(masked).hostname, '::1')
        restored = proxyroutes.keep_secret(masked, stored)
        self.assertEqual((urlsplit(restored).hostname, urlsplit(restored).password), ('::1', 'secret'))
        self.assertEqual(proxyroutes.mask_url('http://user:secret@fixture:invalid'), '***')

    def test_bad_account_document_does_not_hide_other_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = [[], {'auth': []}, {'account': 'invalid'},
                         {'account': {'uid': 'valid'}, 'auth': {'accessToken': 'synthetic',
                                                               'expiresAt': 'malformed'}}]
            # Nonempty invalid nested types must also be rejected.
            documents[1] = {'auth': ['invalid']}
            for index, value in enumerate(documents):
                (root / f'workbuddy-{index}.json').write_text(json.dumps(value))
            rows = wb2api.list_auth_accounts(root)
            self.assertEqual([row['uid'] for row in rows], ['valid'])
            self.assertEqual(rows[0]['expires_at'], 0)


class AsyncManagementBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_redis_probe_rejects_large_bulk_before_reading_it(self):
        reader = AsyncMock()
        reader.readline.return_value = b'$10000000\r\n'
        with self.assertRaisesRegex(ValueError, '64 KiB'):
            await wb2api._resp_read_reply(reader)
        reader.readexactly.assert_not_awaited()

    async def test_cancellation_joins_final_ip_claim_and_never_dispatches(self):
        entered, release = threading.Event(), threading.Event()
        worker_ids = []
        key = {'id': 7, 'upstream_id': None}
        state = observability.RequestAudit('dispatch-fixture', '/v1/chat/completions', 'POST',
                                          'openai', time.monotonic())
        context = observability.current.set(state)
        self.addCleanup(observability.current.reset, context)
        request = Request({'type': 'http', 'path': '/v1/chat/completions',
            'headers': [(b'authorization', b'Bearer synthetic')],
            'server': ('fixture', 80), 'client': ('203.0.113.1', 1), 'scheme': 'http',
            'query_string': b''})
        def claim(*args):
            worker_ids.append(threading.get_ident())
            self.assertIs(observability.current.get(), state)
            entered.set()
            if not release.wait(3):
                raise AssertionError('fixture claim was not released')
        with ExitStack() as stack:
            stack.enter_context(patch.object(gateway, '_prepare_forward', return_value=b'{}'))
            stack.enter_context(patch.object(gateway.keysvc, 'resolve', return_value=key))
            stack.enter_context(patch.object(gateway.keysvc, 'validate', return_value=None))
            stack.enter_context(patch.object(gateway, '_global_ip_blocked', return_value=False))
            stack.enter_context(patch.object(gateway.keysvc, 'claim_ip', side_effect=claim))
            forwarded = stack.enter_context(patch('server.budget.forwarded'))
            task = asyncio.create_task(gateway._prepare_forward_async(key, {'model': 'fixture'},
                                                                      request=request))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2))
                self.assertNotEqual(worker_ids, [threading.get_ident()])
                task.cancel()
                await asyncio.wait_for(asyncio.sleep(.01), .2)
                self.assertFalse(task.done(), 'Request ownership must outlive its claim worker')
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            forwarded.assert_not_called()
