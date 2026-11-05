#!/usr/bin/env python3
"""Compare pure gateway hot paths against an unmodified checkout.

No server, database, account directory or network connection is opened. Only the
named standalone functions/classes are compiled from source with an explicit
namespace, so importing a checkout cannot run application startup code.

    python tests/benchmarks/gateway_resources.py --baseline /path/to/baseline

Timing is a local CPU microbenchmark, not provider tokens/s or end-to-end latency.
Peak allocation uses tracemalloc separately from timing and excludes input data.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import codecs
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import time
import tracemalloc

from starlette.responses import JSONResponse


def source_objects(path, names):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in names:
                selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets):
            selected.append(node)
    namespace = {'asyncio': asyncio, 'codecs': codecs, 'json': json, 'os': os,
                 're': re, 'JSONResponse': JSONResponse}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace, hashlib.sha256(source.encode()).hexdigest()


def measure(before, after, rounds):
    samples = {'before_ms': [], 'after_ms': []}
    before()
    after()
    for iteration in range(rounds):
        order = [('before_ms', before), ('after_ms', after)]
        if iteration % 2:
            order.reverse()
        for label, function in order:
            gc.collect()
            started = time.perf_counter()
            function()
            samples[label].append((time.perf_counter() - started) * 1000)
    peaks = {}
    for label, function in [('before_peak_bytes', before), ('after_peak_bytes', after)]:
        gc.collect()
        tracemalloc.start()
        try:
            function()
            peaks[label] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    previous, current = (statistics.median(samples[key]) for key in ('before_ms', 'after_ms'))
    return {**samples, **peaks, 'before_median_ms': previous, 'after_median_ms': current,
            'speedup': previous / current,
            'peak_reduction_percent': 100 * (1 - peaks['after_peak_bytes'] / peaks['before_peak_bytes'])}


async def fragmented_body(guard_class, size):
    received = 0
    captured = None
    async def receive():
        nonlocal received
        received += 1
        return {'type': 'http.request', 'body': b'x', 'more_body': received < size}
    async def app(scope, receive, send):
        nonlocal captured
        captured = (await receive())['body']
    async def send(message):
        raise AssertionError(f'Unexpected rejection: {message}')
    await guard_class(app, maximum=size)({'type': 'http', 'path': '/api/upload',
                                           'method': 'POST', 'headers': []}, receive, send)
    assert captured == b'x' * size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--rounds', default=7, type=int)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error('rounds must be in 1..100')
    root = Path(__file__).resolve().parents[2]
    objects, hashes = [], {}
    for label, checkout in [('before', args.baseline.resolve()), ('after', root)]:
        modules = {}
        for name, symbols in {
            'routers/gateway.py': {'_invalid_unicode_path', '_SURROGATE'},
            'observability.py': {'StreamAudit', 'merge_usage'},
            'bodyguard.py': {'APIBodyGuard', '_env_int'},
        }.items():
            path = checkout / 'manager/server' / name
            if not path.is_file():
                parser.error(f'Missing source: {path}')
            modules[name], digest = source_objects(path, symbols)
            hashes[f'{label}:{name}'] = digest
        objects.append(modules)
    old, new = objects
    wide = {'input': [0] * 100000}
    text = {'input': 'x' * (1024 * 1024)}
    wire = b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\n' * 4096
    def unicode(module, body):
        assert module['routers/gateway.py']['_invalid_unicode_path'](body) is None
    def sse(module):
        audit = module['observability.py']['StreamAudit']()
        result = audit.feed(wire)
        assert len(result) == 4096 and not audit.finish()
        assert audit.saw_useful and audit.error is None
    report = {'scope': 'Pure local CPU/heap microbenchmarks; no production/network/inference claim',
              'python': platform.python_version(), 'platform': platform.platform(),
              'rounds': args.rounds, 'source_sha256': hashes,
              'unicode_wide_100000_nodes': measure(lambda: unicode(old, wide), lambda: unicode(new, wide), args.rounds),
              'unicode_ascii_1mib': measure(lambda: unicode(old, text), lambda: unicode(new, text), args.rounds),
              'sse_coalesced_4096_events': measure(lambda: sse(old), lambda: sse(new), args.rounds),
              'asgi_128kib_in_one_byte_frames': measure(
                  lambda: asyncio.run(fragmented_body(old['bodyguard.py']['APIBodyGuard'], 128 * 1024)),
                  lambda: asyncio.run(fragmented_body(new['bodyguard.py']['APIBodyGuard'], 128 * 1024)), args.rounds)}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
