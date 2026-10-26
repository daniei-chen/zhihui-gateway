from unittest.mock import AsyncMock

from test_api import ApiTestBase


class PublicReadinessContractTest(ApiTestBase):
    def test_ready_response_is_minimal_and_has_real_status(self):
        response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'ok': True})

    def test_unavailable_source_returns_503_without_addresses(self):
        self.app.state.rt.qoder.health = AsyncMock(return_value={
            'up': False, 'last_error': 'private upstream address', 'latency_ms': 0})
        response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {'ok': False})
        self.assertNotIn('private', response.text)
