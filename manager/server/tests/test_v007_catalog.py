import asyncio
import unittest
from unittest import mock

from server.services import modelcatalog


class CatalogConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        modelcatalog.invalidate()

    async def test_cold_catalog_coalesces_and_waiter_cancel_does_not_cancel_shared_build(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def build(realm, force=False):
            calls.append((realm, force))
            entered.set()
            await release.wait()
            return {'models': [{'id': 'synthetic-model'}], 'source': 'tencent'}

        with mock.patch.object(modelcatalog, '_build', build):
            tasks = [asyncio.create_task(modelcatalog.catalog('cn')) for index in range(32)]
            await entered.wait()
            tasks[0].cancel()
            await asyncio.sleep(0.01)
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertTrue((await modelcatalog.catalog('cn'))['cached'])
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result['fresh'] for result in results[1:]))

    async def test_invalidated_build_cannot_repopulate_stale_models(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def build(realm, force=False):
            entered.set()
            await release.wait()
            return {'models': [{'id': 'synthetic-old'}], 'source': 'tencent'}

        with mock.patch.object(modelcatalog, '_build', build):
            running = asyncio.create_task(modelcatalog.catalog('cn'))
            await entered.wait()
            modelcatalog.invalidate('cn')
            release.set()
            result = await running
        self.assertTrue(result['superseded'])
        self.assertFalse(result['fresh'])
        self.assertIsNone(modelcatalog.cached_ids('cn'))
