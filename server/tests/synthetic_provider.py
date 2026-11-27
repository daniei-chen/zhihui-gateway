"""Small, loopback-only HTTP provider for the deterministic regression gate.

No provider credentials, remote endpoints, model calls or production data are
used. The context owns the listener, request counters and environment lifetime.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
from urllib.parse import urlsplit


MAX_BODY_BYTES = 4 * 1024 * 1024


class _Provider(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def _count(self):
        with self.server.stats_lock:
            self.server.total += 1
            path = urlsplit(self.path).path
            self.server.by_path[path] = self.server.by_path.get(path, 0) + 1

    def _reply(self, body, status=200, content_type='application/json'):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/__stats':
            with self.server.stats_lock:
                stats = {'total': self.server.total, 'by_path': dict(self.server.by_path)}
            self._reply(stats)
        elif path == '/v1/models':
            self._count()
            self._reply({'object': 'list', 'data': [
                {'id': 'fake-model', 'object': 'model', 'owned_by': 'synthetic'}]})
        elif path == '/status':
            self._count()
            self._reply({'connected': True, 'total': 1, 'healthy': 1,
                         'cooling': 0, 'disabled': 0, 'accounts': []})
        else:
            self._reply({'error': {'message': 'Unknown synthetic route'}}, 404)

    def do_POST(self):
        if urlsplit(self.path).path != '/v1/chat/completions':
            self._reply({'error': {'message': 'Unknown synthetic route'}}, 404)
            return
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if length < 0 or self.headers.get('Transfer-Encoding'):
            self._reply({'error': {'message': 'Content-Length required'}}, 411)
            return
        if length > MAX_BODY_BYTES:
            self._reply({'error': {'message': 'Synthetic body limit'}}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
        except (ValueError, UnicodeError):
            self._reply({'error': {'message': 'Invalid synthetic JSON'}}, 400)
            return
        self._count()
        model = body.get('model', 'fake-model')
        if model == 'fake-model:accepted_disconnect':
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if model == 'fake-model:refusal':
            self._reply({'error': {'type': 'permission_error', 'code': 'content_policy_rejected',
                                  'message': 'Explicit provider refusal'}}, 403)
            return
        if model == 'fake-model:error_500':
            self._reply({'error': {'type': 'server_error', 'code': 'synthetic_upstream_error',
                                  'message': 'Synthetic upstream error'}}, 500)
            return
        content = json.dumps(body, ensure_ascii=False) if model == 'fake-model:echo' else 'synthetic answer'
        response = {'id': 'synthetic-chat', 'object': 'chat.completion', 'model': model,
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5, 'credit': 0.01}}
        if body.get('stream'):
            response['object'] = 'chat.completion.chunk'
            response['choices'] = [{'index': 0, 'delta': {'content': content}, 'finish_reason': 'stop'}]
            raw = ('data: ' + json.dumps(response, ensure_ascii=False) + '\n\ndata: [DONE]\n\n').encode()
            self._reply(raw, content_type='text/event-stream')
        else:
            self._reply(response)


@contextmanager
def synthetic_provider():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Provider)
    server.daemon_threads = True
    server.total = 0
    server.by_path = {}
    server.stats_lock = threading.Lock()
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    base = f'http://127.0.0.1:{server.server_port}'
    values = {'OPENAI_BASE_URL': base + '/v1', 'ZH_FAKE_PROVIDER_BASE': base}
    previous = {key: os.environ.get(key) for key in values}
    worker.start()
    os.environ.update(values)
    try:
        yield {'base_url': base, 'loopback_only': True, 'synthetic': True}
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        if worker.is_alive():
            raise RuntimeError('synthetic provider failed to stop')
