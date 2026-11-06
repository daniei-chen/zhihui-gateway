import asyncio
from pathlib import Path
import tempfile
from unittest.mock import patch

from server import observability as obs
from server.sharedadmission import Coordinator
from server.tests.test_admission_bytes import AdmissionBytesTest


class SharedMiddlewareTest(AdmissionBytesTest):
    """Replay the real body's aggregate/cancellation contracts in shared mode."""
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.peers = []
        real = obs.RequestAuditMiddleware
        def construct(app):
            name = 'peer-' + str(len(self.peers))
            peer = Coordinator(Path(self.tmp.name) / 'admission.db', {
                'active': 8, 'large': 8, 'bytes': 1024**2, 'preauth': 8,
                'queue': 64, 'queue_per_key': 4, 'key_default': 4, 'key_quota': 1},
                owner=lambda: name, alive=lambda _: None)
            self.peers.append(peer)
            self.addCleanup(peer.close)
            return real(app, shared=peer)
        self.factory = construct
        item = patch.object(obs, 'RequestAuditMiddleware', side_effect=construct)
        item.start()
        self.addCleanup(item.stop)

    async def test_two_middleware_instances_share_raw_body_limit(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def application(scope, receive, send):
            await receive()
            started.set()
            await release.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        old, new = self.factory(application), self.factory(application)
        first = asyncio.create_task(self.drive(old, b'x' * 700000, 700000))
        await started.wait()
        try:
            messages, reads = await self.drive(new, b'y' * 600000, 600000)
            self.assertEqual(messages[0]['status'], 503)
            self.assertEqual(reads, [])
            self.assertEqual(new.shared.snapshot()['body_bytes'], 700000)
        finally:
            release.set()
            await first
        self.assertEqual(old.shared.snapshot()['leases'], 0)

    async def test_peer_release_wakes_other_instance_queue(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def application(scope, receive, send):
            await receive()
            if not started.is_set():
                started.set()
                await release.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        old, new = self.factory(application), self.factory(application)
        first = asyncio.create_task(self.drive(old, b'x' * 700000, 700000))
        await started.wait()
        queued = asyncio.create_task(self.drive(new, b'y' * 600000, 600000))
        await asyncio.sleep(0.1)
        self.assertFalse(queued.done())
        release.set()
        self.assertEqual((await first)[0][0]['status'], 200)
        self.assertEqual((await queued)[0][0]['status'], 200)
        self.assertEqual(new.shared.snapshot()['leases'], 0)
