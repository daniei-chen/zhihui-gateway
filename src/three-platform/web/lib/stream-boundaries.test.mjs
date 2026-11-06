import test from 'node:test';
import assert from 'node:assert/strict';
import {createSseParser} from './sse.ts';
import {streamChat} from './chat-stream.ts';

const encode = text => new TextEncoder().encode(text);
const sse = value => `data: ${typeof value === 'string' ? value : JSON.stringify(value)}\n\n`;

// A never-ending raw line must fail before a delimiter or EOF can be supplied.
// This covers data, comments and ignored fields: all consume browser memory.
for (const prefix of ['data: ', ': ', 'unknown: ', 'event: ', 'id: ']) {
  test(`SSE bounds an unterminated ${JSON.stringify(prefix)} line and stays terminal`, () => {
    const parser = createSseParser({maxDataBytes: 16});
    parser.push(prefix);
    let error;
    for (let n = 0; n < 30; n++) {
      const events = parser.push('x');
      if (events.length) {error = events[0]; break;}
    }
    assert.equal(parser.overflowed, true);
    assert.equal(error?.event, 'error');
    assert.match(error.data, /line exceeds/);
    assert.deepEqual(parser.push('ignored\n\ndata: unsafe\n\n'), []);
    assert.deepEqual(parser.flush(), []);
    assert.equal(parser.eventCount, 0);
  });
}

test('SSE bounds cumulative data across otherwise legal lines', () => {
  const parser = createSseParser({maxDataBytes: 12});
  assert.deepEqual(parser.push('data: 12345\n'), []);
  assert.deepEqual(parser.push('data: 67890\n'), []);
  const events = parser.push('data: x\ndata: ignored\n\n');
  assert.equal(events.length, 1);
  assert.equal(events[0].event, 'error');
  assert.match(events[0].data, /event exceeds/);
  assert.equal(parser.overflowed, true);
});

test('SSE counts UTF-8 bytes across codepoint and surrogate boundaries', () => {
  for (const parts of [['data: ', '中', '文'], ['data: ', '\ud83d', '\ude00', '中']]) {
    const parser = createSseParser({maxDataBytes: 100, maxLineBytes: 10});
    assert.deepEqual(parts.slice(0, -1).flatMap(part => parser.push(part)), []);
    assert.equal(parser.push(parts.at(-1))[0]?.event, 'error');
    assert.equal(parser.overflowed, true);
  }
  const parser = createSseParser({maxDataBytes: 5, maxLineBytes: 10});
  const events = ['data: ', '\ud83d', '\ude00', '\n\n'].flatMap(part => parser.push(part));
  assert.deepEqual(events.map(event => event.data), ['😀']);
  assert.equal(parser.overflowed, false);
});

test('SSE parsing is invariant at every byte and string split with CRLF, BOM and Unicode', () => {
  const text = '\ufeff: keepalive\r\nid: 7\rdata: 中😀\r\ndata: second\r\n\r\nevent: end\ndata: [DONE]\n\n';
  const expected = [{event: 'message', data: '中😀\nsecond', id: '7'}, {event: 'end', data: '[DONE]', id: '7'}];
  for (const input of [text, encode(text)]) {
    for (let split = 0; split <= input.length; split++) {
      const parser = createSseParser();
      const events = [...parser.push(input.slice(0, split)), ...parser.push(input.slice(split)), ...parser.flush()];
      assert.deepEqual(events, expected, `split ${split} of ${typeof input}`);
      assert.equal(parser.truncated, false);
    }
  }
});

test('SSE ignores only the initial BOM and keeps a later BOM as part of its field', () => {
  const parser = createSseParser();
  assert.equal(parser.push('\ufeffdata: first\n\n').length, 1);
  assert.deepEqual(parser.push('\ufeffdata: second\n\n'), []);
});

test('SSE rejects invalid limits and ignores a retry too large for an exact integer', () => {
  for (const value of [NaN, Infinity, -1, 1.5]) assert.throws(() => createSseParser({maxDataBytes: value}), RangeError);
  const parser = createSseParser();
  assert.equal(parser.push('retry: 999999999999999999999\ndata: value\n\n')[0].retry, undefined);
});

function terminalFixture(chunk) {
  const calls = {reads: 0, cancels: 0, releases: 0, requests: 0};
  const reader = {
    async read() {
      if (++calls.reads === 1) return {done: false, value: encode(chunk)};
      throw new Error('read after a terminal frame would hang on a persistent connection');
    },
    async cancel() {calls.cancels++;},
    releaseLock() {calls.releases++;},
  };
  return {calls, fetchImpl: async () => {calls.requests++; return {ok: true, status: 200, body: {getReader: () => reader}};}};
}

for (const [kind, frame] of [['done', sse('[DONE]')], ['error', sse({error: {message: 'upstream failed'}})], ['overflow', 'data: ' + 'x'.repeat(1024 * 1024 + 1)]]) {
  test(`stream releases its reader at ${kind} without another read or retry`, async () => {
    const {calls, fetchImpl} = terminalFixture(frame);
    const result = await streamChat({url: '/api/playground/chat', body: {}, fetchImpl});
    assert.equal(result.sawDone, kind === 'done');
    assert.equal(result.truncated, false);
    if (kind === 'done') assert.equal(result.error, null);
    else assert.match(result.error, kind === 'error' ? /upstream failed/ : /exceeds/);
    assert.deepEqual(calls, {reads: 1, cancels: 1, releases: 1, requests: 1});
  });
}

test('stream answer and display callback never exceed the declared answer cap', async () => {
  const {fetchImpl, calls} = terminalFixture(sse({choices: [{delta: {content: '123456789'}}]}));
  const displayed = [];
  const result = await streamChat({url: '/api/playground/chat', body: {}, fetchImpl, maxAnswerChars: 5,
    onDelta: (delta, full) => displayed.push([delta, full])});
  assert.equal(result.answer, '12345');
  assert.match(result.error, /exceeds 5/);
  assert.deepEqual(displayed, [['12345', '12345']]);
  assert.equal(calls.reads, 1);
});
