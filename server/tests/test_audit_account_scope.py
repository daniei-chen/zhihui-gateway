"""Credential placement stays bound to the authorized WorkBuddy account group."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import config, db, security, upstreamsvc
from server.routers import accounts
from server.tests.authorization_test_support import install_users, owner


READY = {'status': 'ready', 'uid': 'synthetic-audit-scope', 'nickname': 'Synthetic',
         'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
         'realm': 'cn', 'expires_at': 1900000000}


class AccountAuthorizationScopeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous_db = db._conn
        db._conn = None
        self.addCleanup(self.close_db)
        for name, value in [('DATA_DIR', self.root), ('DB_PATH', self.root / 'audit.db'),
                            ('USERS_FILE', self.root / 'users.json'),
                            ('AUTH_DIR', self.root / 'default-auths')]:
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        install_users('alice')
        self.user = owner('alice')
        self.group_dir = self.root / 'selected-auths'
        self.group = upstreamsvc.create_upstream(name='selected', base_url='http://selected.invalid',
                                                 auth_dir=str(self.group_dir))
        self.group_id = self.group['id']
        for name in ('_login_scopes', '_login_bindings', '_login_locks', '_login_ready',
                     '_login_polls', '_login_tasks', '_login_owner', '_login_regions',
                     '_provisioned_states', '_login_group_bindings'):
            if hasattr(accounts, name):
                mapping = getattr(accounts, name)
                mapping.clear(); self.addCleanup(mapping.clear)
        for obj, name, replacement in [
            (accounts.tencent, 'checkin', mock.AsyncMock(return_value=(0, 'synthetic success'))),
            (accounts.reload, 'request_reload_or_restart', mock.Mock(return_value=None)),
            (accounts, '_schedule_revive_after_login', mock.Mock(return_value=None)),
            (accounts, '_poll_login_background', mock.AsyncMock(return_value=None)),
            (security, 'audit', mock.Mock(return_value=None)),
        ]:
            patch = mock.patch.object(obj, name, replacement)
            patch.start(); self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(accounts.router)
        app.dependency_overrides[security.current_user] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def close_db(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self.previous_db

    def bind(self, upstream_id=None, state='synthetic-state'):
        accounts._bind_login(state, self.user, 'cn', upstream_id)
        return state

    async def poll(self, state, upstream_id=None):
        return await accounts.auth_poll(state, realm='cn', region=None,
                                        upstream_id=upstream_id, user=self.user, request=None)

    def assert_no_credentials(self):
        self.assertEqual(list(self.root.rglob('workbuddy*.json')), [])

    def test_forwarding_only_group_rejects_start_before_provider_call(self):
        upstreamsvc.update_upstream(self.group_id, {'auth_dir': ''})
        with mock.patch.object(accounts.tencent, 'start_login', mock.AsyncMock(return_value={
            'state': 'synthetic-start', 'realm': 'cn', 'authUrl': 'https://copilot.tencent.com/'
        })) as start:
            response = self.client.post('/api/auth/start', params={'upstream_id': self.group_id},
                                        json={'realm': 'cn'})
        self.assertEqual(response.status_code, 409, response.text)
        start.assert_not_awaited()
        self.assert_no_credentials()

    def test_directory_removed_after_authorization_never_falls_back_to_default_pool(self):
        state = self.bind(self.group_id)
        upstreamsvc.update_upstream(self.group_id, {'auth_dir': ''})
        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(return_value=dict(READY))) as poll:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.poll(state, self.group_id))
        self.assertEqual(caught.exception.status_code, 409)
        poll.assert_not_awaited()
        self.assert_no_credentials()

    def test_group_changes_while_provider_is_pending_cannot_redirect_final_save(self):
        for mutation in ('deleted', 'directory', 'endpoint'):
            with self.subTest(mutation=mutation):
                group = upstreamsvc.create_upstream(name=mutation, base_url='http://before.invalid',
                    auth_dir=str(self.root / mutation))
                state = self.bind(group['id'], 'state-' + mutation)

                async def ready(_state, _realm):
                    if mutation == 'deleted':
                        upstreamsvc.delete_upstream(group['id'])
                    else:
                        patch = {'auth_dir': str(self.root / 'other-pool')} if mutation == 'directory' else {
                            'base_url': 'http://another-engine.invalid'}
                        upstreamsvc.update_upstream(group['id'], patch)
                    return dict(READY)

                with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(side_effect=ready)):
                    with self.assertRaises(HTTPException) as caught:
                        asyncio.run(self.poll(state, group['id']))
                self.assertEqual(caught.exception.status_code, 404 if mutation == 'deleted' else 409)
                self.assert_no_credentials()

    def test_group_change_during_authorization_start_cannot_rebind_the_returned_grant(self):
        async def start(*_args):
            upstreamsvc.update_upstream(self.group_id, {'auth_dir': str(self.root / 'changed-during-start')})
            return {'state': 'synthetic-start', 'realm': 'cn', 'authUrl': 'https://copilot.tencent.com/'}

        with mock.patch.object(accounts.tencent, 'start_login', mock.AsyncMock(side_effect=start)):
            response = self.client.post('/api/auth/start', params={'upstream_id': self.group_id},
                                        json={'realm': 'cn'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn('synthetic-start', accounts._login_scopes)
        self.assert_no_credentials()

    def test_directory_symlink_retargeting_invalidates_bound_group(self):
        a, b, link = self.root / 'a', self.root / 'b', self.root / 'selected-link'
        a.mkdir(); b.mkdir(); link.symlink_to(a, target_is_directory=True)
        upstreamsvc.update_upstream(self.group_id, {'auth_dir': str(link)})
        state = self.bind(self.group_id)
        link.unlink(); link.symlink_to(b, target_is_directory=True)
        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(return_value=dict(READY))) as poll:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.poll(state, self.group_id))
        self.assertEqual(caught.exception.status_code, 409)
        poll.assert_not_awaited()
        self.assert_no_credentials()

    def test_final_save_uses_bound_group_even_when_no_snapshot_is_supplied(self):
        state = self.bind(self.group_id)
        result = accounts._save_and_finish(dict(READY), 'cn', '', state, owner=self.user)
        self.assertEqual(result['status'], 'success')
        self.assertTrue((self.group_dir / result['file']).is_file())
        self.assertFalse((config.AUTH_DIR / result['file']).exists())

    def test_default_group_none_and_zero_aliases_remain_compatible(self):
        for start_id, poll_id in ((None, 0), (0, None)):
            state = self.bind(start_id, f'default-{start_id}')
            with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(return_value=dict(READY))):
                result = asyncio.run(self.poll(state, poll_id))
            self.assertEqual(result['status'], 'success')
            self.assertTrue((config.AUTH_DIR / result['file']).is_file())
        self.assertEqual(len(list(config.AUTH_DIR.glob('workbuddy*.json'))), 1)

    def test_unchanged_group_and_harmless_name_change_preserve_correct_placement(self):
        state = self.bind(self.group_id)
        upstreamsvc.update_upstream(self.group_id, {'name': 'renamed-only'})
        with mock.patch.object(accounts.tencent, 'poll_login', mock.AsyncMock(return_value=dict(READY))):
            result = asyncio.run(self.poll(state, self.group_id))
        self.assertEqual(result['status'], 'success')
        self.assertTrue((self.group_dir / result['file']).is_file())
        self.assertFalse(config.AUTH_DIR.exists())


if __name__ == '__main__':
    unittest.main()
