"""Real Nginx -> candidate ASGI -> synthetic provider, private rootfs only.

The outer harness must create a mount/net/PID-isolated chroot and bind runtime
read-only. This script refuses a host filesystem and never uses real identities,
credentials, provider network or production Nginx configuration.
"""
import asyncio
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import httpx
import uvicorn
from fastapi import Request, Response


def port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


class EdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server.tests.test_audit_20261029_gateway_ledger import _Fixture, GOOD
        cls.fixture = _Fixture()
        cls.fixture.setUp()
        cls.provider_calls = []
        def provider(request):
            cls.provider_calls.append(request.url.path)
            return httpx.Response(200, json=GOOD)
        cls.transport = cls.fixture.mock_transport(provider)
        cls.transport.__enter__()
        cls.key = cls.fixture.key()
        cls.hold = threading.Event()
        app = cls.fixture.app

        @app.get('/api/system/internal-session-verify')
        def session(request: Request):
            return Response(status_code=204 if request.cookies.get('fixture-session') == 'synthetic' else 401)

        @app.get('/api/fixture')
        def panel():
            return {'synthetic': True}

        @app.get('/v1/edge-ping')
        def ping():
            return {'synthetic': True}

        @app.get('/v1/hold')
        async def hold():
            while not cls.hold.is_set():
                await asyncio.sleep(.05)
            return {'synthetic': True}

        @app.get('/readyz')
        def ready():
            # Transport fixture: real cached-readiness semantics are covered by
            # Manager tests. This is deliberately not a provider health probe.
            return {'ok': True}

        cls.backend_port = port()
        cls.backend = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=cls.backend_port,
                                                   log_level='error', access_log=False, lifespan='off'))
        cls.backend_thread = threading.Thread(target=cls.backend.run, daemon=True)
        cls.backend_thread.start()
        deadline = time.monotonic() + 10
        while not cls.backend.started:
            if not cls.backend_thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('synthetic backend startup failed')
            time.sleep(.02)

    @classmethod
    def tearDownClass(cls):
        cls.hold.set()
        cls.backend.should_exit = True
        cls.backend_thread.join(15)
        if cls.backend_thread.is_alive():
            raise RuntimeError('synthetic ASGI process did not stop')
        cls.transport.__exit__(None, None, None)
        cls.fixture.tearDown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='edge-fixture-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.port = port()
        self.hold.clear()
        sources = Path('/lab/ops/nginx')
        location = f'''proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header Connection "";
proxy_buffering off;
proxy_read_timeout 20s;
include {sources}/gateway-admission.location.conf;
'''
        conf = f'''user root root;
worker_processes 1;
daemon off;
master_process off;
pid {self.root}/nginx.pid;
error_log {self.root}/error.log warn;
events {{ worker_connections 512; }}
http {{
    include {sources}/gateway-admission.http.conf;
    access_log off;
    client_body_temp_path {self.root}/body;
    proxy_temp_path {self.root}/proxy;
    fastcgi_temp_path {self.root}/fastcgi;
    scgi_temp_path {self.root}/scgi;
    uwsgi_temp_path {self.root}/uwsgi;
    upstream zhihui_mgr {{ server 127.0.0.1:{self.backend_port}; }}
    server {{
        listen 127.0.0.1:{self.port};
        server_name fixture.local;
        include {sources}/gateway-admission.server.conf;
        location = /__zh_session {{
            internal;
            proxy_pass http://zhihui_mgr/api/system/internal-session-verify;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_set_header Cookie $http_cookie;
        }}
        location /api/ {{
            auth_request /__zh_session;
            error_page 401 = @zh_panel_unauthenticated;
            proxy_pass http://zhihui_mgr;
        }}
        location /v1/ {{ proxy_pass http://zhihui_mgr; {location} }}
        location ^~ /qoder/v1/ {{ proxy_pass http://zhihui_mgr/qoder/v1/; proxy_set_header X-QTG-Platform qoder; {location} }}
        location ^~ /trae/v1/ {{ proxy_pass http://zhihui_mgr/trae/v1/; proxy_set_header X-QTG-Platform trae; {location} }}
        location /wb/ {{ proxy_pass http://zhihui_mgr/; {location} }}
    }}
}}
'''
        self.conf = self.root / 'nginx.conf'
        self.conf.write_text(conf)
        check = subprocess.run(['/usr/sbin/nginx', '-t', '-p', str(self.root), '-c', str(self.conf)],
                               capture_output=True, timeout=5)
        self.assertEqual(check.returncode, 0, check.stderr.decode())
        self.nginx = subprocess.Popen(['/usr/sbin/nginx', '-p', str(self.root), '-c', str(self.conf)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 5
        while True:
            if self.nginx.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('private nginx startup failed: ' + (self.root / 'error.log').read_text())
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                    break
            except OSError:
                time.sleep(.02)

    def stop(self):
        self.hold.set()
        self.nginx.terminate()
        self.nginx.wait(timeout=5)

    def request(self, path, *, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def headers_protected(self, headers):
        lower = {key.lower(): value for key, value in headers.items()}
        for name in ('content-security-policy', 'strict-transport-security', 'x-frame-options',
                     'x-content-type-options', 'referrer-policy', 'permissions-policy', 'cache-control'):
            self.assertIn(name, lower)
        self.assertNotIn("'unsafe-eval'", lower['content-security-policy'])

    def test_public_readiness_panel_gate_and_retired_callback(self):
        before = len(self.provider_calls)
        status, headers, body = self.request('/readyz')
        self.assertEqual((status, body), (200, {'ok': True}))
        self.headers_protected(headers)
        for path in ('/api/fixture', '/api/claim/synthetic-invalid'):
            with self.subTest(path=path):
                status, headers, value = self.request(path)
                self.assertEqual(status, 401)
                self.assertEqual(value['error']['code'], 'session_required')
                self.headers_protected(headers)
        self.assertEqual(self.request('/api/fixture', headers={'Cookie': 'fixture-session=synthetic'})[0], 200)
        for path in ('/trae-callback-test', '/trae-callback-test/?AuthCode=synthetic-private-code'):
            status, headers, value = self.request(path)
            self.assertEqual(status, 410)
            self.headers_protected(headers)
        self.assertEqual(len(self.provider_calls), before)
        audit = Path('/var/log/nginx/workbuddy-audit.log').read_text()
        self.assertNotIn('synthetic-private-code', audit)
        self.assertIn('/trae-callback-test/', audit)

    def test_invalid_key_headers_reject_before_complete_body_upload(self):
        before = len(self.provider_calls)
        for prefix in ('/v1', '/qoder/v1', '/trae/v1', '/wb/v1'):
            for suffix in ('chat/completions', 'messages', 'responses'):
                with self.subTest(prefix=prefix, suffix=suffix):
                    connection = socket.create_connection(('127.0.0.1', self.port), timeout=3)
                    with connection:
                        connection.sendall((f'POST {prefix}/{suffix} HTTP/1.1\r\nHost: fixture.local\r\n'
                                            'Content-Type: application/json\r\nContent-Length: 1048576\r\n'
                                            'Authorization: Bearer wbk_synthetic_invalid\r\n\r\n').encode())
                        response = http.client.HTTPResponse(connection)
                        response.begin()
                        self.assertEqual(response.status, 401)
                        value = json.loads(response.read())
                        self.assertEqual(value['error']['code'], 'invalid_api_key')
        self.assertEqual(len(self.provider_calls), before)
        self.assertFalse(any(path.is_file() for path in (self.root / 'body').rglob('*')))

    def test_real_candidate_three_protocols_and_four_ingress_paths(self):
        before = len(self.provider_calls)
        for prefix in ('', '/qoder', '/trae', '/wb'):
            for protocol in ('chat/completions', 'messages', 'responses'):
                payload = self.fixture.payload(protocol)
                status, _headers, value = self.request(prefix + '/v1/' + protocol, method='POST',
                    body=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + self.key['key'],
                    'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'})
                self.assertEqual(status, 200, value)
        self.assertEqual(len(self.provider_calls) - before, 12)

    def test_oversized_edge_body_is_json_without_upstream_effect(self):
        before = len(self.provider_calls)
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        with connection:
            connection.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: fixture.local\r\n'
                               b'Content-Length: 68157440\r\nConnection: close\r\n\r\n')
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 413)
            self.headers_protected(dict(response.getheaders()))
            self.assertEqual(json.loads(response.read())['error']['code'], 'payload_too_large')
        self.assertEqual(len(self.provider_calls), before)

    def test_rate_limit_is_shared_and_returns_stable_json(self):
        # Pipelining keeps the bounded burst below the replenishment window.
        paths = ('/v1/edge-ping', '/wb/v1/edge-ping')
        raw = b''.join((f'GET {paths[index % 2]} HTTP/1.1\r\nHost: fixture.local\r\n'
                        f'Connection: {"close" if index == 179 else "keep-alive"}\r\n\r\n').encode()
                       for index in range(180))
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as connection:
            connection.sendall(raw)
            reply = bytearray()
            while chunk := connection.recv(65536):
                reply.extend(chunk)
                if len(reply) > 1024 * 1024:
                    raise AssertionError('unexpectedly large private response')
        statuses = re.findall(rb'HTTP/1.1 (\d{3})', reply)
        self.assertEqual(len(statuses), 180)
        self.assertIn(b'200', statuses)
        self.assertIn(b'429', statuses)
        self.assertIn(b'"code":"edge_capacity_exceeded"', reply)
        self.assertIn(b'Retry-After: 1', reply)

    def test_connection_limit_cannot_hold_unbounded_slow_requests(self):
        connections = []
        try:
            for _ in range(65):
                connection = socket.create_connection(('127.0.0.1', self.port), timeout=3)
                connection.sendall(b'GET /v1/hold HTTP/1.1\r\nHost: fixture.local\r\nConnection: close\r\n\r\n')
                connections.append(connection)
                time.sleep(.04)  # Stay below rate policy to isolate the connection ceiling.
            # The last request must be rejected before any held operation ends.
            response = http.client.HTTPResponse(connections[-1])
            response.begin()
            self.assertEqual(response.status, 429)
            self.assertEqual(json.loads(response.read())['error']['code'], 'edge_capacity_exceeded')
            self.headers_protected(dict(response.getheaders()))
        finally:
            self.hold.set()
            for connection in connections:
                connection.close()


def main():
    witness = json.loads(Path('/run/zh-edge-isolated-fixture.json').read_bytes())
    info = Path('/').stat()
    if witness != {'schema': 'zhihui.edge-private-fixture', 'root_identity': [info.st_dev, info.st_ino]}:
        raise ValueError('private edge rootfs witness required')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EdgeTests))
    print('EDGE-LAB-RESULT ' + json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
          'errors': len(result.errors), 'skipped': len(result.skipped),
          'synthetic_provider_requests': len(EdgeTests.provider_calls), 'production_modified': False}))
    raise SystemExit(not result.wasSuccessful() or bool(result.skipped))


if __name__ == '__main__':
    main()
