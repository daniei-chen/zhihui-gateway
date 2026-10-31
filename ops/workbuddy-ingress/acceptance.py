"""Synthetic native-ELF acceptance; run only in the network-none test container."""
import argparse
import hashlib
import http.client
import http.server
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if self.path.endswith('/chat/completions'):
            type(self).calls += 1
            if any(message.get('content') == 'fixture-hold' for message in value.get('messages', [])):
                self.entered.set()
                if not self.release.wait(8):
                    raise RuntimeError('bounded fixture upstream was not released')
            events = [
                {'id': 'fixture', 'object': 'chat.completion.chunk', 'choices': [
                    {'index': 0, 'delta': {'role': 'assistant', 'content': 'fixture-ok'}, 'finish_reason': None}]},
                {'id': 'fixture', 'object': 'chat.completion.chunk', 'choices': [
                    {'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2, 'credit': 0}},
            ]
            raw = b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events) + b'data: [DONE]\n\n'
            content_type = 'text/event-stream'
        else:
            raw = b'{"code":0,"data":{"list":[]}}'
            content_type = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def verify(binary: Path, expected: str) -> dict:
    if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
        raise ValueError('exact candidate binary required')
    rows = []
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    upstream.daemon_threads = True
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='zhihui-body-fixture-') as temp:
        root = Path(temp)
        (root / 'auths').mkdir()
        (root / 'data').mkdir()
        (root / 'auths/workbuddy-fixture.json').write_text(json.dumps({
            'accessToken': 'fixture-upstream', 'uid': 'fixture-global', 'expiresAt': 4102444800,
            'realm': 'global', 'domain': 'www.workbuddy.ai'}), encoding='utf-8')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        base = 'http://127.0.0.1:' + str(upstream.server_port)
        config = {'listen': '127.0.0.1:' + str(port), 'api_key': 'fixture-client',
                  'auth_dir': str(root / 'auths'), 'state_file': str(root / 'data/state.json'),
                  'ingress': {'max_body_mb': 1, 'max_buffered_body_mb': 2, 'max_concurrent_bodies': 1},
                  'admin': {'enabled': False}, 'upstash': {'url': '', 'token': ''},
                  'session_sticky': {'enabled': False},
                  'global': {'enabled': True, 'chat_base': base, 'billing_base': base},
                  'upstream': {'gzip_body': False, 'timeout_seconds': 8, 'header_timeout_seconds': 8,
                               'attempt_timeout_seconds': 8, 'idle_timeout_seconds': 8},
                  'pool': {'skip_exhausted': False},
                  'schedule': {name + '_enabled': False for name in
                               ('checkin', 'travel', 'activity', 'keepalive', 'school', 'cat')}}
        path = root / 'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')

        def request(method, path, value=None, *, key=True, chunked=False):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            raw = json.dumps(value).encode() if isinstance(value, dict) else value
            headers = {'Content-Type': 'application/json', 'Connection': 'close'}
            if key:
                headers['Authorization'] = 'Bearer fixture-client'
            if chunked:
                payload = raw
                raw = (payload[start:start + 16384] for start in range(0, len(payload), 16384))
            try:
                conn.request(method, path, body=raw, headers=headers, encode_chunked=chunked)
                response = conn.getresponse()
                return response.status, response.read(2 << 20), dict(response.getheaders())
            finally:
                conn.close()

        def headers_only(key, declared):
            # No body is sent. A vulnerable implementation waits instead of
            # returning the final rejection before any 100 Continue response.
            with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
                auth = 'Authorization: Bearer fixture-client\r\n' if key else ''
                raw = (f'POST /v1/chat/completions HTTP/1.1\r\nHost: fixture\r\n{auth}'
                       f'Content-Length: {declared}\r\nExpect: 100-continue\r\nConnection: close\r\n\r\n')
                sock.sendall(raw.encode())
                first = sock.makefile('rb').readline(4096)
                return int(first.split()[1])

        process = subprocess.Popen([str(binary), '-config', str(path)], cwd=root,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   env={'PATH': '/usr/bin:/bin', 'HOME': temp, 'TMPDIR': temp, 'GOMAXPROCS': '2'})
        try:
            for _ in range(80):
                if process.poll() is not None:
                    raise AssertionError('synthetic engine exited')
                try:
                    if request('GET', '/healthz', key=False)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(.05)
            else:
                raise AssertionError('synthetic engine readiness timeout')
            assert headers_only(False, 2 << 20) == 401
            rows.append('unauthenticated_headers_only_401')
            assert headers_only(True, 2 << 20) == 413
            rows.append('authenticated_oversize_headers_only_413')
            status, raw, _ = request('POST', '/v1/chat/completions', b'x' * (1048576 + 1), chunked=True)
            assert status == 413 and json.loads(raw)['error']['code'] == 'request_too_large'
            assert Upstream.calls == 0
            rows.append('actual_chunked_limit_no_provider_io')
            assert request('GET', '/status', key=False)[0] == 401
            for stream in (False, True):
                status, raw, _ = request('POST', '/v1/chat/completions', {
                    'model': 'global:gpt-5.4', 'stream': stream,
                    'messages': [{'role': 'user', 'content': 'fixture-small'}]})
                assert status == 200 and b'fixture-ok' in raw
                if stream:
                    assert b'[DONE]' in raw
                rows.append('stream' if stream else 'nonstream')
            answer = []
            failures = []

            def hold():
                try:
                    answer.append(request('POST', '/v1/chat/completions', {
                        'model': 'global:gpt-5.4', 'stream': True,
                        'messages': [{'role': 'user', 'content': 'fixture-hold'}]}))
                except BaseException as error:
                    failures.append(type(error).__name__)

            worker = threading.Thread(target=hold)
            worker.start()
            try:
                assert Upstream.entered.wait(3), 'provider hold did not start'
                status, raw, _ = request('GET', '/status')
                body = json.loads(raw)['ingress']
                assert status == 200 and body['active_bodies'] == 1 and body['reserved_body_bytes'] > 0
                assert body['max_body_bytes'] == 1 << 20
                assert body['max_buffered_body_bytes'] == 2 << 20
                assert body['max_concurrent_bodies'] == 1
                status, raw, headers = request('POST', '/v1/chat/completions', b'{}')
                assert status == 503 and json.loads(raw)['error']['code'] == 'body_capacity_exceeded'
                assert headers.get('Retry-After') == '1'
                rows.append('lease_retained_during_forwarding_and_saturation_503')
            finally:
                Upstream.release.set()
                worker.join(timeout=10)
            assert not worker.is_alive() and not failures and answer[0][0] == 200
            status, raw, _ = request('GET', '/status')
            state = json.loads(raw)
            assert status == 200 and state['ingress']['active_bodies'] == 0
            assert state['ingress']['reserved_body_bytes'] == 0
            assert state['ingress']['capacity_rejections_total'] == 1
            assert state['disabled'] == 0 and state['cooling'] == 0 and Upstream.calls == 3
            rows.append('capacity_released_and_no_account_penalty')
            assert not (root / 'data/last_request.json').exists()
        finally:
            Upstream.release.set()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            upstream.shutdown()
            upstream.server_close()
    return {'checks': rows, 'passed': len(rows), 'binary_sha256': expected,
            'synthetic_provider_calls': Upstream.calls, 'real_provider_calls': False,
            'production_data_mounted': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.binary.absolute(), args.sha256), indent=2))
