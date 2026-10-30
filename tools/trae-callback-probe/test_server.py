"""Pure and private-loopback tests; no providers or production Manager calls."""
import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

spec = importlib.util.spec_from_file_location('probe_server', Path(__file__).with_name('server.py'))
probe_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe_server)


class Manager:
    def __init__(self):
        self.calls = []
        self.revoked = False
        self.waiting = False
    def call(self, cookie, method, path, body=None):
        self.calls.append((cookie, method, path, body))
        if self.revoked or cookie not in ('alice-cookie', 'bob-cookie'):
            raise probe_server.Rejected(401)
        if path == '/api/me':
            return {'username': cookie.split('-')[0], 'role': 'account_admin'}
        if path == '/api/auth/review':
            if body.get('state'):
                return {'sessions': [{'status': 'waiting', 'engine_binding': 'current'}]}
            return {'sessions': [{'status': 'waiting'}]} if self.waiting else {'sessions': []}
        if path == '/api/auth/start':
            self.waiting = True
            return {'state': 'zh-' + 'b' * 48, 'platform': 'trae', 'realm': 'cn',
                    'expires_at': 1300, 'mode': 'callback', 'authUrl':
                    'https://www.trae.cn/authorization?' + urlencode({
                        'login_trace_id': 'a' * 32, 'code_challenge_method': 'S256',
                        'code_challenge': 'x' * 43,
                        'auth_callback_url': 'http://127.0.0.1:17388/authorize'})}
        if path == '/api/auth/cancel':
            self.waiting = False
            return {'ok': True}
        raise AssertionError('unexpected effect ' + path)


def callback(**changes):
    return urlencode({'isRedirect': 'true', 'scope': 'trae', 'loginTraceID': 'a' * 32,
                      'host': 'https://api.trae.com.cn', 'userRegion': 'cn',
                      'authCodeInfo': json.dumps({'AuthCode': 'synthetic-private-one-use-code'}),
                      'userInfo': json.dumps({'UserID': 'synthetic-private-user'}), **changes})


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.manager = Manager()
        self.now = 1000
        self.events = []
        self.probe = probe_server.Probe(self.manager, clock=lambda: self.now,
                                        event=lambda: self.events.append('received'))

    def test_only_callback_target_changes_and_no_exchange_capability_exists(self):
        token = self.probe.start('alice-cookie')
        row = self.probe.sessions[token]
        query = parse_qs(urlsplit(row['url']).query)
        self.assertEqual(query['auth_callback_url'], [probe_server.ORIGIN + probe_server.PREFIX + 'return/' + token])
        self.assertEqual(query['code_challenge'], ['x' * 43])
        self.assertEqual(query['login_trace_id'], ['a' * 32])
        self.probe.callback(token, 'alice-cookie', callback())
        self.assertEqual(self.events, ['received'])
        stored = json.dumps(self.probe.sessions)
        self.assertNotIn('synthetic-private', stored)
        self.assertEqual(self.probe.sessions[token]['url'], '')
        self.assertFalse(any('finish' in path for _, _, path, _ in self.manager.calls))
        with self.assertRaises(probe_server.Rejected):
            self.probe.callback(token, 'alice-cookie', callback())
        self.assertEqual(self.events, ['received'])

    def test_foreign_owner_revocation_and_expiry_do_not_record_reception(self):
        token = self.probe.start('alice-cookie')
        with self.assertRaises(probe_server.Rejected):
            self.probe.callback(token, 'bob-cookie', callback())
        self.manager.revoked = True
        with self.assertRaises(probe_server.Rejected):
            self.probe.callback(token, 'alice-cookie', callback())
        self.manager.revoked = False
        self.now = 1301
        with self.assertRaises(probe_server.Rejected):
            self.probe.callback(token, 'alice-cookie', callback())
        self.assertEqual(self.events, [])

    def test_bad_parameters_never_get_accepted(self):
        token = self.probe.start('alice-cookie')
        for query in (callback(scope='other'), callback(isRedirect='false'),
                      callback(host='http://127.0.0.1:8080'), callback(loginTraceID='c' * 32),
                      callback(userRegion='sg'), callback(refreshToken='synthetic-forged'),
                      callback(authCodeInfo='not-json'), callback(authCodeInfo='{}'),
                      callback() + '&scope=trae', 'x=' + 'a' * 16385):
            with self.subTest(query=query[:30]), self.assertRaises(probe_server.Rejected):
                self.probe.callback(token, 'alice-cookie', query)
        self.assertFalse(self.probe.sessions[token]['received'])

    def test_existing_waiting_authorization_is_not_overwritten(self):
        self.manager.waiting = True
        with self.assertRaises(probe_server.Rejected):
            self.probe.start('alice-cookie')
        self.assertFalse(any(path == '/api/auth/start' for _, _, path, _ in self.manager.calls))

    def test_cancel_never_deletes_a_provider_account_and_stops_callback(self):
        token = self.probe.start('alice-cookie')
        self.probe.cancel(token, 'alice-cookie')
        self.assertEqual(self.manager.calls[-1][2], '/api/auth/cancel')
        with self.assertRaises(probe_server.Rejected):
            self.probe.callback(token, 'alice-cookie', callback())

    def test_http_cookie_origin_host_and_sanitized_redirect(self):
        server = probe_server.Server(('127.0.0.1', 0), probe_server.Handler)
        server.probe = self.probe
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, extra=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1])
            headers = {'Host': 'zhihui.kaogong.art', 'Cookie': 'wb_session=alice-cookie'}
            headers.update(extra or {})
            conn.request(method, path, body=b'' if method == 'POST' else None, headers=headers)
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result
        self.assertEqual(request('GET', probe_server.PREFIX)[0], 200)
        self.assertEqual(request('GET', probe_server.PREFIX, {'Host': 'evil.invalid'})[0], 400)
        self.assertEqual(request('POST', probe_server.PREFIX + 'start')[0], 403)
        code, headers, _ = request('POST', probe_server.PREFIX + 'start', {'Origin': probe_server.ORIGIN})
        self.assertEqual(code, 303)
        token = probe_server.cookie_value(headers['Set-Cookie'], probe_server.COOKIE)
        self.assertIn('HttpOnly; Secure; SameSite=Lax', headers['Set-Cookie'])
        route = probe_server.PREFIX + 'return/' + token + '?' + callback()
        self.assertEqual(request('GET', route)[0], 303)
        cookie = 'wb_session=alice-cookie; ' + probe_server.COOKIE + '=' + token
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            code, headers, body = request('GET', route, {'Cookie': cookie})
        self.assertEqual(code, 303)
        self.assertEqual(headers['Location'], probe_server.PREFIX + 'result')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(body, b'')
        self.assertNotIn('synthetic-private', output.getvalue())
        code, headers, body = request('GET', probe_server.PREFIX + 'result', {'Cookie': cookie})
        self.assertEqual(code, 200)
        self.assertIn('未兑换'.encode(), body)
        self.assertNotIn('synthetic-private'.encode(), body)

    def test_duplicate_cookies_and_injected_cookie_are_rejected(self):
        for value in ('wb_session=alice; wb_session=bob', 'wb_session="a;b"', 'wb_session=a\r\nb'):
            with self.subTest(value=value), self.assertRaises(probe_server.Rejected):
                probe_server.cookie_value(value, 'wb_session')

    def test_no_callback_when_manager_started_an_exchange_in_another_tab(self):
        token = self.probe.start('alice-cookie')
        original = self.manager.call
        def changed(cookie, method, path, body=None):
            if path == '/api/auth/review':
                return {'sessions': [{'status': 'processing', 'engine_binding': 'current'}]}
            return original(cookie, method, path, body)
        self.manager.call = changed
        with self.assertRaises(probe_server.Rejected):
            self.probe.callback(token, 'alice-cookie', callback())
        self.assertEqual(self.events, [])

    def test_input_failures_release_busy_flag_and_do_not_create_capability(self):
        original = self.manager.call
        for field, value in (('state', 'zh-not-a-capability'), ('expires_at', float('inf')),
                             ('authUrl', 'https://evil.invalid/authorization')):
            self.manager.waiting = False
            def malformed(cookie, method, path, body=None):
                out = original(cookie, method, path, body)
                if path == '/api/auth/start':
                    out[field] = value
                return out
            self.manager.call = malformed
            with self.subTest(field=field), self.assertRaises(probe_server.Rejected):
                self.probe.start('alice-cookie')
            self.assertFalse(self.probe.busy)
            self.assertEqual(self.probe.sessions, {})

    def test_resource_admission_never_starts_a_ninth_request_thread(self):
        server = probe_server.Server(('127.0.0.1', 0), probe_server.Handler)
        self.addCleanup(server.server_close)
        for _ in range(8):
            self.assertTrue(server.slots.acquire(blocking=False))
        class Request:
            closed = False
            def shutdown(self, *_): pass
            def close(self): self.closed = True
        request = Request()
        server.process_request(request, ('127.0.0.1', 0))
        self.assertTrue(request.closed)
        for _ in range(8):
            server.slots.release()


if __name__ == '__main__':
    unittest.main()
