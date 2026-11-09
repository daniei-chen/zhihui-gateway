import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.services import credentiallease, credentialrefresh, tencent, wb2api


class CredentialRefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'workbuddy-synthetic.json'
        self.document = {'auth': {'accessToken': 'synthetic-old', 'refreshToken': 'synthetic-refresh'},
                         'account': {'uid': 'synthetic-user'}, 'proxy': 'synthetic-route',
                         'weight': 3, 'extra': {'preserve': True}}
        self.path.write_text(json.dumps(self.document), encoding='utf-8')
        self.payload = {'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh',
                        'uid': 'synthetic-user'}

    async def test_success_and_failure_are_singleflight(self):
        for success in (True, False):
            with self.subTest(success=success):
                self.path.write_text(json.dumps(self.document), encoding='utf-8')
                entered, release = asyncio.Event(), asyncio.Event()
                calls = []

                async def upstream(payload):
                    calls.append(True)
                    entered.set()
                    await release.wait()
                    return success, 'synthetic result', {'access_token': 'synthetic-new'} if success else {}

                with mock.patch.object(tencent, 'refresh_token', upstream):
                    tasks = [asyncio.create_task(credentialrefresh.refresh(self.path.name, self.payload, self.root))
                             for index in range(32)]
                    await entered.wait()
                    await asyncio.sleep(0.02)
                    release.set()
                    results = await asyncio.gather(*tasks)
                self.assertEqual(len(calls), 1)
                self.assertTrue(all(result[0] == success for result in results))
                actual = json.loads(self.path.read_text(encoding='utf-8'))
                for key in ('proxy', 'weight', 'extra'):
                    self.assertEqual(actual[key], self.document[key])

    async def test_read_allowed_but_unrelated_write_refuses_without_blocking(self):
        async with credentiallease.acquire(self.path):
            self.assertEqual(wb2api.read_account_file(self.path.name, self.root)['account']['uid'], 'synthetic-user')

            async def other_writer():
                with self.assertRaises(OSError):
                    wb2api.set_account_weight(self.path.name, 7, self.root)

            await asyncio.wait_for(asyncio.create_task(other_writer()), 0.5)
        self.assertEqual(json.loads(self.path.read_text())['weight'], 3)

    async def test_waiter_cancel_preserves_shared_persistence(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def upstream(payload):
            entered.set()
            await release.wait()
            return True, 'synthetic result', {'access_token': 'synthetic-new'}

        with mock.patch.object(tencent, 'refresh_token', upstream):
            waiter = asyncio.create_task(credentialrefresh.refresh(self.path.name, self.payload, self.root))
            await entered.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            other = asyncio.create_task(credentialrefresh.refresh(self.path.name, self.payload, self.root))
            release.set()
            self.assertTrue((await other)[0])
        self.assertEqual(json.loads(self.path.read_text())['auth']['accessToken'], 'synthetic-new')

    async def test_stale_generation_cannot_overwrite_newer_credentials(self):
        expected = credentialrefresh.generation(self.document['auth'])
        changed = copy.deepcopy(self.document)
        changed['auth']['accessToken'] = 'synthetic-manual-new'
        self.path.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            tencent.update_auth_tokens(self.path.name, {'access_token': 'synthetic-stale',
                                                       '_refresh_expected': expected}, self.root)
        with mock.patch.object(tencent, 'refresh_token', side_effect=AssertionError('must not refresh')):
            result = await credentialrefresh.refresh(self.path.name, self.payload, self.root)
        self.assertTrue(result[0])
        self.assertEqual(json.loads(self.path.read_text()), changed)
