"""Initial authentication must not be a cross-site form submission target."""
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from server.routers import auth


class LoginOriginTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(auth.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        patcher = mock.patch.object(auth, '_complete_login', return_value=JSONResponse({'ok': True}))
        self.verify = patcher.start()
        self.addCleanup(patcher.stop)

    def test_cross_site_plain_text_json_is_rejected_without_existing_cookie(self):
        # HTML text/plain forms can produce a valid JSON body containing "=".
        response = self.client.post('/api/login',
            content='{"username":"attacker","password":"synthetic","form":"="}',
            headers={'content-type': 'text/plain', 'origin': 'https://other.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers['x-manager-csrf-reason'], 'csrf_origin_mismatch')
        self.assertNotIn('set-cookie', response.headers)
        self.verify.assert_not_called()

    def test_invalid_or_missing_browser_origin_is_rejected(self):
        for headers in ({'origin': 'null'}, {'sec-fetch-site': 'cross-site'},
                        {'referer': 'http://testserver.other.invalid/page'},
                        [('origin', 'http://testserver'), ('origin', 'https://other.invalid')]):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post('/api/login', json={}, headers=headers).status_code, 403)
        self.verify.assert_not_called()

    def test_same_origin_login_and_explicit_non_browser_client_remain_supported(self):
        for headers in ({'origin': 'http://testserver'}, {},
                        {'referer': 'http://testserver/login'}):
            with self.subTest(headers=headers):
                response = self.client.post('/api/login', json={'username': 'fixture', 'password': 'fixture'}, headers=headers)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.verify.call_count, 3)

    def test_cross_site_logout_does_not_clear_a_cookie(self):
        response = self.client.post('/api/logout', headers={'origin': 'https://other.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('set-cookie', response.headers)
        response = self.client.post('/api/logout', headers={'origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Max-Age=0', response.headers['set-cookie'])
