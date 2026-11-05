// Local parser microbenchmark. Node 24 can import the TypeScript source directly.
// Usage: node tests/benchmarks/frontend_sse.mjs /absolute/path/to/checkout
import { performance } from 'node:perf_hooks';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

if (process.argv.length !== 3) {
  throw new Error('Provide exactly one checkout path; this benchmark never calls an upstream.');
}
const source = resolve(process.argv[2], 'src/three-platform/web/lib/sse.ts');
const { createSseParser } = await import(pathToFileURL(source).href);
const rows = [];
for (const bytes of [65536, 131072, 262144, 524288]) {
  const samples = [];
  for (let round = 0; round < 5; round++) {
    const parser = createSseParser();
    const payload = 'data: ' + 'x'.repeat(bytes) + '\n\n';
    const start = performance.now();
    let events = 0;
    for (let offset = 0; offset < payload.length; offset += 256) {
      events += parser.push(payload.slice(offset, offset + 256)).length;
    }
    const elapsed = performance.now() - start;
    if (events !== 1 || parser.overflowed) throw new Error('Invalid benchmark output');
    samples.push(elapsed);
  }
  samples.sort((left, right) => left - right);
  rows.push({ payload_bytes: bytes, chunk_chars: 256, samples_ms: samples, median_ms: samples[2] });
}
console.log(JSON.stringify({
  node: process.version,
  source_sha256: createHash('sha256').update(readFileSync(source)).digest('hex'),
  scenario: 'valid single SSE data line, fixed 256-character fragments; local parser only',
  rows,
}, null, 2));
