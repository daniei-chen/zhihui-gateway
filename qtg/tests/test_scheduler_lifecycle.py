"""No hardcoded state paths, no upstream side effect without a durable claim."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from qtg import checkin_scheduler as scheduler
from identity_support import build_settings


class SchedulerOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.settings=build_settings(self.root)
        self.data=Path(self.settings.data_dir)
        self.data.mkdir(parents=True,exist_ok=True)
        self.rt=SimpleNamespace(settings=self.settings,
            qoder=SimpleNamespace(checkin=AsyncMock(return_value={'ok':True})),
            trae=SimpleNamespace(checkin=AsyncMock(return_value={'ok':True})))

    def due_state(self):
        value=scheduler._fresh_state()
        value['targets']={name:0 for name in ('qoder','trae')}
        path=self.data/'random_checkin.json'
        path.write_text(json.dumps(value));return path

    async def tick(self):
        with patch.object(scheduler.asyncio,'sleep',new=AsyncMock(side_effect=[None,asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler._loop(self.rt)

    async def test_disabled_scheduler_neither_starts_a_task_nor_creates_state(self):
        self.rt.settings=replace(self.settings,checkin_scheduler_enabled=False)
        self.assertIsNone(scheduler.start(self.rt))
        self.assertFalse((self.data/'random_checkin.json').exists())
        self.rt.qoder.checkin.assert_not_awaited()

    async def test_state_and_plan_use_the_same_configured_data_directory(self):
        path=self.due_state()
        await self.tick()
        data=json.loads(path.read_text())
        self.assertEqual(set(data['done']),{'qoder','trae'})
        self.rt.qoder.checkin.assert_awaited_once()
        self.rt.trae.checkin.assert_awaited_once()
        self.assertEqual(data['attempts'],{'qoder':1,'trae':1})

    async def test_failed_attempt_persistence_prevents_upstream_side_effect(self):
        self.due_state()
        with patch.object(scheduler,'_save',return_value=False):
            await self.tick()
        self.rt.qoder.checkin.assert_not_awaited()
        self.rt.trae.checkin.assert_not_awaited()

    async def test_disabled_plan_never_contacts_upstream(self):
        self.due_state()
        (self.data/'compat-plans.json').write_text(json.dumps({'daily':{'checkin':{'enabled':False}}}))
        await self.tick()
        self.rt.qoder.checkin.assert_not_awaited()
        self.rt.trae.checkin.assert_not_awaited()

    def test_corrupt_unreadable_or_non_boolean_plan_does_not_enable_job(self):
        path=self.data/'compat-plans.json'
        self.assertTrue(scheduler._plans_enabled(path))  # explicit legacy default
        for content in ('bad','[]',json.dumps({'daily':{'checkin':{'enabled':'false'}}})):
            path.write_text(content)
            self.assertFalse(scheduler._plans_enabled(path))
        with patch.object(Path,'read_text',side_effect=PermissionError('QA-only')):
            self.assertFalse(scheduler._plans_enabled(path))

    async def test_lifespan_waits_for_scheduler_cancellation_before_closing_adapters(self):
        from qtg.main import create_app
        events=[]
        async def job():
            try:await asyncio.Event().wait()
            finally:events.append('scheduler_stopped')
        async def shutdown():events.append('adapters_closed')
        runtime=SimpleNamespace(startup=AsyncMock(),shutdown=AsyncMock(side_effect=shutdown))
        with patch('qtg.main.Runtime',return_value=runtime), \
             patch.object(scheduler,'start',side_effect=lambda rt:asyncio.create_task(job())):
            app=create_app(self.settings)
            async with app.router.lifespan_context(app):await asyncio.sleep(0)
        self.assertEqual(events,['scheduler_stopped','adapters_closed'])
