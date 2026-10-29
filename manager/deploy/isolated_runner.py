#!/usr/bin/env python3
"""Run the suite against a private, real loopback HTTP provider and temporary state.

No provider account, API key, production database, or writable source tree is used.
Usage: dev/run_isolated_tests.py [pytest arguments...]
"""
from __future__ import annotations

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


class Provider(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.lock = threading.Lock()
        self.counts = Counter()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def reply(self, status, payload, content_type='application/json'):
        raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        if self.headers.get('Authorization') != 'Bearer isolated-upstream-test':
            self.reply(401, {'error': {'message': 'Invalid fixture credential'}})
            return False
        with self.server.lock:
            self.server.counts[urlsplit(self.path).path] += 1
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/__stats':
            with self.server.lock:
                counts = dict(self.server.counts)
            self.reply(200, {'total': sum(counts.values()), 'by_path': counts})
        elif not self.authorized():
            return
        elif path == '/v1/models':
            self.reply(200, {'object': 'list', 'data': [
                {'id': 'fake-model', 'object': 'model', 'owned_by': 'isolated-test'}]})
        elif path == '/healthz':
            self.reply(200, {'healthy': 1, 'total': 1})
        elif path == '/status':
            self.reply(200, {'accounts': [], 'healthy': 1, 'total': 1})
        else:
            self.reply(404, {'error': {'message': 'Unknown fixture route'}})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if not self.authorized():
            return
        if urlsplit(self.path).path != '/v1/chat/completions':
            self.reply(404, {'error': {'message': 'Unknown fixture route'}})
            return
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self.reply(400, {'error': {'message': 'Invalid fixture JSON'}})
            return
        model = body.get('model', 'fake-model')
        if model.endswith(':slow'):
            time.sleep(0.15)
        if model.endswith(':accepted_disconnect'):
            # Count a fully accepted request, then lose the response. A replay can
            # duplicate billing; the integration test must observe exactly one call.
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if model.endswith(':error_500'):
            self.reply(500, {'error': {'type': 'api_error', 'code': 'fixture_error',
                                     'message': 'Isolated upstream failure'}})
            return
        if model.endswith(':refusal'):
            self.reply(403, {'error': {'type': 'permission_error', 'code': 'content_policy_rejected',
                                     'message': 'Explicit provider refusal', 'gateway_hint': 'Review request'},
                             'request_id': 'fixture-refusal-123'})
            return
        content = json.dumps(body, ensure_ascii=False) if model.endswith(':echo') else 'hello'
        usage = {'prompt_tokens': 17, 'completion_tokens': 5, 'total_tokens': 22}
        base = {'id': 'chatcmpl-isolated', 'model': model, 'created': int(time.time())}
        if body.get('stream'):
            frames = [dict(base, object='chat.completion.chunk', choices=[{
                'index': 0, 'delta': {'role': 'assistant', 'content': 'hello'}, 'finish_reason': None}]),
                dict(base, object='chat.completion.chunk', usage=usage, choices=[{
                    'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
            stream = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            self.reply(200, stream + 'data: [DONE]\n\n', 'text/event-stream')
        else:
            self.reply(200, dict(base, object='chat.completion', usage=usage, choices=[{
                'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]))


def main():
    root = Path(__file__).resolve().parent.parent
    provider = Provider()
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='zhihui-suite-') as name:
            state = Path(name)
            (state / 'auths').mkdir()
            (state / 'home').mkdir()
            upstream = state / 'config.json'
            upstream.write_text('{"server":{"listen":":7863"}}\n')
            env = dict(os.environ, HOME=str(state / 'home'), USERPROFILE=str(state / 'home'),
                WB_DATA_DIR=name, WB_AUTH_DIR=str(state / 'auths'), WB_UPSTREAM_CONFIG=str(upstream),
                OPENAI_BASE_URL=f'http://127.0.0.1:{provider.server_port}/v1',
                WB2API_BASE=f'http://127.0.0.1:{provider.server_port}',
                WB2API_KEY='isolated-upstream-test', PYTHONDONTWRITEBYTECODE='1')
            args = sys.argv[1:] or ['server/tests', '-q', '-p', 'no:cacheprovider',
                                      '--timeout=60', '--timeout-method=thread']
            result = subprocess.run([sys.executable, '-m', 'pytest', *args], cwd=root, env=env)
            print(json.dumps({'fixture': 'private-loopback-http',
                              'provider_requests': sum(provider.counts.values()),
                              'exit_code': result.returncode}), flush=True)
            return result.returncode
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
