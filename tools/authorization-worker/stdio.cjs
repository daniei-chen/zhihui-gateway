'use strict';

const {TextDecoder} = require('node:util');
const {FixtureBroker} = require('./broker.cjs');

const MAX_LINE_BYTES = 24576;
const MAX_RESPONSE_BYTES = 1400000;

/** One in-flight line, no unbounded user-space request or response queue. */
async function serveStdio({input, output, broker, writeTimeoutMs = 5000} = {}) {
  if (!(broker instanceof FixtureBroker) || !input?.[Symbol.asyncIterator] ||
      typeof output?.write !== 'function' || !Number.isInteger(writeTimeoutMs) ||
      writeTimeoutMs < 10 || writeTimeoutMs > 5000) throw new Error('invalid_transport');
  let pending = Buffer.alloc(0), failed = false;
  const decoder = new TextDecoder('utf-8', {fatal: true});
  const disconnect = () => { failed = true; input.destroy?.(); void broker.shutdown(); };
  input.on('error', disconnect);
  output.on('error', disconnect);
  output.on('close', disconnect);
  async function send(response) {
    if (failed || output.destroyed || output.writableEnded) throw new Error('transport_closed');
    const line = JSON.stringify(response) + '\n';
    if (Buffer.byteLength(line) > MAX_RESPONSE_BYTES) throw new Error('response_too_large');
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('transport_timeout')), writeTimeoutMs);
      // The callback waits for the stream to flush; never accumulate frames behind backpressure.
      try {
        output.write(line, error => {
          clearTimeout(timer);
          if (error) reject(new Error('transport_closed'));
          else resolve();
        });
      } catch {
        clearTimeout(timer);
        reject(new Error('transport_closed'));
      }
    });
  }
  async function consume(bytes) {
    let request;
    try { request = JSON.parse(decoder.decode(bytes)); }
    catch {
      await send({id: null, ok: false, error: {code: 'invalid_json'}});
      return;
    }
    await send(await broker.execute(request));
  }
  try {
    for await (const piece of input) {
      if (failed) break;
      const chunk = Buffer.isBuffer(piece) ? piece : Buffer.from(piece);
      // An oversized pipe read must not become an unbounded parsing queue.
      if (chunk.length > MAX_LINE_BYTES * 2) {
        await send({id: null, ok: false, error: {code: 'input_too_large'}});
        failed = true;
        break;
      }
      let start = 0;
      for (let i = 0; i < chunk.length; i += 1) {
        if (chunk[i] !== 10) continue;
        const length = pending.length + i - start;
        if (length > MAX_LINE_BYTES) {
          await send({id: null, ok: false, error: {code: 'input_too_large'}});
          failed = true;
          break;
        }
        await consume(Buffer.concat([pending, chunk.subarray(start, i)], length));
        pending = Buffer.alloc(0);
        start = i + 1;
        if (failed) break;
      }
      if (failed) break;
      if (pending.length + chunk.length - start > MAX_LINE_BYTES) {
        await send({id: null, ok: false, error: {code: 'input_too_large'}});
        failed = true;
        break;
      }
      pending = Buffer.concat([pending, chunk.subarray(start)]);
    }
    if (!failed && pending.length) {
      await send({id: null, ok: false, error: {code: 'incomplete_line'}});
    }
  } catch { /* Fixed protocol errors only; never write exception text or request bodies. */ }
  finally {
    failed = true;
    pending = Buffer.alloc(0);
    await broker.shutdown();
    output.destroy?.();
    input.off('error', disconnect);
    output.off('error', disconnect);
    output.off('close', disconnect);
    input.destroy?.();
  }
}

module.exports = {serveStdio, MAX_LINE_BYTES, MAX_RESPONSE_BYTES};
