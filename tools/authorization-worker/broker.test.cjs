'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {PassThrough, Writable} = require('node:stream');
const {FixtureBroker, BrokerError} = require('./broker.cjs');
const {serveStdio, MAX_LINE_BYTES} = require('./stdio.cjs');

const ORIGIN = 'http://127.0.0.1:48765';
const OWNER = 'synthetic-owner';
const STATE = 's'.repeat(43);
const SECRET = 'synthetic-private-value-never-log';
const tick = () => new Promise(resolve => setImmediate(resolve));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function bootstrap() {
  return ORIGIN + '/authorization?' + new URLSearchParams({
    auth_callback_url: 'http://127.0.0.1:17388/authorize', login_trace_id: 'a'.repeat(32),
    code_challenge_method: 'S256', code_challenge: 'x'.repeat(43),
  });
}
function open(extra = {}) {
  return {id: 1, command: 'open', owner: OWNER, state: STATE, authUrl: bootstrap(), ...extra};
}
function cmd(handle, command, extra = {}) {
  return {id: 2, command, owner: OWNER, state: STATE, handle, ...extra};
}
function code(response, expected) {
  assert.deepEqual(response, {id: response.id, ok: false, error: {code: expected}});
}
class FakePage extends EventEmitter {
  constructor(context) {
    super();
    this.context = context;
    this.current = 'about:blank';
    this.mouse = {move: async () => {}, down: async () => {}, up: async () => {}, wheel: async () => {}};
    this.keyboard = {press: async () => {}};
  }
  async goto(url) {
    if (this.context.options.gotoError) throw new Error(SECRET);
    if (this.context.options.goto) await this.context.options.goto();
    this.current = url;
    this.emit('framenavigated', this);
  }
  url() { return this.current; }
  mainFrame() { return this; }
  viewportSize() { return {width: 520, height: 760}; }
  async screenshot() {
    if (this.context.options.screenshot) return this.context.options.screenshot();
    return Buffer.alloc(256, 65);
  }
  getByText() { return {click: async () => {}}; }
  getByPlaceholder() { return {fill: async () => {}}; }
}
class FakeContext extends EventEmitter {
  constructor(options = {}) { super(); this.options = options; this.closed = false; }
  async newPage() { this.page = new FakePage(this); return this.page; }
  async route() {}
  async close() {
    if (this.options.closeError) throw new Error(SECRET);
    if (this.options.close) await this.options.close();
    if (!this.closed) { this.closed = true; this.emit('close'); }
  }
}
function fixture(t, options = {}) {
  const contexts = [], received = [];
  const broker = new FixtureBroker({fixtureOrigin: ORIGIN,
    createContext: async settings => {
      received.push(settings);
      const context = new FakeContext(options.context);
      contexts.push(context);
      return context;
    }, ...options.broker});
  t.after(() => broker.shutdown());
  return {broker, contexts, received};
}
async function transport(broker, chunks, options = {}) {
  const input = new PassThrough(), lines = [];
  const output = new Writable({write(chunk, encoding, callback) { lines.push(chunk.toString()); callback(); }});
  const serving = serveStdio({input, output, broker, ...options});
  for (const chunk of chunks) input.write(chunk);
  input.end();
  await serving;
  return lines.join('').trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
}

test('constructor is fixture-only with bounded trusted runtime configuration', () => {
  for (const origin of [undefined, 'https://www.trae.cn', 'http://localhost:80',
    'http://127.0.0.1:80/', 'http://127.0.0.1:65536', 'http://127.0.0.1:00080',
    'http://127.0.0.1:1234@unreviewed.invalid']) {
    assert.throws(() => new FixtureBroker({fixtureOrigin: origin, createContext() {}}),
      error => error.code === 'fixture_runtime_required');
  }
  for (const settings of [{maxSessions: 5}, {maxSessions: 1, maxPerOwner: 2},
    {ttlMs: 300001}, {operationTimeoutMs: 20001}, {maxFrameBytes: 1048577}, {maxInFlight: 9}]) {
    assert.throws(() => new FixtureBroker({fixtureOrigin: ORIGIN, createContext() {}, ...settings}),
      error => error.code === 'invalid_configuration');
  }
});

test('bootstrap validates before context allocation and rejects arbitrary URLs/query fields', async t => {
  const {broker, contexts} = fixture(t);
  for (const authUrl of ['bad-' + SECRET, bootstrap().replace(ORIGIN, 'https://www.trae.cn'),
    bootstrap().replace('/authorization?', '/private?'), bootstrap() + '&url=' + SECRET,
    bootstrap() + '&login_trace_id=' + 'b'.repeat(32), bootstrap() + '#private']) {
    code(await broker.execute(open({authUrl})), 'invalid_bootstrap');
  }
  assert.equal(contexts.length, 0);
  assert.equal(broker.stats().sessions, 0);
});

test('finite schema rejects undeclared commands, fields, values and nonplain objects', async t => {
  const {broker, contexts} = fixture(t);
  for (const request of [null, [], {}, Object.create({command: 'open'}),
    open({id: SECRET}), open({command: 'evaluate'}), open({script: SECRET}),
    open({owner: 'bad\nowner'}), open({state: 'short'}), open({owner: 'x'.repeat(129)}),
    JSON.parse(JSON.stringify(open()).replace('"command"', '"__proto__":{},"command"'))]) {
    const result = await broker.execute(request);
    code(result, 'invalid_command');
    assert.equal(JSON.stringify(result).includes(SECRET), false);
  }
  assert.equal(contexts.length, 0);
});

test('random capability, owner and state are independently required for every session operation', async t => {
  const {broker, contexts, received} = fixture(t);
  const opened = await broker.execute(open());
  assert.equal(opened.ok, true);
  const handle = opened.data.handle;
  assert.match(handle, /^[A-Za-z0-9_-]{43}$/);
  assert.deepEqual(received, [{viewport: {width: 520, height: 760}, serviceWorkers: 'block', acceptDownloads: false}]);
  assert.equal(opened.data.mode, 'synthetic_fixture');
  assert.equal(opened.data.productionReady, false);
  for (const command of ['status', 'capture', 'cancel', 'close']) {
    for (const extra of [{owner: 'wrong-owner'}, {state: 'q'.repeat(43)}, {handle: 'x'.repeat(43)}]) {
      code(await broker.execute(cmd(handle, command, extra)), 'session_not_found');
    }
  }
  assert.equal(contexts[0].closed, false);
  const closed = await broker.execute(cmd(handle, 'close'));
  assert.deepEqual(closed.data, {closed: true});
  assert.equal(contexts[0].closed, true);
  code(await broker.execute(cmd(handle, 'status')), 'session_not_found');
});

test('capacity reserves starting contexts before await, enforces owner quota and allows reuse after close', async t => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const {broker, contexts} = fixture(t, {broker: {createContext: async () => {
    await waiting;
    const context = new FakeContext(); contexts.push(context); return context;
  }}});
  const first = broker.execute(open());
  code(await broker.execute(open({id: 2})), 'owner_capacity_exceeded');
  const second = broker.execute(open({id: 3, owner: 'second-owner'}));
  code(await broker.execute(open({id: 4, owner: 'third-owner'})), 'capacity_exceeded');
  assert.equal(broker.stats().sessions, 2);
  release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.ok && b.ok, true);
  await broker.execute(cmd(a.data.handle, 'close'));
  assert.equal((await broker.execute(open({id: 5}))).ok, true);
});

test('global in-flight and per-session busy limits do not queue unbounded operations', async t => {
  let release;
  const screenshot = new Promise(resolve => { release = resolve; });
  const {broker} = fixture(t, {context: {screenshot: () => screenshot}, broker: {maxInFlight: 1}});
  const opened = await broker.execute(open());
  const capture = broker.execute(cmd(opened.data.handle, 'capture'));
  code(await broker.execute(cmd(opened.data.handle, 'status')), 'worker_busy');
  release(Buffer.alloc(256));
  assert.equal((await capture).ok, true);
  const {broker: other} = fixture(t, {context: {screenshot: () => new Promise(() => {})},
    broker: {operationTimeoutMs: 20}});
  const next = await other.execute(open());
  const active = other.execute(cmd(next.data.handle, 'capture'));
  code(await other.execute(cmd(next.data.handle, 'cancel')), 'session_busy');
  code(await active, 'operation_timeout');
});

test('only finite form actions, human keys and bounded frame coordinates can be dispatched', async t => {
  const {broker} = fixture(t);
  const {data: {handle}} = await broker.execute(open());
  for (const request of [cmd(handle, 'action', {kind: 'evaluate', value: SECRET}),
    cmd(handle, 'action', {kind: 'choose_phone', value: SECRET}),
    cmd(handle, 'pointer', {frameId: 1, type: 'move', x: 520, y: 1}),
    cmd(handle, 'key', {frameId: 1, key: 'Control+L'}),
    cmd(handle, 'scroll', {frameId: 1, x: 1, y: 1, deltaX: 601, deltaY: 0})]) {
    assert.equal((await broker.execute(request)).ok, false);
  }
  code(await broker.execute(cmd(handle, 'action', {kind: 'fill_phone', value: SECRET})), 'invalid_phone');
  code(await broker.execute(cmd(handle, 'action', {kind: 'fill_code', value: SECRET})), 'invalid_code');
  assert.equal((await broker.execute(cmd(handle, 'action', {kind: 'choose_phone'}))).data.phase, 'phone');
  assert.equal((await broker.execute(cmd(handle, 'action', {kind: 'fill_phone', value: '13800000000'}))).ok, true);
  const frame = await broker.execute(cmd(handle, 'capture'));
  assert.equal((await broker.execute(cmd(handle, 'key', {frameId: frame.data.id, key: 'Tab'}))).ok, true);
});

test('frames are direct responses with bounded metadata; status does not contain images or bootstrap', async t => {
  const {broker} = fixture(t);
  const {data: {handle}} = await broker.execute(open());
  const capture = await broker.execute(cmd(handle, 'capture'));
  assert.equal(Buffer.from(capture.data.jpegBase64, 'base64').length, 256);
  assert.deepEqual(Object.keys(capture.data).sort(),
    ['expiresAt', 'height', 'id', 'jpegBase64', 'mime', 'origin', 'width']);
  const status = await broker.execute(cmd(handle, 'status'));
  assert.equal(JSON.stringify(status).includes('jpeg'), false);
  assert.equal(JSON.stringify(capture).includes('login_trace_id'), false);
  assert.equal(JSON.stringify(status).includes(STATE), false);
});

test('oversized frame closes context instead of encoding or retaining it', async t => {
  const {broker, contexts} = fixture(t, {context: {screenshot: async () => Buffer.alloc(129)},
    broker: {maxFrameBytes: 128}});
  const {data: {handle}} = await broker.execute(open());
  code(await broker.execute(cmd(handle, 'capture')), 'frame_too_large');
  await tick();
  assert.equal(contexts[0].closed, true);
  assert.equal(broker.stats().sessions, 0);
});

test('factory/start failures use fixed errors, cleanup contexts and do not reflect exceptions', async t => {
  const {broker} = fixture(t, {broker: {createContext: async () => { throw new Error(SECRET); }}});
  code(await broker.execute(open()), 'worker_failed');
  assert.equal(broker.stats().sessions, 0);
  const {broker: coded} = fixture(t, {broker: {createContext: async () => { throw new BrokerError(SECRET); }}});
  code(await coded.execute(open()), 'worker_failed');
  const {broker: failing, contexts} = fixture(t, {context: {gotoError: true}});
  code(await failing.execute(open()), 'official_page_unavailable');
  await tick();
  assert.equal(contexts[0].closed, true);
  assert.equal(failing.stats().sessions, 0);
});

test('close cancels active frame work and late completion cannot return an image', async t => {
  let release;
  const screenshot = new Promise(resolve => { release = resolve; });
  const {broker, contexts} = fixture(t, {context: {screenshot: () => screenshot}});
  const opened = await broker.execute(open());
  const capture = broker.execute(cmd(opened.data.handle, 'capture'));
  assert.equal((await broker.execute(cmd(opened.data.handle, 'close'))).ok, true);
  release(Buffer.alloc(256));
  const result = await capture;
  code(result, 'session_closed');
  assert.equal(JSON.stringify(result).includes('jpeg'), false);
  assert.equal(contexts[0].closed, true);
});

test('TTL expires memory sessions and status reports the actual shorter transport lifetime', async t => {
  let clock = 1000;
  const {broker, contexts} = fixture(t, {broker: {ttlMs: 30, now: () => clock}});
  const opened = await broker.execute(open());
  assert.equal(opened.data.expiresAt, 1030);
  assert.equal((await broker.execute(cmd(opened.data.handle, 'status'))).data.expiresAt, 1030);
  clock = 1030;
  code(await broker.execute(cmd(opened.data.handle, 'status')), 'session_expired');
  await tick();
  assert.equal(contexts[0].closed, true);
  assert.equal(broker.stats().sessions, 0);
});

test('wall-clock TTL and unexpected context close reclaim contexts without new commands', async t => {
  const {broker, contexts} = fixture(t, {broker: {ttlMs: 15}});
  await broker.execute(open());
  await delay(25);
  assert.equal(contexts[0].closed, true);
  assert.equal(broker.stats().sessions, 0);
  const {broker: other, contexts: more} = fixture(t);
  await other.execute(open());
  await more[0].close();
  await tick();
  assert.equal(other.stats().sessions, 0);
});

test('late context factory resolution after timeout/disconnect is closed and cannot escape capacity', async t => {
  let release;
  const late = new Promise(resolve => { release = resolve; });
  const {broker} = fixture(t, {broker: {operationTimeoutMs: 10, createContext: () => late}});
  code(await broker.execute(open()), 'operation_timeout');
  assert.equal(broker.stats().sessions, 1, 'Unresolved context still owns its capacity slot');
  await broker.shutdown();
  const context = new FakeContext();
  release(context);
  await tick();
  assert.equal(context.closed, true);
  assert.equal(broker.stats().sessions, 0);
  code(await broker.execute(open()), 'worker_closed');
});

test('late native close confirmation frees a quarantined slot, never before confirmation', async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const {broker, contexts} = fixture(t, {context: {close: () => pending}, broker: {operationTimeoutMs: 10}});
  const opened = await broker.execute(open());
  code(await broker.execute(cmd(opened.data.handle, 'close')), 'operation_timeout');
  assert.equal(broker.stats().sessions, 1);
  release();
  await tick();
  assert.equal(contexts[0].closed, true);
  assert.equal(broker.stats().sessions, 0);
});

test('shutdown closes contexts, is idempotent, and quarantines unconfirmed browser teardown', async t => {
  const {broker, contexts} = fixture(t);
  await broker.execute(open());
  await broker.shutdown();
  await broker.shutdown();
  assert.equal(contexts[0].closed, true);
  assert.equal(broker.stats().sessions, 0);
  const {broker: stubborn} = fixture(t, {context: {closeError: true}});
  const opened = await stubborn.execute(open());
  code(await stubborn.execute(cmd(opened.data.handle, 'close')), 'shutdown_unconfirmed');
  assert.equal(stubborn.stats().sessions, 1, 'Do not claim a leaked resource is free');
});

test('JSONL transport accepts split UTF-8 frames, serializes responses and tears down on EOF', async t => {
  const {broker, contexts} = fixture(t);
  const request = Buffer.from(JSON.stringify(open({owner: '\u5408\u6210\u7528\u6237'})) + '\n');
  const split = request.indexOf(Buffer.from('\u5408')) + 1;
  const responses = await transport(broker, [request.subarray(0, split), request.subarray(split)]);
  assert.equal(responses[0].ok, true);
  assert.equal(contexts[0].closed, true);
  assert.equal(broker.stats().sessions, 0);
});

test('JSONL malformed/invalid UTF-8 and unterminated requests never reflect input', async t => {
  const {broker, contexts} = fixture(t);
  const responses = await transport(broker, ['{' + SECRET + '\n', Buffer.from([0xff, 10]),
    JSON.stringify(open())]);
  assert.deepEqual(responses.map(row => row.error.code), ['invalid_json', 'invalid_json', 'incomplete_line']);
  assert.equal(JSON.stringify(responses).includes(SECRET), false);
  assert.equal(contexts.length, 0);
});

test('JSONL line and chunk limits disconnect rather than retain oversized input', async t => {
  for (const chunks of [['x'.repeat(MAX_LINE_BYTES + 1) + '\n'],
    ['x'.repeat(MAX_LINE_BYTES), 'x'], ['x'.repeat(MAX_LINE_BYTES * 2 + 1)],
    ['x', 'x'.repeat(MAX_LINE_BYTES * 2 + 1)]]) {
    const {broker, contexts} = fixture(t);
    const responses = await transport(broker, chunks);
    assert.equal(responses[0].error.code, 'input_too_large');
    assert.equal(responses.length, 1);
    assert.equal(contexts.length, 0);
  }
});

test('JSONL output backpressure is bounded and output disconnect destroys active session', async t => {
  const {broker, contexts} = fixture(t);
  const input = new PassThrough();
  const output = new Writable({write(chunk, encoding, callback) { void callback; }});
  const serving = serveStdio({input, output, broker, writeTimeoutMs: 10});
  input.end(JSON.stringify(open()) + '\n');
  await serving;
  assert.equal(contexts[0].closed, true);
  const {broker: disconnected, contexts: more} = fixture(t);
  const source = new PassThrough(), sink = new PassThrough();
  const active = serveStdio({input: source, output: sink, broker: disconnected});
  source.write(JSON.stringify(open()) + '\n');
  await tick();
  sink.destroy();
  await active;
  assert.equal(more[0].closed, true);
});
