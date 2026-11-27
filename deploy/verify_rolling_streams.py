#!/usr/bin/env python3
"""Exercise the repository relay across two route changes over real loopback HTTP.

This is an offline release acceptance tool, not a production rollout command.
It creates only private temporary routes and synthetic providers; it accepts no
endpoint, credential, service, or route-file argument. Its evidence cannot mark
a production deployment as zero-downtime verified.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, nullcontext
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn

from deploy.cohort_rollout import atomic_json
from server.rollingrelay import RouteError, StreamingRelay

SCOPE = 'synthetic-loopback-relay-cutover/v1'
FRAMES = 8
MAX_BODY = 64 * 1024
MAX_REPORT = 1024 * 1024
MAX_PROBES = 256
MAX_EVIDENCE_AGE = 3600


class EvidenceError(RuntimeError):
    pass


def _frame(request_id, generation, sequence):
    return ('data: ' + json.dumps({'id': request_id, 'generation': generation,
        'sequence': sequence}, sort_keys=True, separators=(',', ':')) + '\n\n').encode()


def _payload(request_id, generation, frames):
    return b''.join(_frame(request_id, generation, index) for index in range(frames)) + b'data: [DONE]\n\n'


class _Provider:
    """Synthetic responses only; no real gateway credentials or business data."""
    def __init__(self, generation, release, calls, hold_seconds, fault=''):
        self.generation, self.release, self.calls = generation, release, calls
        self.hold_seconds, self.fault = hold_seconds, fault

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        body = bytearray()
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                return
            body.extend(event.get('body', b''))
            if len(body) > 4096:
                raise EvidenceError('synthetic_request_limit')
            if not event.get('more_body'):
                break
        value = json.loads(body)
        request_id, long = value['id'], value['long']
        if not re.fullmatch(r'[a-z0-9-]{1,80}', request_id) or type(long) is not bool:
            raise EvidenceError('invalid_synthetic_request')
        self.calls.append({'id': request_id, 'generation': self.generation,
                           'accepted_ns': time.monotonic_ns()})
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/event-stream'),
                                (b'x-synthetic-generation', self.generation.encode())]})
        count = FRAMES if long else 1
        for index in range(count):
            if long and index == 1:
                await self.release.wait()
            if long and index:
                await asyncio.sleep(self.hold_seconds / (FRAMES - 1))
            if long and self.fault == 'stall' and index == 1:
                await asyncio.Event().wait()
            if long and self.fault == 'truncate' and index == 2:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                return
            actual_index = index + 1 if long and self.fault == 'sequence' and index == 2 else index
            await send({'type': 'http.response.body',
                        'body': _frame(request_id, self.generation, actual_index), 'more_body': True})
        terminal = b'' if long and self.fault == 'missing-terminal' else b'data: [DONE]\n\n'
        await send({'type': 'http.response.body', 'body': terminal, 'more_body': False})


class _Server(uvicorn.Server):
    # The tool owns tasks, not process signal handlers or production services.
    def capture_signals(self):
        return nullcontext()

    def install_signal_handlers(self):
        pass


@asynccontextmanager
async def _http_server(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    listener.setblocking(False)
    server = _Server(uvicorn.Config(app, lifespan='off', log_config=None,
        access_log=False, log_level='critical', timeout_keep_alive=1,
        timeout_graceful_shutdown=0.2, h11_max_incomplete_event_size=8192))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
                raise EvidenceError('fixture_server_did_not_start')
            await asyncio.sleep(0.01)
        else:
            raise EvidenceError('fixture_server_start_timeout')
        yield 'http://127.0.0.1:' + str(listener.getsockname()[1])
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 3)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            listener.close()


async def _observe(client, url, request_id, generation, long, started=None):
    record = {'id': request_id, 'generation': generation, 'long': long,
              'status': None, 'first_ns': None, 'last_ns': None, 'end_ns': None,
              'frames': 0, 'terminal': False, 'bytes': 0}
    digest, pending, sequences = hashlib.sha256(), bytearray(), []
    async with client.stream('POST', url + '/v1/chat/completions',
            json={'id': request_id, 'long': long},
            headers={'X-Request-ID': request_id}) as response:
        record['status'] = response.status_code
        actual_generation = response.headers.get('x-synthetic-generation')
        if response.status_code != 200 or actual_generation not in ('old', 'new'):
            raise EvidenceError('bad_stream_response')
        if generation is not None and actual_generation != generation:
            raise EvidenceError('wrong_generation')
        record['generation'] = actual_generation
        async for chunk in response.aiter_bytes():
            record['bytes'] += len(chunk)
            if record['bytes'] > MAX_BODY:
                raise EvidenceError('stream_byte_limit')
            digest.update(chunk)
            pending.extend(chunk)
            while b'\n\n' in pending:
                raw, _, tail = pending.partition(b'\n\n')
                pending = bytearray(tail)
                if record['terminal']:
                    raise EvidenceError('data_after_terminal')
                if raw == b'data: [DONE]':
                    record['terminal'] = True
                    continue
                if not raw.startswith(b'data: '):
                    raise EvidenceError('invalid_sse_frame')
                frame = json.loads(raw[6:])
                if frame != {'id': request_id, 'generation': actual_generation,
                             'sequence': len(sequences)}:
                    raise EvidenceError('invalid_stream_sequence')
                sequences.append(frame['sequence'])
                if len(sequences) > FRAMES:
                    raise EvidenceError('stream_frame_limit')
                now = time.monotonic_ns()
                record['first_ns'] = record['first_ns'] or now
                record['last_ns'] = now
                if started is not None:
                    started.set()
            if len(pending) > 4096:
                raise EvidenceError('sse_frame_byte_limit')
    record['end_ns'] = time.monotonic_ns()
    record['frames'], record['sha256'] = len(sequences), digest.hexdigest()
    expected = _payload(request_id, record['generation'], FRAMES if long else 1)
    if pending or not record['terminal'] or record['sha256'] != hashlib.sha256(expected).hexdigest():
        raise EvidenceError('incomplete_stream')
    return record


def _source_identity():
    root = Path(__file__).resolve().parents[1]
    files = ('server/rollingrelay.py', 'server/services/asyncwork.py',
             'deploy/cohort_rollout.py', 'deploy/verify_rolling_streams.py')
    identity = {'commit': None, 'clean': False, 'files': {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}}
    try:
        commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
            capture_output=True, timeout=5, check=True).stdout.decode('ascii').strip()
        status = subprocess.run(['git', '-C', str(root), '-c', 'core.fsmonitor=false',
            'status', '--porcelain', '--untracked-files=all'], capture_output=True,
            timeout=5, check=True).stdout
        if re.fullmatch('[0-9a-f]{40}', commit):
            identity.update(commit=commit, clean=not status)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return identity


def validate_evidence(report, *, now=None):
    """Validate bounded, fresh local proof. Never certifies a production ingress."""
    now = time.time() if now is None else now

    def require(condition):
        if not condition:
            raise EvidenceError('invalid_cutover_evidence')

    try:
        require(isinstance(report, dict) and type(report['schema']) is int and
                report['schema'] == 1 and report['scope'] == SCOPE)
        require(report['result'] == 'passed' and report['synthetic_cutover_verified'] is True)
        require(report['transport'] == 'real-loopback-http1' and
                report['production_zero_downtime_verified'] is False)
        require(type(report['created_at']) is int and -5 <= now - report['created_at'] <= MAX_EVIDENCE_AGE)
        require(re.fullmatch('[0-9a-f]{40}', report['source']['commit']) and
                type(report['source']['clean']) is bool)
        source_files = report['source']['files']
        require(set(source_files) == {'server/rollingrelay.py', 'server/services/asyncwork.py',
                'deploy/cohort_rollout.py', 'deploy/verify_rolling_streams.py'} and
                all(re.fullmatch('[0-9a-f]{64}', value) for value in source_files.values()))
        cuts, streams, probes = report['cutovers'], report['streams'], report['probes']
        require(type(cuts) is list and type(streams) is list and type(probes) is list and
                len(cuts) == 2 and len(streams) == 2 and 4 <= len(probes) <= MAX_PROBES)
        require(all(type(value) is int and value > 0 for value in (
            report['started_ns'], report['ended_ns'], cuts[0]['at_ns'], cuts[1]['at_ns'])))
        require(cuts[0]['target'] == 'new' and cuts[0]['epoch'] == 2)
        require(cuts[1]['target'] == 'old' and cuts[1]['epoch'] == 3)
        require(report['started_ns'] < cuts[0]['at_ns'] < cuts[1]['at_ns'] < report['ended_ns'])
        require(cuts[0]['active'] == {'old': 1, 'new': 0} and cuts[1]['active'] == {'old': 1, 'new': 1})
        require(report['active_after'] == {'old': 0, 'new': 0})
        require(report['close_refused_with_live_streams'] is True and report['transport_errors'] == 0)
        seen = set()
        for record in streams + probes:
            require(re.fullmatch('[a-z0-9-]{1,80}', record['id']) and record['id'] not in seen)
            seen.add(record['id'])
            require(record['status'] == 200 and record['terminal'] is True)
            require(record['generation'] in ('old', 'new') and type(record['long']) is bool)
            require(record['long'] is (record in streams))
            require(all(type(record[name]) is int for name in ('status', 'frames', 'bytes',
                'first_ns', 'last_ns', 'end_ns')))
            count = FRAMES if record['long'] else 1
            expected = _payload(record['id'], record['generation'], count)
            require(record['frames'] == count and record['bytes'] == len(expected))
            require(record['sha256'] == hashlib.sha256(expected).hexdigest())
            require(report['started_ns'] <= record['first_ns'] <= record['last_ns'] <= record['end_ns'] <= report['ended_ns'])
        for index, generation in enumerate(('old', 'new')):
            stream = streams[index]
            require(stream['long'] and stream['generation'] == generation)
            require(stream['first_ns'] < cuts[index]['at_ns'] < stream['last_ns'])
            require(stream['last_ns'] > cuts[-1]['at_ns'])
        by_phase = {probe.get('phase'): probe for probe in probes if probe.get('phase')}
        require(set(by_phase) == {'before', 'after-first', 'after-second'} and
                sum(bool(probe.get('phase')) for probe in probes) == 3)
        require(by_phase['before']['generation'] == 'old' and by_phase['before']['end_ns'] < cuts[0]['at_ns'])
        require(by_phase['after-first']['generation'] == 'new')
        require(cuts[0]['at_ns'] < by_phase['after-first']['first_ns'] <= by_phase['after-first']['end_ns'] < cuts[1]['at_ns'])
        require(by_phase['after-second']['generation'] == 'old' and cuts[1]['at_ns'] < by_phase['after-second']['first_ns'])
        require(any(not probe.get('phase') and cuts[1]['at_ns'] < probe['first_ns'] <
                    max(stream['end_ns'] for stream in streams) for probe in probes))
        calls = report['upstream_calls']
        require(len(calls) == len(seen) and Counter(call['id'] for call in calls) == Counter(seen))
        observed = {record['id']: record for record in streams + probes}
        require(all(call['generation'] == observed[call['id']]['generation'] and
                   report['started_ns'] <= call['accepted_ns'] <= observed[call['id']]['first_ns'] for call in calls))
        require(report['relay_accepted'] == len(calls))
        return True
    except (EvidenceError, KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return False


async def _exercise(report, directory, *, hold_seconds, timeout, fault):
    release, calls, tasks = asyncio.Event(), [], []
    route = directory / 'route.json'
    atomic_json(route, {'epoch': 1, 'target': 'old'})
    old = _Provider('old', release, calls, hold_seconds, fault)
    new = _Provider('new', release, calls, hold_seconds)
    async with _http_server(old) as old_url, _http_server(new) as new_url:
        relay = StreamingRelay({'old': old_url, 'new': new_url}, route, maximum=16)
        try:
            async with _http_server(relay) as ingress, httpx.AsyncClient(trust_env=False,
                    follow_redirects=False, timeout=httpx.Timeout(timeout),
                    limits=httpx.Limits(max_connections=8)) as client:
                nonce = uuid.uuid4().hex
                async def probe(phase=None, target=None):
                    index = len(report['probes'])
                    if index >= MAX_PROBES:
                        raise EvidenceError('probe_count_limit')
                    result = await _observe(client, ingress, f'{nonce}-probe-{index}', target, False)
                    if phase:
                        result['phase'] = phase
                    report['probes'].append(result)

                await probe('before', 'old')
                old_started, new_started = asyncio.Event(), asyncio.Event()
                tasks.append(asyncio.create_task(_observe(client, ingress,
                    nonce + '-old-stream', 'old', True, old_started)))
                await asyncio.wait_for(old_started.wait(), timeout)
                atomic_json(route, {'epoch': 2, 'target': 'new'})
                report['cutovers'].append({'target': 'new', 'epoch': 2,
                    'at_ns': time.monotonic_ns(), 'active': relay.snapshot()['active']})
                await probe('after-first', 'new')
                tasks.append(asyncio.create_task(_observe(client, ingress,
                    nonce + '-new-stream', 'new', True, new_started)))
                await asyncio.wait_for(new_started.wait(), timeout)
                atomic_json(route, {'epoch': 3, 'target': 'old'})
                report['cutovers'].append({'target': 'old', 'epoch': 3,
                    'at_ns': time.monotonic_ns(), 'active': relay.snapshot()['active']})
                await probe('after-second', 'old')
                try:
                    await relay.close()
                except RouteError:
                    report['close_refused_with_live_streams'] = True
                else:
                    raise EvidenceError('closed_relay_with_live_streams')
                # Causal liveness evidence while both original streams are
                # still held; do not depend on scheduler/TCP coalescing timing.
                await probe()
                release.set()
                while not all(task.done() for task in tasks):
                    await probe()
                    await asyncio.sleep(max(0.01, hold_seconds / 100))
                report['streams'] = await asyncio.gather(*tasks)
                for _ in range(100):
                    if not any(relay.active.values()):
                        break
                    await asyncio.sleep(0.01)
                report.update(active_after=relay.snapshot()['active'],
                    upstream_calls=list(calls), relay_accepted=relay.accepted,
                    transport_errors=relay.transport_errors)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Closing is permitted only after the actual server has joined all
            # request tasks. A leak fails the proof rather than hiding ownership.
            await relay.close()


async def run_probe(*, hold_seconds=2.0, timeout=10.0, fault=''):
    if (not 0.001 <= hold_seconds <= 10 or not 0.1 <= timeout <= 60
            or fault not in ('', 'truncate', 'missing-terminal', 'sequence', 'stall')):
        raise ValueError('invalid_probe_bounds')
    report = {'schema': 1, 'scope': SCOPE, 'transport': 'real-loopback-http1',
        'created_at': int(time.time()), 'started_ns': time.monotonic_ns(),
        'source': _source_identity(), 'cutovers': [], 'streams': [], 'probes': [],
        'synthetic_cutover_verified': False, 'production_zero_downtime_verified': False,
        'close_refused_with_live_streams': False, 'result': 'failed'}
    try:
        with tempfile.TemporaryDirectory(prefix='zhihui-stream-proof-') as temporary:
            await asyncio.wait_for(_exercise(report, Path(temporary),
                hold_seconds=hold_seconds, timeout=timeout, fault=fault), timeout + hold_seconds + 5)
        report.update(ended_ns=time.monotonic_ns(), result='passed', synthetic_cutover_verified=True)
        if not validate_evidence(report):
            raise EvidenceError('incomplete_cutover_evidence')
    except Exception as error:
        report.update(ended_ns=time.monotonic_ns(), error_type=type(error).__name__,
                      result='failed', synthetic_cutover_verified=False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='write a new report file (never overwrite)')
    parser.add_argument('--verify-evidence', type=Path, help='only validate a saved local proof; no HTTP listeners')
    parser.add_argument('--hold-seconds', type=float, default=2.0)
    parser.add_argument('--timeout', type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        if args.verify_evidence is not None:
            if args.output is not None:
                raise ValueError('verification_does_not_write_reports')
            fd = os.open(args.verify_evidence, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as saved:
                info = os.fstat(saved.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT:
                    raise EvidenceError('unsafe_evidence_file')
                payload = saved.read(info.st_size + 1)
                if len(payload) != info.st_size:
                    raise EvidenceError('evidence_changed_during_read')
            accepted = validate_evidence(json.loads(payload))
            print(json.dumps({'result': 'passed' if accepted else 'failed',
                'synthetic_cutover_verified': accepted, 'production_zero_downtime_verified': False}))
            return 0 if accepted else 1
        report = asyncio.run(run_probe(hold_seconds=args.hold_seconds, timeout=args.timeout))
        payload = (json.dumps(report, sort_keys=True, indent=2) + '\n').encode()
        if len(payload) > MAX_REPORT:
            raise EvidenceError('report_size_limit')
        if args.output is not None:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(payload)
        print(payload.decode(), end='')
        return 0 if report['synthetic_cutover_verified'] else 1
    except (OSError, ValueError, EvidenceError) as error:
        print(json.dumps({'result': 'failed', 'error_type': type(error).__name__,
                          'production_zero_downtime_verified': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
