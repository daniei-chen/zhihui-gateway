"""A blocked account-file write must not monopolize the ASGI event loop."""
import asyncio
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from starlette.datastructures import UploadFile
from server.routers import accounts


class UploadResponsiveness(unittest.IsolatedAsyncioTestCase):
    async def test_loop_can_run_while_account_file_write_is_blocked(self):
        entered, unblock, completed = threading.Event(), threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            def write_auth(account, auth_dir, **kwargs):
                entered.set()
                unblock.wait(1.5)
                target = base / 'workbuddy-fairness.json'
                target.write_text('{}', encoding='utf8')
                completed.set()
                return target.name, False
            upload = UploadFile(io.BytesIO(json.dumps({'uid':'fairness','accessToken':'synthetic'}).encode()), filename='one.json')
            group = {'name':'isolated'}
            valid = {'uid':'fairness','proxy':'','device_token':''}
            with patch.object(accounts, '_group', return_value=group), patch.object(accounts, '_require_dir', return_value=base), patch.object(accounts, '_uploaded_account', return_value=valid), patch.object(accounts.tencent, 'write_auth_file', side_effect=write_auth), patch.object(accounts.reload, 'request_reload_or_restart'), patch.object(accounts, '_reload_target', return_value=None), patch.object(accounts.security, 'audit'), patch.object(accounts, 'client_ip', return_value='127.0.0.1'):
                task = asyncio.create_task(accounts.upload_accounts(request=Mock(), files=[upload], upstream_id=None, overwrite=False, user={}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    self.assertFalse(completed.is_set(), 'ASGI loop was blocked until account write completed')
                finally:
                    unblock.set()
                    result = await task
                self.assertEqual(len(result['added']), 1)
                self.assertFalse(result['failed'])
                self.assertEqual(result['added'][0]['uid'], 'fairness')
